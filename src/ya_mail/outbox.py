import base64
import hashlib
import json
import re
import smtplib
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .accounts import Problem, address, database
from .mail import Ref, Short, raw_message, special
from .transport import outgoing

Addresses = Annotated[list[Short], Field(max_length=100)]


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Short
    content_base64: Annotated[str, Field(max_length=5600000)]
    mime: Short = "application/octet-stream"


class Letter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Addresses = Field(default_factory=list)
    cc: Addresses = Field(default_factory=list)
    bcc: Addresses = Field(default_factory=list)
    subject: Short
    text: Annotated[str, Field(max_length=500000)]
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)
    reply_to_message: Ref | None = None
    forward_message: Ref | None = None


def build(client, account, letter):
    if letter.reply_to_message and letter.forward_message:
        raise Problem("Выберите ответ или пересылку.")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = account.email
    for header, entries in (("To", letter.to), ("Cc", letter.cc), ("Bcc", letter.bcc)):
        if entries:
            msg[header] = ", ".join(address(item) for item in entries)
    msg["Subject"] = letter.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=account.email.split("@")[1])
    if letter.reply_to_message:
        original = raw_message(client, letter.reply_to_message, account)
        parent = str(original.get("Message-ID", ""))
        if not re.fullmatch(r"<[^<>\s]+>", parent):
            raise Problem("У исходного письма отсутствует корректный Message-ID.")
        refs = re.findall(r"<[^<>\s]+>", str(original.get("References", "")))
        msg["In-Reply-To"] = parent
        msg["References"] = " ".join([*refs[-20:], parent])
    msg.set_content(letter.text)
    if letter.forward_message:
        original = raw_message(client, letter.forward_message, account)
        # Hidden delivery details must not travel with a forwarded message.
        for header in (
            "Bcc",
            "Resent-Bcc",
            "Delivered-To",
            "Return-Path",
            "Received",
            "Authentication-Results",
        ):
            del original[header]
        msg.add_attachment(original, filename="forwarded.eml")
    for att in letter.attachments:
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", att.mime) or any(
            c in att.name for c in "\\/"
        ):
            raise Problem("Проверьте MIME-тип и имя вложения.")
        try:
            payload = base64.b64decode(att.content_base64, validate=True)
        except ValueError:
            raise Problem("Некорректный base64 вложения.") from None
        msg.add_attachment(
            payload,
            maintype=att.mime.split("/")[0],
            subtype=att.mime.split("/")[1],
            filename=att.name,
        )
    if len(msg.as_bytes()) > 25 * 1024 * 1024:
        raise Problem("Исходящее письмо превышает 25 МиБ.")
    return msg


def draft(client, account, letter):
    msg = build(client, account, letter)
    folder = special(client, "drafts")
    reply = client.append(folder, msg.as_bytes(), flags=[r"\Draft", r"\Seen"])
    match = re.search(rb"\bAPPENDUID (\d+) (\d+)", reply or b"", re.IGNORECASE)
    ref = None
    if match:
        ref = Ref(
            account=account.email,
            folder=folder,
            validity=int(match[1]),
            uid=int(match[2]),
        )
    else:
        info = client.select_folder(folder, readonly=True)
        found = client.search(["HEADER", "Message-ID", str(msg["Message-ID"])])
        if len(found) == 1:
            ref = Ref(
                account=account.email,
                folder=folder,
                validity=info[b"UIDVALIDITY"],
                uid=found[0],
            )
    return {
        "folder": folder,
        "message_id": str(msg["Message-ID"]),
        "ref": ref.model_dump() if ref else None,
    }


def submit(account, request, fingerprint, msg):
    """Reserve before SMTP DATA. An ambiguous delivery may never be retried automatically."""
    digest = hashlib.sha256(fingerprint).hexdigest()
    request = str(request)
    message_id = str(msg.get("Message-ID") or make_msgid())
    if not msg.get("Message-ID"):
        msg["Message-ID"] = message_id
    recipients = [
        addr.addr_spec
        for key in ("To", "Cc", "Bcc")
        for h in msg.get_all(key, [])
        for addr in h.addresses
    ]
    if not recipients:
        raise Problem("Для отправки укажите хотя бы одного адресата.")
    if any(
        msg.get_all(key)
        for key in (
            "Resent-Date",
            "Resent-From",
            "Resent-To",
            "Resent-Cc",
            "Resent-Bcc",
        )
    ):
        raise Problem("Для отправки пересланного черновика создайте новое письмо.")
    if len(msg.get_all("From", [])) != 1 or [
        a.addr_spec.lower() for a in msg["From"].addresses
    ] != [account.email]:
        raise Problem(
            "Отправитель черновика должен совпадать с адресом подключённого ящика."
        )
    recipients = list(dict.fromkeys(address(r) for r in recipients))
    if len(msg.as_bytes()) > 25 * 1024 * 1024:
        raise Problem("Исходящее письмо превышает 25 МиБ.")

    def existing(db):
        row = db.execute(
            "SELECT * FROM sends WHERE email=? AND request=?", (account.email, request)
        ).fetchone()
        if row:
            if row["digest"] != digest:
                raise Problem("Этот ключ отправки уже использован с другим содержимым.")
            if row["result"]:
                return json.loads(row["result"])
            raise Problem(
                "Результат отправки ещё неизвестен. Проверьте «Отправленные» по Message-ID "
                + row["message_id"]
            )

    with database() as db:
        previous = existing(db)
    if previous:
        return previous
    with outgoing(account) as smtp:
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = existing(db)
            if previous:
                return previous
            db.execute(
                "INSERT INTO sends VALUES (?,?,?,?,NULL)",
                (account.email, request, digest, message_id),
            )
        # smtplib strips Bcc from the transmitted MIME, retaining envelope recipients.
        try:
            refused = smtp.send_message(
                msg, from_addr=account.email, to_addrs=recipients
            )
        except smtplib.SMTPRecipientsRefused as exc:
            refused = exc.recipients
        except (
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPNotSupportedError,
        ):
            # These are explicit rejections before acceptance. Authentication was
            # already successful and a corrected request may be retried.
            with database() as db:
                db.execute(
                    "DELETE FROM sends WHERE email=? AND request=?",
                    (account.email, request),
                )
            raise Problem(
                "SMTP отклонил отправку. Проверьте адреса и размер письма."
            ) from None
        except (OSError, smtplib.SMTPException):
            raise Problem(
                "Ответ SMTP потерян. Проверьте «Отправленные» по Message-ID "
                + message_id
            ) from None
        result = {
            "message_id": message_id,
            "accepted": [r for r in recipients if r not in refused],
            "refused": list(refused),
        }
        with database() as db:
            db.execute(
                "UPDATE sends SET result=? WHERE email=? AND request=?",
                (json.dumps(result), account.email, request),
            )
        return result
