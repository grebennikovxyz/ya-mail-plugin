import secrets

from .accounts import Problem, database
from .folders import allow
from .mail import summaries


def poll(client, account, folder, name, limit, include_existing):
    allow(account.email, folder, client)
    info = client.select_folder(folder, readonly=True)
    validity, ceiling = info[b"UIDVALIDITY"], info[b"UIDNEXT"] - 1
    key = (account.email, folder, name)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO feeds VALUES (?,?,?,?,?,NULL,NULL)",
            (*key, validity, 0 if include_existing else ceiling),
        )
        row = db.execute(
            "SELECT * FROM feeds WHERE email=? AND folder=? AND name=?", key
        ).fetchone()
        if row["validity"] != validity:
            raise Problem(
                "UIDVALIDITY папки изменился. Начните новое расписание с другим именем и проверьте период вручную."
            )
        start, end, receipt = row["position"] + 1, row["batch_end"], row["receipt"]
    # Never use 'start:*' when the box is empty or high-water exceeds last UID.
    upper = end if end is not None else ceiling
    ids = (
        sorted(client.search(["UNDELETED", "UID", f"{start}:{upper}"]))
        if start <= upper
        else []
    )
    if end is None:
        ids = ids[:limit]
        if ids:
            end, receipt = ids[-1], secrets.token_urlsafe(20)
            with database() as db:
                changed = db.execute(
                    """UPDATE feeds SET batch_end=?,receipt=? WHERE email=? AND folder=? AND name=?
                                     AND validity=? AND position=? AND receipt IS NULL""",
                    (end, receipt, *key, validity, start - 1),
                ).rowcount
                if not changed:
                    raise Problem(
                        "Другая проверка уже получила пакет. Повторите чтение."
                    )
    return {
        "messages": summaries(client, account, folder, validity, ids),
        "receipt": receipt,
        "has_more": bool(end is not None and end < ceiling),
        "start_after_uid": start - 1,
        "folder": folder,
        "feed": name,
    }


def acknowledge(account, folder, name, receipt):
    allow(account.email, folder)
    with database() as db:
        changed = db.execute(
            """UPDATE feeds SET position=batch_end,batch_end=NULL,receipt=NULL
                             WHERE email=? AND folder=? AND name=? AND receipt=?""",
            (account.email, folder, name, receipt),
        ).rowcount
    return {"advanced": bool(changed)}


def inventory(account, name, items):
    """An already-running task must also see the first arrival in a newly created folder."""
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = dict(
            db.execute(
                "SELECT folder,from_start FROM feed_inventory WHERE email=? AND name=?",
                (account.email, name),
            )
        )
        fresh = {
            i["folder"]: bool(previous)
            for i in items
            if not i.get("missing") and i["folder"] not in previous
        }
        db.executemany(
            "INSERT INTO feed_inventory VALUES(?,?,?,?)",
            [(account.email, name, folder, start) for folder, start in fresh.items()],
        )
    return previous | fresh
