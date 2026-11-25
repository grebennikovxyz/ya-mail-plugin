import base64
import hashlib
import re
from datetime import date
from email import policy
from email.parser import BytesParser
from itertools import batched, islice
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .accounts import Problem, database

Short = Annotated[
    str, Field(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$")
]
Number = Annotated[int, Field(ge=1, le=4294967295)]
PageSize = Annotated[int, Field(ge=1, le=100)]
MAX_MESSAGE = 32 * 1024 * 1024
HEADER = b"BODY[HEADER.FIELDS (DATE FROM TO CC REPLY-TO SUBJECT MESSAGE-ID REFERENCES IN-REPLY-TO)]"
HEADER_PEEK = HEADER.replace(b"BODY[", b"BODY.PEEK[").decode()
THREAD_BATCH = 60
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


def fetched_headers(row):
    # Each FETCH requests one header section. Yandex may reorder its field names.
    raw = next(
        (v for k, v in row.items() if k.startswith(b"BODY[HEADER.FIELDS (")), None
    )
    if not isinstance(raw, bytes):
        raise Problem("IMAP-сервер не вернул запрошенные заголовки письма.")
    return PARSER.parsebytes(raw, headersonly=True)


def identifier_rows(client, uids):
    """Yandex may reject UID SEARCH HEADER. Only fetch identifier headers."""
    section = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)]"
    for batch in batched(uids, 60):
        lo, hi = min(batch), max(batch)
        # Avoid long comma lists on Yandex, without scanning large sparse ranges.
        sequence = f"{lo}:{hi}" if hi - lo < 2 * len(batch) else batch
        rows = client.fetch(sequence, [section])
        for uid in batch:
            if uid not in rows:
                continue
            headers = fetched_headers(rows[uid])
            mid = re.search(r"<[^<>\s]+>", str(headers.get("Message-ID", "")))
            parents = " ".join(
                str(v)
                for k in ("In-Reply-To", "References")
                for v in headers.get_all(k, [])
            )
            hashes = [header_hash(mid[0]) if mid else "-"]
            hashes.extend(map(header_hash, re.findall(r"<[^<>\s]+>", parents)))
            yield uid, " ".join(hashes)


def matching_uids(client, account, folder, validity, filters, extra=()):
    uids = sorted(
        client.search([*filters.criteria(), *extra], charset="UTF-8"), reverse=True
    )
    if filters.message_id:
        value = filters.message_id.strip()
        wanted = value if value.startswith("<") else f"<{value}>"
        cached, _, remaining = indexed_headers(
            client, account, folder, validity, uids, 2 * THREAD_BATCH
        )
        if remaining:
            raise Problem(
                f"Продолжается точный поиск. Осталось проверить {remaining} заголовков в папке. Повторите вызов с теми же параметрами."
            )
        digest = header_hash(wanted)
        return (uid for uid in uids if cached.get(uid, "").partition(" ")[0] == digest)
    return iter(uids)


def header_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def indexed_headers(client, account, folder, validity, uids, budget):
    key = (account.email, folder, validity)
    with database() as db:
        cached = dict(
            db.execute(
                "SELECT uid,hashes FROM thread_headers WHERE email=? AND folder=? AND validity=?",
                key,
            )
        )
    missing = [uid for uid in uids if uid not in cached]
    new = missing[:budget]
    for batch in batched(new, THREAD_BATCH):
        rows = dict(identifier_rows(client, batch))
        fresh = {uid: rows.get(uid, "") for uid in batch}
        with database() as db:
            db.executemany(
                "INSERT OR IGNORE INTO thread_headers VALUES(?,?,?,?,?)",
                [(*key, uid, hashes) for uid, hashes in fresh.items()],
            )
        cached.update(fresh)
    return cached, len(new), len(missing) - len(new)


def thread_refs(client, account, folders, roots):
    """Join replies across folders even when senders only set In-Reply-To."""
    links = {}
    locations = []
    budget = 2 * THREAD_BATCH
    complete = True
    for folder in folders:
        validity = client.select_folder(folder, readonly=True)[b"UIDVALIDITY"]
        uids = sorted(client.search(["UNDELETED"]), reverse=True)
        cached, indexed, remaining = indexed_headers(
            client, account, folder, validity, uids, budget
        )
        complete &= not remaining
        budget -= indexed
        for uid in uids:
            ids = cached.get(uid, "").split()
            if ids and ids[0] == "-":
                ids = ids[1:]
            if not ids:
                continue
            first = ids[0]
            locations.append((folder, validity, uid, first))
            for other in ids[1:]:
                links.setdefault(first, set()).add(other)
                links.setdefault(other, set()).add(first)
    related = set(map(header_hash, roots))
    pending = list(related)
    while pending:
        unseen = links.get(pending.pop(), set()) - related
        related.update(unseen)
        pending.extend(unseen)
    return (
        (
            Ref(account=account.email, folder=f, validity=v, uid=u)
            for f, v, u, mid in locations
            if mid in related
        ),
        complete,
    )


def summaries(client, account, folder, validity, uids):
    if not uids:
        return []
    rows = client.fetch(uids, [HEADER_PEEK, "FLAGS", "RFC822.SIZE"])
    result = []
    for uid in uids:
        row = rows.get(uid)
        if row is None:
            continue
        msg = fetched_headers(row)
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
    extra = []
    if before_ref:
        if (before_ref.folder, before_ref.account, before_ref.validity) != (
            folder,
            account.email,
            validity,
        ):
            raise Problem("Указатель страницы устарел или относится к другой папке.")
        if before_ref.uid == 1:
            return {"messages": [], "next_before": None}
        extra = ["UID", f"1:{before_ref.uid - 1}"]
    uids = list(
        islice(
            matching_uids(client, account, folder, validity, filters, extra), limit + 1
        )
    )
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
    if msg.get_content_disposition() == "attachment" or (
        not msg.is_multipart()
        and (msg.get_filename() or msg.get_content_maintype() != "text")
    ):
        yield msg
        return
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
