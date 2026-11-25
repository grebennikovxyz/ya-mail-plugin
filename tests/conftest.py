from contextlib import nullcontext
from email import policy
from email.message import EmailMessage

import pytest

from ya_mail import accounts, mail, server

EMAIL = "qa@example.org"


@pytest.fixture(autouse=True)
def local_database(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "DATA", tmp_path)


class Vault:
    def __init__(self):
        self.records = {}

    def get_password(self, service, key):
        return self.records.get((service, key))

    def set_password(self, service, key, value):
        self.records[service, key] = value

    def delete_password(self, service, key):
        del self.records[service, key]


class Box:
    def __init__(self):
        self.data = {"INBOX": {}, "Drafts": {}, "Archive": {}, "Trash": {}}
        self.selected = None
        self.validity = 50
        self.flags = {}
        self.writable = False
        self.permanent = (rb"\*", rb"\Seen", rb"\Flagged")
        self.calls = []

    def add(self, folder="INBOX", subject="Счёт за печать"):
        msg = EmailMessage(policy=policy.SMTP)
        uid = max(self.data[folder], default=0) + 1
        msg["From"] = EMAIL
        msg["To"] = "user@example.org"
        msg["Subject"] = subject
        label = folder if folder.isascii() else folder.encode().hex()
        msg["Message-ID"] = f"<{uid}-{label}@example.org>"
        msg.set_content("Заказ готов")
        msg.add_attachment(
            b"fixture", maintype="application", subtype="pdf", filename="Счёт.pdf"
        )
        self.data[folder][uid] = msg.as_bytes()
        return uid

    def select_folder(self, folder, readonly=False):
        self.selected, self.writable = folder, not readonly
        return {
            b"UIDNEXT": max(self.data[folder], default=0) + 1,
            b"UIDVALIDITY": self.validity,
            b"PERMANENTFLAGS": self.permanent,
            b"FLAGS": self.permanent,
        }

    def list_folders(self):
        return [((), b"/", k) for k in self.data]

    def search(self, terms, charset=None):
        self.calls.append(("search", terms, charset))
        ids = list(self.data[self.selected])
        if "UID" in terms:
            lo, hi = map(int, terms[terms.index("UID") + 1].split(":"))
            ids = [i for i in ids if lo <= i <= hi]
        if "KEYWORD" in terms:
            flag = terms[terms.index("KEYWORD") + 1].encode()
            ids = [i for i in ids if flag in self.flags.get(i, set())]
        return ids

    def get_flags(self, ids):
        return {
            i: tuple(self.flags.get(i, set()))
            for i in ids
            if i in self.data[self.selected]
        }

    def fetch(self, ids, items):
        self.calls.append(("fetch", tuple(items)))
        if isinstance(ids, str):
            lo, hi = map(int, ids.split(":"))
            ids = range(lo, hi + 1)
        return {
            i: {
                b"BODY[]": raw,
                mail.HEADER: raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n",
                b"RFC822.SIZE": len(raw),
                b"FLAGS": tuple(self.flags.get(i, set())),
            }
            for i, raw in self.data[self.selected].items()
            if i in ids
        }

    def add_flags(self, ids, flags, silent=False):
        assert self.writable
        for i in ids:
            self.flags.setdefault(i, set()).update(f.encode() for f in flags)

    def remove_flags(self, ids, flags, silent=False):
        assert self.writable
        for i in ids:
            self.flags.setdefault(i, set()).difference_update(f.encode() for f in flags)

    def append(self, folder, msg, flags):
        uid = max(self.data[folder], default=0) + 1
        self.data[folder][uid] = msg
        return f"[APPENDUID {self.validity} {uid}]".encode()

    def find_special_folder(self, role):
        return {
            rb"\Trash": "Trash",
            rb"\Drafts": "Drafts",
            rb"\Archive": "Archive",
        }.get(role)

    def has_capability(self, name):
        return name in ("MOVE", "UIDPLUS")

    def move(self, ids, dest):
        assert self.writable
        for i in ids:
            self.data[dest][max(self.data[dest], default=0) + 1] = self.data[
                self.selected
            ].pop(i)


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = Vault()
    monkeypatch.setattr(accounts, "DATA", tmp_path)
    monkeypatch.setattr(accounts, "vault", lambda: vault)
    account = accounts.Account(EMAIL, "send", "synthetic-pass")
    accounts.save(account)
    box = Box()
    monkeypatch.setattr(server, "mailbox", lambda account: nullcontext(box))
    return account, box
