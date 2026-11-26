"""UID-bound moves and permanent cleanup of server-advertised system folders."""

import json
import re
import time
from collections import defaultdict

from pydantic import BaseModel, Field

from . import folders, mail
from .accounts import Problem, current, database
from .transport import mailbox


class ConfirmTrash(BaseModel):
    move_listed_messages_to_trash: bool = Field(
        title="Переместить только перечисленные письма"
    )


def printable(value):
    # Render a single line per message, including attacker-controlled headers, as quoted data.
    import unicodedata

    return json.dumps(
        "".join(
            c if not unicodedata.category(c).startswith("C") else " "
            for c in str(value)
        ),
        ensure_ascii=False,
    )


def inspect(client, acc, groups, folder, role):
    dest = folder or mail.special(client, role)
    folders.allow(acc.email, dest, client)
    result = []
    for refs in groups.values():
        mail.checked_folder(client, refs[0], acc)
        uids = [r.uid for r in refs]
        rows = mail.summaries(client, acc, refs[0].folder, refs[0].validity, uids)
        if len(rows) != len(uids):
            raise Problem("Выборка изменилась. Повторите поиск перед перемещением.")
        result.extend((r["ref"], r["From"], r["Date"], r["Subject"]) for r in rows)
    trash = folders.verified_role(client, "trash")
    spam = folders.verified_role(client, "spam")
    sensitive = (
        role in {"trash", "spam"}
        or not trash
        or dest.casefold() in {trash.casefold(), (spam or "").casefold()}
    )
    return dest, result, sensitive


def move_uids(client, uids, dest):
    if client.has_capability("MOVE"):
        return client.move(uids, dest)
    elif client.has_capability("UIDPLUS"):
        reply = client.copy(uids, dest)
        try:
            client.add_flags(uids, [r"\Deleted"], silent=True)
            client.uid_expunge(uids)
        except Exception:
            raise Problem(
                "Копия уже создана. Проверьте обе папки перед повтором перемещения."
            ) from None
        return reply
    else:
        raise Problem(
            "Сервер не поддерживает MOVE или UIDPLUS для точного перемещения."
        )


def record_move(email, dest, count, reply):
    match = re.search(
        rb"\bCOPYUID (\d+) [\d:,]+ ([\d:,]+)\]", reply or b"", re.IGNORECASE
    )
    if not match:
        return
    uids = []
    for part in match[2].split(b","):
        bounds = part.split(b":")
        if len(bounds) > 2 or not all(b.isdigit() and len(b) <= 10 for b in bounds):
            return
        lo, hi = sorted([int(bounds[0]), int(bounds[-1])])
        if lo < 1 or hi - lo + 1 > count - len(uids):
            return
        uids.extend(range(lo, hi + 1))
    if len(uids) == count:
        with database() as db:
            db.executemany(
                "INSERT OR IGNORE INTO observed_uids VALUES(?,?,?,?,?)",
                [(email, dest, int(match[1]), u, time.time()) for u in uids],
            )


async def confirm_snapshot(ctx, email, dest, snapshot):
    lines = [
        f"Переместить перечисленные письма? Ящик {email}. Папка назначения {printable(dest)}."
    ]
    for ref, sender, date, subject in snapshot:
        lines.append(
            " · ".join(
                map(printable, (ref["folder"], ref["uid"], sender, date, subject))
            )
        )
    response = await ctx.elicit("\n".join(lines), ConfirmTrash)
    if response.action != "accept" or not response.data.move_listed_messages_to_trash:
        raise Problem("Письма сохранены.")


async def move(refs, folder, role, ctx, connect):
    if bool(folder) == bool(role):
        raise Problem("Укажите имя папки или её роль.")
    acc = current(refs[0].account, "organize")
    groups = defaultdict(list)
    for r in {r.model_dump_json(): r for r in refs}.values():
        if r.account != acc.email:
            raise Problem("Выберите письма одного ящика.")
        groups[r.folder, r.validity].append(r)
    with connect(acc) as client:
        dest, snapshot, sensitive = inspect(client, acc, groups, folder, role)
        needs_consent = len(snapshot) > 1 and sensitive
    if needs_consent:
        await confirm_snapshot(ctx, acc.email, dest, snapshot)
    with connect(current(acc.email, "organize")) as client:
        if inspect(client, acc, groups, folder, role) != (dest, snapshot, sensitive):
            raise Problem("Выборка изменилась. Повторите поиск.")
        moved = []
        for subset in groups.values():
            mail.checked_folder(client, subset[0], acc, write=True)
            uids = [r.uid for r in subset]
            if set(client.get_flags(uids)) != set(uids):
                raise Problem("Выборка изменилась. Проверьте папки перед повтором.")
            if dest != subset[0].folder:
                reply = move_uids(client, uids, dest)
                record_move(acc.email, dest, len(uids), reply)
            moved.extend(r.model_dump() for r in subset)
    return {"destination": dest, "uids": [r["uid"] for r in moved], "messages": moved}


def cleanup(acc, role, days, connect=mailbox):
    threshold = (
        days if days is not None else {"spam": 0, "trash": 10, "drafts": 7}[role]
    )
    with connect(acc) as client:
        folder = folders.verified_role(client, role)
        if not folder:
            raise Problem(
                "Сервер не подтвердил уникальную системную роль папки. Очистка остановлена."
            )
        folders.allow(acc.email, folder, client)
        if not client.has_capability("UIDPLUS"):
            raise Problem("Точная очистка требует UIDPLUS.")
        info = client.select_folder(folder, readonly=False)
        validity = info[b"UIDVALIDITY"]
        uids = sorted(set(client.search(["ALL"])))
        now = time.time()
        deleted = 0
        protected = 0
        # UIDVALIDITY+UID is immutable; editing a stored IMAP draft creates a new UID.
        # Even INTERNALDATE may have been preserved by COPY into Trash or Drafts.
        from itertools import batched

        for group in batched(uids, 100):
            folders.allow(acc.email, folder, client)
            with database() as db:
                db.execute("BEGIN IMMEDIATE")
                rules = {
                    r["folder"]: (r["mode"], r["delimiter"])
                    for r in db.execute(
                        "SELECT * FROM folder_rules WHERE email=?", (acc.email,)
                    )
                }
                if folders.blocked(folder, rules):
                    raise Problem("Доступ к папке запрещён.")
                db.executemany(
                    "INSERT OR IGNORE INTO observed_uids VALUES(?,?,?,?,?)",
                    [(acc.email, folder, validity, uid, now) for uid in group],
                )
                candidates = []
                for uid in group:
                    ref = mail.Ref(
                        account=acc.email, folder=folder, validity=validity, uid=uid
                    ).model_dump_json()
                    if (
                        role == "drafts"
                        and db.execute(
                            """SELECT 1 FROM delivery_jobs WHERE email=? AND draft_ref=?
                        AND state IN ('queued','dispatching','stopped','saving_draft','uncertain')
                        UNION ALL SELECT 1 FROM draft_origins WHERE email=? AND ref=? AND protected>0 LIMIT 1""",
                            (acc.email, ref, acc.email, ref),
                        ).fetchone()
                    ):
                        protected += 1
                        continue
                    observed = db.execute(
                        "SELECT observed FROM observed_uids WHERE email=? AND folder=? AND validity=? AND uid=?",
                        (acc.email, folder, validity, uid),
                    ).fetchone()[0]
                    if threshold == 0 or now - observed > threshold * 86400:
                        candidates.append(uid)
                if candidates:
                    if role == "drafts":
                        pending_ids = {
                            json.loads(row[0]).get("check_drafts_for")
                            for row in db.execute(
                                "SELECT result FROM delivery_jobs WHERE email=? AND state IN ('saving_draft','stopped') AND result IS NOT NULL",
                                (acc.email,),
                            )
                        }
                        pending_ids.discard(None)
                        if pending_ids:
                            headers = client.fetch(candidates, [mail.HEADER_PEEK])
                            safe = [
                                uid
                                for uid in candidates
                                if uid in headers
                                and str(
                                    mail.fetched_headers(headers[uid]).get(
                                        "Message-ID", ""
                                    )
                                )
                                not in pending_ids
                            ]
                            protected += len(candidates) - len(safe)
                            candidates = safe
                if candidates:
                    client.add_flags(candidates, [r"\Deleted"], silent=True)
                    client.uid_expunge(candidates)
                    deleted += len(candidates)
                    db.executemany(
                        "DELETE FROM observed_uids WHERE email=? AND folder=? AND validity=? AND uid=?",
                        [(acc.email, folder, validity, u) for u in candidates],
                    )
    return {
        "account": acc.email,
        "folder": folder,
        "role": role,
        "days": threshold,
        "observed": len(uids),
        "deleted": deleted,
        "protected": protected,
    }
