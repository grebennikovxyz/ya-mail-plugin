"""One-use encrypted mailbox setup for a browser outside the Gateway network."""

import base64
import json
import re
import secrets
import sys
import time

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import accounts
from .accounts import MODES, Problem, address
from .transport import connect

FORM = "https://grebennikovxyz.github.io/ya-mail-plugin/"
TTL = 600


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def challenge(email):
    email = address(email).lower()
    now = int(time.time())
    with accounts.database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM mailbox_pairings WHERE expires <= ?", (now,))
        if db.execute("SELECT COUNT(*) FROM mailbox_pairings").fetchone()[0] >= 16:
            raise Problem("Слишком много открытых форм. Повторите через 10 минут.")
        ticket = secrets.token_hex(16)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        db.execute(
            "INSERT INTO mailbox_pairings VALUES (?,?,?,?)",
            (ticket, email, now + TTL, private),
        )
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    context = encode(
        json.dumps(
            {
                "id": ticket,
                "email": email,
                "key": encode(public),
                "linux": sys.platform == "linux",
            },
            separators=(",", ":"),
        ).encode()
    )
    return {
        "url": FORM + "#" + context,
        "account": email,
        "expires_in_seconds": TTL,
        "next_tool": "finish_mailbox_connection",
        "instructions": "Откройте форму, создайте в Яндекс ID пароль приложения типа «Почта» и введите его в форму. Верните в личный чат только полученный код YM1.",
    }


def finish(envelope):
    match = re.fullmatch(r"YM1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{512})", envelope)
    if not match:
        raise Problem("Вставьте полный зашифрованный код YM1 из формы.")
    ticket, ciphertext = match.groups()
    with accounts.database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM mailbox_pairings WHERE id=? AND expires>?",
            (ticket, int(time.time())),
        ).fetchone()
        if not row:
            raise Problem(
                "Срок подключения истёк или код уже использован. Запросите новую форму."
            )
        try:
            key = serialization.load_der_private_key(row["private_key"], password=None)
            label = ("Ya-Ma/1:" + ticket + ":" + row["email"]).encode()
            clear = key.decrypt(
                base64.urlsafe_b64decode(ciphertext),
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=label,
                ),
            )
            mode, password, local = json.loads(clear)
            if (
                mode not in MODES
                or not isinstance(password, str)
                or type(local) is not bool
            ):
                raise ValueError
        except (ValueError, TypeError, InvalidKey):
            raise Problem(
                "Код не соответствует этой форме. Откройте исходную ссылку."
            ) from None
        db.execute("DELETE FROM mailbox_pairings WHERE id=?", (ticket,))
    # Consume before network I/O. A failed login requires a fresh form.
    connect(row["email"], password, mode, local_store=local)
    return {"account": row["email"], "mode": mode, "connected": True}
