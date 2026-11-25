import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from platformdirs import user_data_path

DATA = user_data_path("YaMailPlugin", "Grebennikov")
SERVICE = "org.grebennikov.ya-mail"
Mode = Literal["read", "organize", "send"]
MODES = ("read", "organize", "send")


class Problem(ValueError):
    """An actionable message that is safe to show in chat."""


def address(value: str) -> str:
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(
        r"[A-Za-z0-9_+%=-]+(?:\.[A-Za-z0-9_+%=-]+)*@(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+",
        value,
    ):
        raise Problem("Укажите полный почтовый адрес без имени и угловых скобок.")
    return value


@dataclass(frozen=True)
class Account:
    email: str
    mode: Mode
    password: str = field(repr=False)

    @property
    def login(self):
        name, domain = self.email.split("@")
        return name if domain.lower() == "yandex.ru" else self.email


def vault():
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Backend
    elif sys.platform == "darwin":
        from keyring.backends.macOS import Keyring as Backend
    else:
        from keyring.backends.SecretService import Keyring as Backend
    return Backend()


@contextmanager
def database():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "mail.sqlite3", timeout=35)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(
                email TEXT PRIMARY KEY, mode TEXT NOT NULL, secret TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feeds(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, folder TEXT, name TEXT,
                validity INTEGER NOT NULL, position INTEGER NOT NULL, batch_end INTEGER, receipt TEXT,
                PRIMARY KEY(email, folder, name));
            CREATE TABLE IF NOT EXISTS sends(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, request TEXT, digest TEXT,
                message_id TEXT, result TEXT, PRIMARY KEY(email,request));
            CREATE TABLE IF NOT EXISTS labels(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, name TEXT,
                PRIMARY KEY(email,name));
            CREATE TABLE IF NOT EXISTS thread_headers(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, folder TEXT,
                validity INTEGER, uid INTEGER, hashes TEXT NOT NULL,
                PRIMARY KEY(email,folder,validity,uid));
            CREATE TABLE IF NOT EXISTS mailbox_names(
                email TEXT PRIMARY KEY REFERENCES accounts(email) ON DELETE CASCADE, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS folder_rules(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, folder TEXT, delimiter TEXT,
                mode TEXT NOT NULL, PRIMARY KEY(email,folder));
            CREATE TABLE IF NOT EXISTS feed_inventory(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, name TEXT, folder TEXT,
                from_start INTEGER NOT NULL, PRIMARY KEY(email,name,folder));
            CREATE TABLE IF NOT EXISTS routed_messages(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, digest TEXT, origin TEXT NOT NULL,
                PRIMARY KEY(email,digest));
            CREATE TABLE IF NOT EXISTS delivery_jobs(
                id TEXT PRIMARY KEY, email TEXT REFERENCES accounts(email) ON DELETE CASCADE,
                request TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, due REAL,
                mime BLOB, preview TEXT NOT NULL, sources TEXT NOT NULL, draft_ref TEXT,
                result TEXT, started INTEGER NOT NULL DEFAULT 0, UNIQUE(email,request));
            CREATE TABLE IF NOT EXISTS draft_origins(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, ref TEXT PRIMARY KEY,
                sources TEXT NOT NULL, protected INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS observed_uids(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, folder TEXT,
                validity INTEGER, uid INTEGER, observed REAL NOT NULL, PRIMARY KEY(email,folder,validity,uid));
            CREATE TABLE IF NOT EXISTS learning(
                email TEXT PRIMARY KEY REFERENCES accounts(email) ON DELETE CASCADE, mode TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_exports(
                email TEXT PRIMARY KEY REFERENCES accounts(email) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS contact_evidence(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, ref TEXT, addr TEXT, name TEXT,
                folder TEXT NOT NULL, identity TEXT NOT NULL, stamp REAL NOT NULL, subject TEXT, sent INTEGER,
                PRIMARY KEY(email,ref,addr));
            CREATE TABLE IF NOT EXISTS contact_aliases(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, addr TEXT, alias TEXT,
                PRIMARY KEY(email,addr,alias));
            CREATE TABLE IF NOT EXISTS forgotten_contacts(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, addr TEXT, PRIMARY KEY(email,addr));
            CREATE TABLE IF NOT EXISTS contact_choices(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, addr TEXT, PRIMARY KEY(email,addr));
            CREATE TABLE IF NOT EXISTS sent_index(
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE, folder TEXT, validity INTEGER,
                position INTEGER NOT NULL, PRIMARY KEY(email,folder,validity));
            CREATE TABLE IF NOT EXISTS mail_facts(
                id INTEGER PRIMARY KEY, email TEXT REFERENCES accounts(email) ON DELETE CASCADE,
                ref TEXT NOT NULL, category TEXT NOT NULL, fact TEXT NOT NULL, correspondent TEXT NOT NULL,
                date TEXT NOT NULL, revision INTEGER NOT NULL, quote TEXT NOT NULL, UNIQUE(email,ref,category,fact));
            CREATE TABLE IF NOT EXISTS fact_revisions(
                fact_id INTEGER REFERENCES mail_facts(id) ON DELETE CASCADE, revision INTEGER,
                ref TEXT NOT NULL, category TEXT NOT NULL, fact TEXT NOT NULL, correspondent TEXT NOT NULL,
                date TEXT NOT NULL, quote TEXT NOT NULL, PRIMARY KEY(fact_id,revision));
        """)
        with db:
            yield db
    finally:
        db.close()


def profiles():
    with database() as db:
        return [
            {k: v for k, v in dict(r).items() if v is not None}
            for r in db.execute(
                "SELECT a.email,a.mode,n.name FROM accounts a LEFT JOIN mailbox_names n ON a.email=n.email ORDER BY a.email"
            )
        ]


def save(account):
    key = uuid4().hex
    backend = vault()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT secret FROM accounts WHERE email=?", (account.email,)
        ).fetchone()
        backend.set_password(
            SERVICE, key, json.dumps([account.email, account.mode, account.password])
        )
        try:
            db.execute(
                """INSERT INTO accounts VALUES (?,?,?) ON CONFLICT(email)
                       DO UPDATE SET mode=excluded.mode, secret=excluded.secret""",
                (account.email, account.mode, key),
            )
            db.commit()
        except Exception:
            backend.delete_password(SERVICE, key)
            raise
    if old:
        backend.delete_password(SERVICE, old["secret"])


def current(email, access: Mode = "read"):
    email = address(email).lower()
    with database() as db:
        db.execute("BEGIN")
        row = db.execute(
            "SELECT secret FROM accounts WHERE email=?", (email,)
        ).fetchone()
        data = row and vault().get_password(SERVICE, row["secret"])
    if not data:
        raise Problem(
            "Подключите этот ящик на своём компьютере командой ya-mail connect."
        )
    account = Account(*json.loads(data))
    if (
        account.email != email
        or account.mode not in MODES
        or MODES.index(account.mode) < MODES.index(access)
    ):
        raise Problem(
            "Этот режим доступа не разрешает операцию. Режим выбирает владелец в мастере."
        )
    return account


def forget(email):
    email = address(email).lower()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT secret FROM accounts WHERE email=?", (email,)
        ).fetchone()
        if row:
            from .knowledge import invalidate_files

            invalidate_files(email)
            db.execute("DELETE FROM accounts WHERE email=?", (email,))
            if vault().get_password(SERVICE, row["secret"]) is not None:
                vault().delete_password(SERVICE, row["secret"])
