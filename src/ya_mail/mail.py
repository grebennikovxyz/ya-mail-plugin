import base64
import re
from datetime import date
from email import policy
from email.parser import BytesParser
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .accounts import Problem

Short = Annotated[
    str, Field(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$")
]
Number = Annotated[int, Field(ge=1, le=4294967295)]
PageSize = Annotated[int, Field(ge=1, le=100)]
MAX_MESSAGE = 32 * 1024 * 1024
HEADER = b"BODY[HEADER.FIELDS (DATE FROM TO CC REPLY-TO SUBJECT MESSAGE-ID REFERENCES IN-REPLY-TO)]"
HEADER_PEEK = HEADER.replace(b"BODY[", b"BODY.PEEK[").decode()
PARSER = BytesParser(policy=policy.default)


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: Short
    folder: Short
    validity: Number
    uid: Number


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: Short | None = None
    recipient: Short | None = None
    subject: Short | None = None
    text: Short | None = None
    message_id: Short | None = None
    since: date | None = None
    before: date | None = None
    unread: bool | None = None
    starred: bool | None = None
    label: Short | None = None

    def criteria(self):
        terms = ["UNDELETED"]
        for key, value in (
            ("FROM", self.sender),
            ("TO", self.recipient),
            ("SUBJECT", self.subject),
            ("TEXT", self.text),
        ):
            if value:
                terms.extend([key, search_string(value)])
        if self.message_id:
            terms.extend(["HEADER", "Message-ID", search_string(self.message_id)])
        if self.since:
            terms.extend(["SINCE", self.since])
        if self.before:
            terms.extend(["BEFORE", self.before])
        if self.unread is not None:
            terms.append("UNSEEN" if self.unread else "SEEN")
        if self.starred is not None:
            terms.append("FLAGGED" if self.starred else "UNFLAGGED")
        if self.label:
            terms.extend(["KEYWORD", keyword(self.label)])
        return terms


def search_string(value):
    # IMAPClient quotes whitespace and sends non-ASCII as literals. Reject bare
    # syntax characters that its ASCII atom formatter would pass through.
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise Problem("В условии поиска есть управляющий символ.")
    if (
        value.isascii()
        and not any(c in value for c in ' "\\')
        and re.search(r"[(){}%*\]]", value)
    ):
        raise Problem("Уточните поисковую фразу без служебных символов IMAP.")
    return value


def keyword(label):
    if re.fullmatch(r"[A-Za-z0-9_.:+-]{1,96}", label) and not label.lower().startswith(
        "ym_"
    ):
        return label
    if (
        not label.strip()
        or len(label.encode()) > 100
        or any(ord(c) < 32 for c in label)
    ):
        raise Problem("Название метки должно занимать от 1 до 100 байт.")
    return "ym_" + base64.b32encode(label.encode()).decode().rstrip("=").lower()


def label_name(flag):
    name = flag.decode("ascii", "replace")
    if name.lower().startswith("ym_"):
        try:
            raw = name[3:].upper()
            return base64.b32decode(raw + "=" * (-len(raw) % 8)).decode()
        except ValueError:
            pass
    return name


def flags_view(flags):
    return {
        "flags": [f.decode("ascii", "replace") for f in flags],
        "labels": [label_name(f) for f in flags if not f.startswith((b"\\", b"$"))],
    }


def checked_folder(client, ref, account, write=False):
    if ref.account != account.email:
        raise Problem("Идентификатор письма относится к другому ящику.")
    info = client.select_folder(ref.folder, readonly=not write)
    if info[b"UIDVALIDITY"] != ref.validity:
        raise Problem(
            "Папка изменилась. Повторите поиск, чтобы получить текущие идентификаторы."
        )
    return info


def summaries(client, account, folder, validity, uids):
    if not uids:
        return []
    rows = client.fetch(uids, [HEADER_PEEK, "FLAGS", "RFC822.SIZE"])
    result = []
    for uid in uids:
        row = rows.get(uid)
        if row is None:
            continue
        # Only one header section was requested. Yandex can return its field
        # names in a different order from the FETCH command.
        headers = next(
            (v for k, v in row.items() if k.startswith(b"BODY[HEADER.FIELDS (")), None
        )
        if not isinstance(headers, bytes):
            raise Problem("IMAP-сервер не вернул запрошенные заголовки письма.")
        msg = PARSER.parsebytes(headers, headersonly=True)
        result.append(
            {
                "ref": Ref(
                    account=account.email, folder=folder, validity=validity, uid=uid
                ).model_dump(),
                **{
                    k: str(msg.get(k, ""))
                    for k in (
                        "Date",
                        "From",
                        "To",
                        "Cc",
                        "Reply-To",
                        "Subject",
                        "Message-ID",
                        "In-Reply-To",
                        "References",
                    )
                },
                **flags_view(row.get(b"FLAGS", ())),
                "bytes": row.get(b"RFC822.SIZE"),
            }
        )
    return result


def search_folder(client, account, folder, filters, limit, before_ref=None):
    info = client.select_folder(folder, readonly=True)
    validity = info[b"UIDVALIDITY"]
    criteria = filters.criteria()
    if before_ref:
        if (before_ref.folder, before_ref.account, before_ref.validity) != (
            folder,
            account.email,
            validity,
        ):
            raise Problem("Указатель страницы устарел или относится к другой папке.")
        if before_ref.uid == 1:
            return {"messages": [], "next_before": None}
        criteria.extend(["UID", f"1:{before_ref.uid - 1}"])
    uids = sorted(client.search(criteria, charset="UTF-8"), reverse=True)
    page = uids[:limit]
    return {
        "messages": summaries(client, account, folder, validity, page),
        "next_before": Ref(
            account=account.email, folder=folder, validity=validity, uid=page[-1]
        ).model_dump()
        if len(uids) > limit
        else None,
    }


def raw_message(client, ref, account):
    checked_folder(client, ref, account)
    sizes = client.fetch([ref.uid], ["RFC822.SIZE"])
    size = sizes.get(ref.uid, {}).get(b"RFC822.SIZE")
    if size is None:
        raise Problem("Письмо перемещено или удалено. Повторите поиск.")
    if size > MAX_MESSAGE:
        raise Problem(
            "Письмо больше 32 МиБ. Метаданные доступны через поиск, содержимое — в Почте."
        )
    rows = client.fetch([ref.uid], ["BODY.PEEK[]"])
    raw = rows.get(ref.uid, {}).get(b"BODY[]")
    if raw is None:
        raise Problem("Письмо перемещено или удалено. Повторите поиск.")
    return PARSER.parsebytes(raw)


def parts(msg):
    # Do not descend into attached messages when selecting downloadable parts.
    for child in msg.iter_parts():
        if (
            child.get_filename()
            or child.get_content_disposition() == "attachment"
            or child.get_content_maintype() == "message"
        ):
            yield child
        elif child.is_multipart():
            yield from parts(child)
        elif child.get_content_maintype() != "text" or child.get("Content-ID"):
            yield child


def body(msg):
    item = msg.get_body(preferencelist=("plain", "html"))
    if item is None:
        return "", "text/plain"
    raw = item.get_payload(decode=True) or b""
    try:
        content = raw.decode(item.get_content_charset() or "utf-8", "replace")
    except LookupError:
        content = raw.decode("utf-8", "replace")
    return content, item.get_content_type()


def attachments(msg):
    return [
        {"part": i, "name": part.get_filename(), "type": part.get_content_type()}
        for i, part in enumerate(parts(msg), 1)
    ]


def attachment_content(part):
    data = part.get_payload(decode=True)
    if data is not None:
        return data
    if part.get_content_type() == "message/rfc822":
        return part.get_payload(0).as_bytes(policy=policy.SMTP)
    raise Problem("Этот составной MIME-объект нельзя скачать как отдельный файл.")


def read(client, account, ref, offset, count):
    msg = raw_message(client, ref, account)
    content, mime = body(msg)
    return {
        "ref": ref.model_dump(),
        "headers": {
            k: str(msg.get(k, ""))
            for k in (
                "From",
                "To",
                "Cc",
                "Reply-To",
                "Subject",
                "Date",
                "Message-ID",
                "References",
                "In-Reply-To",
            )
        },
        "body": content[offset : offset + count],
        "body_type": mime,
        "next_offset": offset + count if offset + count < len(content) else None,
        "attachments": attachments(msg),
    }


class Changes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: bool | None = None
    starred: bool | None = None
    add_labels: list[Short] = Field(default_factory=list, max_length=30)
    remove_labels: list[Short] = Field(default_factory=list, max_length=30)

    def flags(self):
        add = {keyword(name) for name in self.add_labels}
        remove = {keyword(name) for name in self.remove_labels}
        for value, flag in ((self.read, r"\Seen"), (self.starred, r"\Flagged")):
            if value is not None:
                (add if value else remove).add(flag)
        if {f.lower() for f in add} & {f.lower() for f in remove} or not (
            add or remove
        ):
            raise Problem("Укажите непустой набор непротиворечивых изменений.")
        return sorted(add), sorted(remove)


def modify(client, account, refs, changes):
    first, *rest = refs
    if any(
        (r.account, r.folder, r.validity)
        != (first.account, first.folder, first.validity)
        for r in rest
    ):
        raise Problem(
            "Для пакетной операции выберите письма из одной папки одного ящика."
        )
    info = checked_folder(client, first, account, write=True)
    add, remove = changes.flags()
    allowed = {flag.decode().lower() for flag in info.get(b"PERMANENTFLAGS", ())}
    for flag in add + remove:
        if flag.lower() not in allowed and (
            flag.startswith("\\") or r"\*" not in allowed
        ):
            raise Problem(
                "Сервер не разрешает сохранять эту метку или флаг в выбранной папке."
            )
    uids = sorted({r.uid for r in refs})
    if set(client.get_flags(uids)) != set(uids):
        raise Problem("Часть писем уже перемещена. Обновите подборку.")
    if add:
        client.add_flags(uids, add, silent=True)
    if remove:
        client.remove_flags(uids, remove, silent=True)
    actual = client.get_flags(uids)
    changed = []
    for uid in uids:
        present = {f.decode().lower() for f in actual.get(uid, ())}
        if (
            uid not in actual
            or not all(f.lower() in present for f in add)
            or any(f.lower() in present for f in remove)
        ):
            raise Problem(
                "Сервер не подтвердил все флаги. Прочитайте их состояние перед повтором."
            )
        changed.append({"uid": uid, **flags_view(actual[uid])})
    return changed


def special(client, role):
    flag = {
        "drafts": rb"\Drafts",
        "trash": rb"\Trash",
        "archive": rb"\Archive",
        "sent": rb"\Sent",
        "spam": rb"\Junk",
    }[role]
    folder = client.find_special_folder(flag)
    if not folder and client.has_capability("XLIST"):
        folder = next(
            (
                f
                for flags, _, f in client.xlist_folders()
                if flag.lower() in {v.lower() for v in flags}
            ),
            None,
        )
    if not folder:
        raise Problem(
            "Сервер не указал системную папку. Получите список папок и выберите нужную явно."
        )
    return folder
