"""UID-bound moves and permanent cleanup of server-advertised system folders."""

import json
import re
import time
from collections import defaultdict
from email.utils import getaddresses

from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field, ValidationError

from . import folders, mail
from .accounts import Problem, current, database, profiles
from .transport import mailbox


class ConfirmTrash(BaseModel):
    confirmation: str = Field(
        title="Введите фразу под списком писем", min_length=1, max_length=80
    )


def printable(value, limit=None):
    # Render a single line per message, including attacker-controlled headers, as quoted data.
    text = "".join(c if c.isprintable() else " " for c in str(value))
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return json.dumps(text, ensure_ascii=False).replace("`", r"\u0060")


def needs_digest(client, dest, role):
    cleanup_flags = {r"\trash", r"\junk", r"\spam", r"\drafts"}
    target = dest.casefold()
    if (
        role in {"trash", "spam", "drafts"}
        or folders.SYSTEM_ROOTS.get(target) in cleanup_flags
    ):
        return True
    listed = list(client.list_folders())
    if client.has_capability("XLIST"):
        listed.extend(client.xlist_folders())
    trash = set()
    for flags, _, name in listed:
        roles = {flag.decode().lower() for flag in flags}
        if name.casefold() == target and roles & cleanup_flags:
            return True
        if r"\trash" in roles:
            trash.add(name)
    # When Trash is ambiguous, a named destination could be destructive.
    return len(trash) != 1


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
        result.extend(
            (r["ref"], r["From"], r["To"], r["Cc"], r["Date"], r["Subject"])
            for r in rows
        )
    return dest, result, needs_digest(client, dest, role)


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
    with database() as db:
        number = db.execute(
            """INSERT INTO removal_counter VALUES(1,1)
               ON CONFLICT(id) DO UPDATE SET number=number+1 RETURNING number"""
        ).fetchone()[0]
    digest_id = f"R-{number:02d}"
    phrase = f"Удалить по дайджесту {digest_id}"
    own = {item["email"].casefold() for item in profiles()} | {email.casefold()}
    lines = [
        f"Дайджест {digest_id}. Писем к перемещению: {len(snapshot)}.",
        "```",
        f"Ящик {printable(email)} → папка {printable(dest)}",
        "Папка · UID · Корреспондент · Дата · Тема",
    ]
    for ref, sender, to, cc, date, subject in snapshot:
        peer = next(
            (
                f"{name[:80]} <{addr}>" if name else addr
                for field in (sender, to, cc)
                for name, addr in getaddresses([field])
                if addr and addr.casefold() not in own
            ),
            "Внешний корреспондент не указан",
        )
        lines.append(
            " · ".join(
                (
                    printable(ref["folder"]),
                    str(ref["uid"]),
                    printable(peer),
                    printable(date, 80),
                    printable(subject, 200),
                )
            )
        )
    lines.extend(
        ["```", "Для подтверждения введите в поле формы точную фразу:", phrase]
    )
    try:
        response = await ctx.elicit("\n".join(lines), ConfirmTrash)
    except (McpError, ValidationError) as exc:
        raise Problem(
            "Письма сохранены. Требуется ответ владельца в текстовой форме MCP."
        ) from exc
    if response.action != "accept" or response.data.confirmation != phrase:
        raise Problem(
            "Письма сохранены. Подтверждение для этого дайджеста не получено."
        )
    return digest_id


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
    digest_id = (
        await confirm_snapshot(ctx, acc.email, dest, snapshot) if sensitive else None
    )
    acc = current(acc.email, "organize")
    with connect(acc) as client:
        if inspect(client, acc, groups, folder, role) != (dest, snapshot, sensitive):
            raise Problem("Выборка изменилась. Повторите поиск.")
        moved = []
        for subset in groups.values():
            mail.checked_folder(client, subset[0], acc, write=True)
            uids = [r.uid for r in subset]
            if set(client.get_flags(uids)) != set(uids):
                raise Problem("Выборка изменилась. Проверьте папки перед повтором.")
            if dest != subset[0].folder:
                folders.allow(acc.email, dest, client)
                reply = move_uids(client, uids, dest)
                record_move(acc.email, dest, len(uids), reply)
            moved.extend(r.model_dump() for r in subset)
    result = {"destination": dest, "uids": [r["uid"] for r in moved], "messages": moved}
    if digest_id:
        result["digest_id"] = digest_id
    return result


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
