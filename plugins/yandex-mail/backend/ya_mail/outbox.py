import base64
import hashlib
import json
import re
import smtplib
import time
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from imaplib import IMAP4
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .accounts import Problem, address, database
from .folders import allow
from .mail import Filter, Ref, Short, raw_message, reply_source, search_folder, special
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


def reply_addresses(original):
    header = original.get("Reply-To") or original.get("From")
    if not header or header.defects or not header.addresses:
        raise Problem("Проверьте адрес получателя ответа в исходном письме.")
    return [address(a.addr_spec) for a in header.addresses]


def build(client, account, letter, *, original=None):
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
        if original is None:
            original = reply_source(client, letter.reply_to_message, account)
        if not (letter.to or letter.cc or letter.bcc):
            msg["To"] = ", ".join(reply_addresses(original))
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


def draft(client, account, letter, *, original=None):
    msg = build(client, account, letter, original=original)
    refs = [r for r in (letter.reply_to_message, letter.forward_message) if r]
    return save_draft(client, account, msg, refs)


def save_draft(client, account, msg, sources, protected=False):
    folder = special(client, "drafts")
    allow(account.email, folder, client)
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
        # APPEND succeeded. Even if the follow-up lookup fails the draft is saved.
        try:
            found = search_folder(
                client, account, folder, Filter(message_id=str(msg["Message-ID"])), 2
            )
            if len(found["messages"]) == 1:
                ref = Ref(**found["messages"][0]["ref"])
        except (IMAP4.error, OSError, Problem):
            pass
    if ref:
        with database() as db:
            db.execute(
                "INSERT OR IGNORE INTO observed_uids VALUES(?,?,?,?,?)",
                (account.email, ref.folder, ref.validity, ref.uid, time.time()),
            )
            db.execute(
                "INSERT OR REPLACE INTO draft_origins VALUES (?,?,?,?)",
                (
                    account.email,
                    ref.model_dump_json(),
                    json.dumps([r.model_dump() for r in sources]),
                    int(protected),
                ),
            )
    elif protected or sources:
        raise Problem(
            "Черновик сохранён, но сервер не подтвердил его UID. Проверьте Черновики по Message-ID "
            + str(msg["Message-ID"])
        )
    return {
        "from": str(msg["From"]),
        "to": [a.addr_spec for a in msg["To"].addresses] if msg["To"] else [],
        "subject": str(msg["Subject"]),
        "folder": folder,
        "message_id": str(msg["Message-ID"]),
        "ref": ref.model_dump() if ref else None,
    }


def envelope(account, msg):
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
    return recipients


def submit(account, request, fingerprint, msg):
    """Reserve before SMTP DATA. An ambiguous delivery may never be retried automatically."""
    return submit_digest(account, request, hashlib.sha256(fingerprint).hexdigest(), msg)


def submit_digest(account, request, digest, msg):
    request = str(request)
    message_id = str(msg.get("Message-ID") or make_msgid())
    if not msg.get("Message-ID"):
        msg["Message-ID"] = message_id
    recipients = envelope(account, msg)

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


def revise(client, account, ref, letter, sources):
    from . import mail, removal

    if ref.folder != special(client, "drafts"):
        raise Problem("Для редактирования выберите письмо из Черновиков.")
    trash = special(client, "trash")
    allow(account.email, ref.folder, client)
    allow(account.email, trash, client)
    if trash == ref.folder:
        raise Problem("Выберите разные системные папки для Черновиков и Корзины.")
    key = (account.email, ref.folder, ref.validity, ref.uid)
    digest = hashlib.sha256(letter.model_dump_json().encode()).hexdigest()
    msg = build(client, account, letter)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT digest,message_id,result FROM revisions WHERE email=? AND folder=? AND validity=? AND uid=?",
            key,
        ).fetchone()
        if old:
            if old["digest"] != digest:
                raise Problem(
                    "Эта версия уже редактировалась. Получите актуальный черновик."
                )
            if old["result"]:
                return json.loads(old["result"])
            raise Problem(
                "Состояние сохранения неизвестно. Проверьте Черновики по Message-ID "
                + old["message_id"]
            )
        mail.checked_folder(client, ref, account, write=True)
        state = db.execute(
            "SELECT state FROM delivery_jobs WHERE email=? AND draft_ref=?",
            (account.email, ref.model_dump_json()),
        ).fetchone()
        if state and state[0] not in ("stopped", "failed"):
            raise Problem(
                "Этот черновик связан с очередью. Используйте edit_queued_email и номер отправки."
            )
        origin = db.execute(
            "SELECT protected FROM draft_origins WHERE ref=?", (ref.model_dump_json(),)
        ).fetchone()
        if origin and origin[0] < 0:
            raise Problem("Выберите последнюю версию черновика.")
        if ref.uid not in client.get_flags([ref.uid]):
            raise Problem("Исходный черновик перемещён. Обновите список.")
        old_msg = mail.reply_source(client, ref, account)
        if not letter.reply_to_message and not letter.forward_message:
            for header in ("In-Reply-To", "References"):
                if old_msg.get(header):
                    msg[header] = old_msg[header]
        db.execute(
            "INSERT INTO draft_origins VALUES(?,?,?,-1) ON CONFLICT(ref) DO UPDATE SET protected=-1",
            (
                account.email,
                ref.model_dump_json(),
                json.dumps([s.model_dump() for s in sources]),
            ),
        )
        db.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?,NULL)",
            (*key, digest, str(msg["Message-ID"])),
        )

    for src in (letter.reply_to_message, letter.forward_message):
        if src and src not in sources:
            sources = [*sources, src]
    result = save_draft(client, account, msg, sources, protected=2)
    result["original"] = "check_original"

    def record():
        with database() as db:
            db.execute(
                "UPDATE revisions SET result=? WHERE email=? AND folder=? AND validity=? AND uid=?",
                (json.dumps(result), *key),
            )

    record()
    try:
        mail.checked_folder(client, ref, account, write=True)
        reply = removal.move_uids(client, [ref.uid], trash)
        removal.record_move(account.email, trash, 1, reply)
    except (IMAP4.error, OSError, Problem):
        return result
    result["original"] = "moved_to_trash"
    record()
    return result
