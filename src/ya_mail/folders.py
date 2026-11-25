"""Mailbox-local folder rules. IMAP supplies each hierarchy delimiter and role."""

from typing import Literal

from .accounts import Problem, database

Mode = Literal["priority", "normal", "skip", "blocked", "inherit"]
SYSTEM_ROLES = {
    r"\drafts",
    r"\sent",
    r"\trash",
    r"\junk",
    r"\spam",
    r"\archive",
    r"\all",
}
SYSTEM_ROOTS = {
    "drafts": r"\drafts",
    "черновики": r"\drafts",
    "sent": r"\sent",
    "sent items": r"\sent",
    "отправленные": r"\sent",
    "trash": r"\trash",
    "удалённые": r"\trash",
    "корзина": r"\trash",
    "junk": r"\junk",
    "spam": r"\junk",
    "спам": r"\junk",
    "archive": r"\archive",
    "архив": r"\archive",
}


def rules(email):
    with database() as db:
        return {
            r["folder"]: (r["mode"], r["delimiter"])
            for r in db.execute(
                "SELECT folder,mode,delimiter FROM folder_rules WHERE email=?", (email,)
            )
        }


def under(folder, parent, delimiter):
    return folder == parent or bool(delimiter and folder.startswith(parent + delimiter))


def blocked(folder, settings):
    return any(
        mode == "blocked" and under(folder.casefold(), parent.casefold(), delim)
        for parent, (mode, delim) in settings.items()
    )


def allow(email, folder, client=None, *, creating=False):
    settings = rules(email)
    if blocked(folder, settings):
        raise Problem("Доступ к этой папке запрещён владельцем ящика.")
    if client is not None and any(m == "blocked" for m, _ in settings.values()):
        items = catalog(client, email, settings)
        check_blocked_roots(items)
        if creating:
            return
        # A server's virtual All Mail can expose content of restricted folders.
        canonical = "INBOX" if folder.upper() == "INBOX" else folder
        if any(
            i["folder"] == canonical and i["selectable"] and i["mode"] != "blocked"
            for i in items
        ):
            return
        raise Problem("Доступ к папке не подтверждён при действующем запрете.")


def check_blocked_roots(items):
    if any(i.get("missing") and i["rule"] == "blocked" for i in items):
        raise Problem(
            "Закрытая папка исчезла или сменила имя. Проверьте настройки папок этого ящика и подтвердите новый путь."
        )


def catalog(client, email, settings=None):
    if settings is None:
        settings = rules(email)
    items = []
    defaults = {}
    listed = client.list_folders()
    advertised = {f.decode().lower() for flags, _, _ in listed for f in flags}
    if r"\spam" in advertised:
        advertised.add(r"\junk")
    for flags, delim, name in listed:
        delim = delim.decode() if isinstance(delim, bytes) else delim
        roles = [f.decode() for f in flags]
        lower = {r.lower() for r in roles}
        root = name.split(delim, 1)[0] if delim else name
        fallback = SYSTEM_ROOTS.get(name.casefold()) if root == name else None
        if lower & SYSTEM_ROLES or (fallback and fallback not in advertised):
            defaults[name] = ("skip", delim)
        items.append(
            dict(
                folder=name,
                delimiter=delim,
                roles=roles,
                selectable=r"\noselect" not in lower,
            )
        )
    known = {i["folder"] for i in items}
    for name, (_, delim) in settings.items():
        if name not in known:
            items.append(
                dict(
                    folder=name,
                    delimiter=delim,
                    roles=[],
                    selectable=False,
                    missing=True,
                )
            )
    policies = defaults | settings
    has_block = any(mode == "blocked" for mode, _ in settings.values())
    unresolved = any(
        path not in known and mode == "blocked" for path, (mode, _) in settings.items()
    )
    for item in items:
        name = item["folder"]
        aggregate = any(r.lower() == r"\all" for r in item["roles"])
        if unresolved or blocked(name, settings) or (aggregate and has_block):
            mode = "blocked"
        else:
            matches = [
                (len(path), mode)
                for path, (mode, delim) in policies.items()
                if under(name, path, delim)
            ]
            mode = max(matches)[1] if matches else "normal"
        item.update(mode=mode, rule=settings.get(name, (None, None))[0])
    return sorted(
        items,
        key=lambda i: (
            i["mode"] != "priority",
            i["folder"].upper() != "INBOX",
            i["folder"],
        ),
    )


def active(items, selected=None):
    return [
        i
        for i in items
        if i["selectable"]
        and i["mode"] in {"priority", "normal"}
        and (selected is None or i["folder"] in selected)
    ]


def update(email, folder, delimiter, mode, previous):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT mode FROM folder_rules WHERE email=? AND folder=?", (email, folder)
        ).fetchone()
        if (row[0] if row else None) != previous:
            raise Problem("Настройка уже изменилась. Обновите список папок.")
        if mode == "inherit":
            db.execute(
                "DELETE FROM folder_rules WHERE email=? AND folder=?", (email, folder)
            )
        else:
            db.execute(
                "INSERT INTO folder_rules VALUES(?,?,?,?) ON CONFLICT(email,folder) DO UPDATE SET mode=excluded.mode,delimiter=excluded.delimiter",
                (email, folder, delimiter, mode),
            )


def native_guard():
    # The separate OAuth adapter cannot attest to an IMAP account or folder.
    with database() as db:
        if db.execute(
            "SELECT 1 FROM folder_rules WHERE mode='blocked' LIMIT 1"
        ).fetchone():
            raise Problem(
                "Яндекс 360 не подтверждает ящик и папку для этого вызова. Используйте основной сервер Ya-Ma с проверкой доступа."
            )
