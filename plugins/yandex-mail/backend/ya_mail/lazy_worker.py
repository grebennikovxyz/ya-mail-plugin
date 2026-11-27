"""Dispatch claims in arrival order. Only the move journal can initiate an IMAP mutation."""

import asyncio

from filelock import Timeout

from . import accounts, folders, lazy_codex, lazy_mail, lazy_store, mail, removal
from .accounts import Problem
from .transport import mailbox


def reference(row):
    return mail.Ref(
        account=row["account"],
        folder=row["folder"],
        uid=row["uid"],
        validity=row["validity"],
    )


def collect(email, rules, guard):
    acc = accounts.current(email, "organize")
    cutoff = lazy_mail.years_ago(1)
    with mailbox(acc) as client:
        for folder in lazy_mail.permitted_sources(client, email, rules.folders):
            info = client.select_folder(folder, readonly=True)
            validity, ceiling = info[b"UIDVALIDITY"], info[b"UIDNEXT"]
            with lazy_store.db() as con:
                con.execute(
                    "DELETE FROM lazy_queue WHERE account=? AND folder=? AND validity!=? AND state='pending'",
                    (email, folder, validity),
                )
                cursor = con.execute(
                    "SELECT * FROM lazy_inventory WHERE account=? AND folder=?",
                    (email, folder),
                ).fetchone()
            start = (
                cursor["next_uid"]
                if cursor
                and cursor["validity"] == validity
                and cursor["cutoff"] == cutoff.isoformat()
                else 1
            )
            for lo in range(start, ceiling, 2000):
                if not guard():
                    return
                hi = min(lo + 1999, ceiling - 1)
                ids = client.search(
                    ["UNDELETED", "UNSEEN", "BEFORE", cutoff, "UID", f"{lo}:{hi}"]
                )
                rows = client.fetch(ids, ["INTERNALDATE"]) if ids else {}
                lazy_store.enqueue(
                    email,
                    folder,
                    validity,
                    [(uid, lazy_mail.stamp(r)) for uid, r in rows.items()],
                    rules.revision,
                )
                with lazy_store.db() as con:
                    con.execute(
                        "INSERT OR REPLACE INTO lazy_inventory VALUES(?,?,?,?,?)",
                        (email, folder, validity, cutoff.isoformat(), hi + 1),
                    )


def preview(email, rules, limit=8, *, classify=None):
    acc = accounts.current(email)
    with mailbox(acc) as client:
        candidates = sorted(
            (day, folder, validity, uid)
            for folder, validity, pairs in lazy_mail.inventory(
                client, acc, rules, limit
            )
            for uid, day in pairs
        )[:limit]
        history = lazy_mail.History()
        result = []
        for _, folder, validity, uid in candidates:
            ref = mail.Ref(account=email, folder=folder, validity=validity, uid=uid)
            item = lazy_mail.inspect(client, acc, rules, ref, history)
            if item.action == "classify" and classify:
                item.action, item.reason = lazy_codex.verdicts(classify(item), 1)[0]
            result.append(
                {
                    "ref": ref.model_dump(),
                    "subject": item.subject,
                    "decision": item.action,
                    "reason": item.reason,
                }
            )
        return result


def prepare(row, rules, history):
    acc = accounts.current(row["account"], "organize")
    with mailbox(acc) as client:
        return lazy_mail.inspect(client, acc, rules, reference(row), history)


def apply(row, rules, history, item, action, reason, guard, *, confirmed_dest=None):
    if not guard():
        return
    key = row["id"]
    if action in ("keep", "stale"):
        lazy_store.finish_item(key, action, reason, item.subject)
        return
    acc = accounts.current(row["account"], "organize")
    ref = reference(row)
    with mailbox(acc) as client:
        if lazy_store.policies().get(acc.email) != rules:
            raise Problem("Правила изменились.")

        def check():
            updated = lazy_mail.inspect(
                client, acc, rules, ref, history, with_text=False
            )
            if updated.action in ("keep", "stale"):
                lazy_store.finish_item(
                    key, updated.action, updated.reason, updated.subject
                )
                return False
            if updated.mid != item.mid:
                lazy_store.finish_item(
                    key, "uncertain", "Изменилась идентичность письма", item.subject
                )
                return False
            return updated.action == "classify" or action == "review"

        if not check() or not guard():
            return
        if action == "trash" and confirmed_dest is None:
            with lazy_store.db() as db:
                db.execute("UPDATE lazy_queue SET mid=? WHERE id=?", (item.mid, key))
            lazy_store.finish_item(key, "awaiting_trash", reason, item.subject)
            return
        dest = (
            folders.verified_role(client, "trash")
            if action == "trash"
            else lazy_mail.REVIEW
        )
        if not dest or (confirmed_dest and dest != confirmed_dest):
            raise Problem("Сервер не подтвердил выбранную Корзину.")
        if action == "review" and any(
            i["folder"] == dest
            and (
                not i["selectable"]
                or {r.lower() for r in i["roles"]} & folders.SYSTEM_ROLES
            )
            for i in folders.catalog(client, acc.email)
        ):
            raise Problem("Папка согласования совпала с системной папкой.")
        if dest == ref.folder:
            lazy_store.finish_item(key, "keep", "Уже в целевой папке", item.subject)
            return
        if action == "review" and not client.folder_exists(dest):
            folders.allow(acc.email, dest, client, creating=True)
            if not guard():
                return
            client.create_folder(dest)
        folders.allow(acc.email, dest, client)
        box = client.folder_status(dest, ["UIDVALIDITY", "UIDNEXT"])
        if not check():
            return
        mail.checked_folder(client, ref, acc, write=True)
        flags = client.get_flags([ref.uid]).get(ref.uid)
        if flags is None or {v.lower() for v in flags} & {
            rb"\seen",
            rb"\answered",
            rb"\deleted",
            rb"\flagged",
        }:
            lazy_store.finish_item(
                key, "keep", "Состояние письма изменилось", item.subject
            )
            return
        folders.allow(acc.email, dest, client)
        if not guard():
            return
        lazy_store.reserve_move(key, action, dest, box, item.mid, reason, item.subject)
        if not guard():
            lazy_store.finish_item(key, "pending", "Работа приостановлена")
            return
        reply = removal.move_uids(client, [ref.uid], dest)
        removal.record_move(acc.email, dest, 1, reply)
        lazy_store.finish_item(key, action, reason, item.subject)


def reconcile(email):
    with lazy_store.policy_lock():
        reconcile_moves(email)


def reconcile_moves(email):
    with lazy_store.db() as con:
        rows = list(
            con.execute(
                "SELECT * FROM lazy_queue WHERE account=? AND state IN ('moving_trash','moving_review')",
                (email,),
            )
        )
    if not rows:
        return
    acc = accounts.current(email, "organize")
    with mailbox(acc) as client:
        for row in rows:
            ref = reference(row)
            folders.allow(email, ref.folder, client)
            source = client.select_folder(ref.folder, readonly=True)
            if source[b"UIDVALIDITY"] != ref.validity:
                lazy_store.finish_item(
                    row["id"],
                    "uncertain",
                    "UIDVALIDITY исходной папки изменилась",
                    row["subject"],
                )
                continue
            source_present = ref.uid in client.get_flags([ref.uid])
            folders.allow(email, row["dest"], client)
            info = client.select_folder(row["dest"], readonly=True)
            matched = []
            if (
                row["mid"]
                and info[b"UIDVALIDITY"] == row["dest_validity"]
                and info[b"UIDNEXT"] > row["dest_next"]
            ):
                possible = lazy_mail.uid_pages(
                    client, info[b"UIDNEXT"], ["UNDELETED"], row["dest_next"]
                )
                matched = [
                    uid
                    for uid, _, msg in lazy_mail.headers(client, possible)
                    if str(msg.get("Message-ID", "")).strip() == row["mid"]
                ]
            if not source_present and len(matched) == 1:
                lazy_store.finish_item(
                    row["id"],
                    row["state"].removeprefix("moving_"),
                    row["reason"],
                    row["subject"],
                )
            else:
                lazy_store.finish_item(
                    row["id"],
                    "uncertain",
                    "Проверьте исходную и целевую папки перед повтором",
                    row["subject"],
                )


async def run(
    policies,
    classify,
    *,
    guard=lambda: True,
    width=lambda: 3,
    max_messages=None,
    token_budget=None,
):
    if not policies:
        raise Problem("Настройте хотя бы один ящик командой ya-mail lazy setup.")
    try:
        exclusive = lazy_store.lock().acquire(timeout=0)
    except Timeout:
        raise Problem("Другой разбор или мастер уже работает.") from None
    with exclusive:
        # Setup and another process can finish in the interval before the lock was acquired.
        current = lazy_store.policies()
        if any(current.get(email) != rules for email, rules in policies.items()):
            raise Problem("Правила изменились. Начните разбор снова.")
        run_id = lazy_store.start_run(policies)
        reason = "Очередь исчерпана"
        remaining = token_budget
        issued = 0
        stop = False
        moving = asyncio.Lock()

        def allowed():
            return not stop and guard()

        budget_exhausted = False
        histories = {email: lazy_mail.History() for email in policies}

        async def consume(slot):
            nonlocal issued, remaining, reason, stop, budget_exhausted
            more = True
            while more and allowed() and slot < width() and not budget_exhausted:
                batch = []
                batch_chars = 0
                for _ in range(4):
                    if not allowed() or budget_exhausted:
                        break
                    if max_messages is not None and issued >= max_messages:
                        reason = "Достигнут лимит писем"
                        more = False
                        break
                    row = lazy_store.claim(run_id, policies)
                    if not row:
                        more = False
                        break
                    issued += 1
                    email = row["account"]
                    rules = policies[email]
                    if lazy_store.policies().get(email) != rules:
                        reason = "Правила изменились"
                        stop = True
                        return
                    history = histories[email]
                    item = await asyncio.to_thread(prepare, row, rules, history)
                    if not allowed():
                        return
                    if item.action != "classify":
                        async with moving:
                            await asyncio.to_thread(
                                commit_item,
                                row,
                                rules,
                                history,
                                item,
                                item.action,
                                item.reason,
                                allowed,
                            )
                        continue
                    cost = len(lazy_codex.prompt([item]).encode()) + 512
                    if remaining is not None and cost > remaining:
                        budget_exhausted = True
                        reason = "Достигнут явный бюджет модели"
                        break
                    if remaining is not None:
                        remaining -= cost
                    batch.append((row, rules, history, item))
                    batch_chars += len(item.text)
                    if batch_chars >= 24000:
                        break
                if batch and allowed():
                    if any(
                        lazy_store.policies().get(e[0]["account"]) != e[1]
                        for e in batch
                    ):
                        reason = "Правила изменились"
                        stop = True
                        return
                    task = asyncio.create_task(classify([entry[3] for entry in batch]))
                    try:
                        while not task.done():
                            if not allowed():
                                return
                            await asyncio.wait([task], timeout=0.25)
                        decisions = lazy_codex.verdicts(await task, len(batch))
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                    for entry, decision in zip(batch, decisions, strict=True):
                        async with moving:
                            await asyncio.to_thread(
                                commit_item, *entry, *decision, allowed
                            )

        async def safe_worker(slot):
            nonlocal stop
            try:
                await consume(slot)
            except BaseException:
                stop = True
                raise

        try:
            if not any(r.sent for r in policies.values()):
                reason = "Без доступа к истории Отправленных защита переписки не подтверждена"
                return {"run": run_id, "reason": reason, "claimed": 0}
            policies = {k: v for k, v in policies.items() if v.sent}
            for email, rules in policies.items():
                if not allowed():
                    break
                await asyncio.to_thread(reconcile, email)
                await asyncio.to_thread(collect, email, rules, allowed)
            results = await asyncio.gather(
                *(safe_worker(i) for i in range(min(3, width()))),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            if not guard():
                reason = (
                    "Пауза: активность пользователя, лимит или устаревшие данные Codex"
                )
        except BaseException:
            reason = "Остановлено при ошибке или прерывании. Проверка журнала при следующем запуске."
            raise
        finally:
            stop = True
            lazy_store.finish_run(run_id, reason)
        return {"run": run_id, "reason": reason, "claimed": issued}


def commit_item(row, rules, history, item, action, why, allowed):
    with lazy_store.policy_lock():
        apply(row, rules, history, item, action, why, allowed)


async def watch(socket=None, *, once=False):
    while True:
        rules = [p for p in lazy_store.policies().values() if p.anchor]
        if not rules:
            return
        endpoints = {p.endpoint for p in rules}
        if not socket and len(endpoints) != 1:
            raise Problem(
                "Все автоматические ящики должны использовать один рабочий Codex."
            )
        endpoint = socket or rules[0].endpoint
        try:
            async with lazy_codex.Proxy(endpoint) as proxy:
                model = None
                while True:
                    policies = {
                        k: v for k, v in lazy_store.policies().items() if v.anchor
                    }
                    if not policies:
                        return
                    if not socket and any(
                        r.endpoint != endpoint for r in policies.values()
                    ):
                        break
                    anchors = {r.anchor for r in policies.values()}
                    weekly = min(r.weekly for r in policies.values())
                    await proxy.sample(anchors, weekly)
                    if proxy.allowed():
                        revision = ":".join(
                            k + "=" + v.revision for k, v in sorted(policies.items())
                        )
                        key = (*proxy.window_key, revision)
                        with lazy_store.db() as con:
                            done = con.execute(
                                "SELECT 1 FROM lazy_completed_windows WHERE owner=? AND reset=? AND revision=?",
                                key,
                            ).fetchone()
                        if done:
                            if once:
                                return
                            await asyncio.sleep(60)
                            continue
                        model = model or await lazy_codex.model_name(proxy)
                        generation = proxy.generation

                        def guard(generation=generation):
                            return proxy.allowed() and proxy.generation == generation

                        async def monitor(anchors=anchors, weekly=weekly):
                            try:
                                while True:
                                    await asyncio.sleep(3)
                                    await proxy.sample(anchors, weekly)
                                    if not guard():
                                        return
                            finally:
                                proxy.width = 0

                        poll = asyncio.create_task(monitor())
                        try:
                            report = await run(
                                policies,
                                lambda item, model=model: proxy.classify(model, item),
                                guard=guard,
                                width=lambda: proxy.width,
                            )
                            if guard() and (
                                report["reason"] == "Очередь исчерпана"
                                or not any(r.sent for r in policies.values())
                            ):
                                with lazy_store.db() as con:
                                    con.execute(
                                        "INSERT OR IGNORE INTO lazy_completed_windows VALUES(?,?,?)",
                                        key,
                                    )
                        finally:
                            poll.cancel()
                            await asyncio.gather(poll, return_exceptions=True)
                        if once:
                            return
                        await asyncio.sleep(60)
                    elif once:
                        return
                    else:
                        await asyncio.sleep(
                            60 if not proxy.active and proxy.sample_at else 5
                        )
        except (Problem, OSError, ValueError, TimeoutError, KeyError) as exc:
            if once:
                raise Problem(
                    "Не удалось подтвердить активность и квоту рабочего Codex."
                ) from exc
            await asyncio.sleep(30)


def digest(email, run_id=None, offset=0, limit=20):
    if offset < 0 or not 1 <= limit <= 100:
        raise Problem("Укажите неотрицательное смещение и от 1 до 100 записей.")
    acc = accounts.current(email)
    if not any(mode == "blocked" for mode, _ in folders.rules(acc.email).values()):
        return lazy_store.digest(acc.email, None, run_id, offset, limit)
    with mailbox(acc) as client:
        items = folders.catalog(client, email)
        folders.check_blocked_roots(items)
        permitted = {
            i["folder"] for i in items if i["selectable"] and i["mode"] != "blocked"
        }
    return lazy_store.digest(acc.email, permitted, run_id, offset, limit)


async def confirm_trash(account, run_id, limit, ctx):
    """Bind owner approval to this page, then repeat eligibility checks before moving."""
    acc = accounts.current(account, "organize")
    rules = lazy_store.policies().get(acc.email)
    if not rules or not rules.sent:
        raise Problem("Включите защиту личной переписки в мастере.")
    with mailbox(acc) as client:
        sources = set(lazy_mail.permitted_sources(client, acc.email, rules.folders))
        dest = folders.verified_role(client, "trash")
        if not dest:
            raise Problem("Сервер не подтвердил системную Корзину.")
        folders.allow(acc.email, dest, client)
        from itertools import islice

        with lazy_store.db() as db:
            rows = list(
                islice(
                    (
                        dict(r)
                        for r in db.execute(
                            "SELECT * FROM lazy_queue WHERE account=? AND run=? AND state='awaiting_trash' AND revision=? ORDER BY received,id",
                            (acc.email, run_id, rules.revision),
                        )
                        if r["folder"] in sources
                    ),
                    limit,
                )
            )
        snapshot, candidates = [], []
        for row in rows:
            ref = reference(row)
            mail.checked_folder(client, ref, acc)
            entry = next(lazy_mail.headers(client, [ref.uid]), None)
            if entry and str(entry[2].get("Message-ID", "")).strip() == row["mid"]:
                snapshot.append(
                    (
                        ref.model_dump(),
                        str(entry[2].get("From", "")),
                        str(entry[2].get("To", "")),
                        str(entry[2].get("Cc", "")),
                        entry[1][b"INTERNALDATE"].isoformat(),
                        str(entry[2].get("Subject", "")),
                    )
                )
                candidates.append(row)
            else:
                with lazy_store.policy_lock(), lazy_store.db() as db:
                    pending = db.execute(
                        "SELECT 1 FROM lazy_queue WHERE id=? AND state='awaiting_trash'",
                        (row["id"],),
                    ).fetchone()
                    if pending:
                        lazy_store.finish_item(
                            row["id"], "stale", "Исходное письмо перемещено"
                        )
        if not snapshot:
            return {"run": run_id, "moved": 0}
    digest_id = await removal.confirm_snapshot(ctx, acc.email, dest, snapshot)
    moved = 0
    with lazy_store.policy_lock():
        if lazy_store.policies().get(acc.email) != rules:
            raise Problem("Правила изменились. Откройте дайджест заново.")
        history = lazy_mail.History()
        for row in candidates:
            with lazy_store.db() as db:
                if not db.execute(
                    "SELECT 1 FROM lazy_queue WHERE id=? AND state='awaiting_trash'",
                    (row["id"],),
                ).fetchone():
                    continue
            item = lazy_mail.Decision(
                "trash", row["reason"], row["subject"], mid=row["mid"]
            )
            apply(
                row,
                rules,
                history,
                item,
                "trash",
                row["reason"],
                lambda: True,
                confirmed_dest=dest,
            )
            with lazy_store.db() as db:
                moved += (
                    db.execute(
                        "SELECT state FROM lazy_queue WHERE id=?", (row["id"],)
                    ).fetchone()[0]
                    == "trash"
                )
    return {"run": run_id, "moved": moved, "digest_id": digest_id}
