"""Read-only discovery and last-moment checks for lazy mail processing."""

import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import batched

from . import folders, mail
from .accounts import Problem
from .routing import VisibleText

REVIEW = "На согласование"
HEADERS = "BODY.PEEK[HEADER.FIELDS (FROM TO CC BCC REPLY-TO MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT LIST-ID LIST-UNSUBSCRIBE)]"
IMPORTANT = re.compile(
    r"\b(сч[её]т(?:а|у|ом)?|заказ(?:а|у)?|договор\w*|плат[её]ж\w*|оплат\w*|доступ\w*|квитанци\w*|билет\w*|парол\w*|задолж\w*|подтверждени\w*|изменени\w* условий|invoice|receipt|password|security|payment|booking|order|deadline)\b",
    re.IGNORECASE,
)
IDS = re.compile(r"<[^<>\s]{1,450}>")
MAX_BYTES = 256 * 1024
MAX_CHARS = 14000


def years_ago(years, today=None):
    today = today or date.today()
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def permitted_sources(client, email, selected):
    items = folders.catalog(client, email)
    folders.check_blocked_roots(items)
    return [
        i["folder"]
        for i in folders.active(items, selected)
        if i["folder"].casefold() != REVIEW.casefold()
        and not ({r.lower() for r in i["roles"]} & folders.SYSTEM_ROLES)
        and i["folder"].casefold() not in folders.SYSTEM_ROOTS
    ]


def contacts(msg, names):
    result = set()
    for name in names:
        for header in msg.get_all(name, []):
            if header.defects:
                raise ValueError("Неоднозначный адрес")
            result.update(a.addr_spec.casefold() for a in header.addresses)
    return result


def identifiers(msg):
    return set(
        IDS.findall(
            " ".join(
                str(msg.get(k, "")) for k in ("Message-ID", "In-Reply-To", "References")
            )
        )
    )


def stamp(row):
    return row[b"INTERNALDATE"].timestamp()


def headers(client, ids):
    for chunk in batched(ids, 250):
        for uid, row in client.fetch(
            list(chunk), [HEADERS, "INTERNALDATE", "FLAGS", "RFC822.SIZE"]
        ).items():
            yield uid, row, mail.fetched_headers(row)


def uid_pages(client, ceiling, criteria, start=1):
    # Even ten-digit UIDs fit imaplib's one-megabyte response limit.
    for lo in range(start, ceiling, 60000):
        hi = min(lo + 59999, ceiling - 1)
        yield from client.search(["UID", f"{lo}:{hi}", *criteria])


@dataclass
class History:
    lock: threading.Lock = field(default_factory=threading.Lock)
    peers: set = field(default_factory=set)
    links: set = field(default_factory=set)
    folder: str = ""
    validity: int = 0
    next_uid: int = 0
    complete: bool = False
    malformed: bool = False

    def protection(self, client, account, rules, participants, msg):
        with self.lock:
            try:
                self.update(client, account, rules)
            except (Problem, ValueError, KeyError):
                self.complete = False
            if not self.complete:
                return "Недостаточно данных для проверки личной переписки"
            if (
                identifiers(msg) & self.links
                or (participants - {account.email}) & self.peers
            ):
                return "Переписка с ответами владельца"
        return None

    def update(self, client, account, rules, visit=None):
        self.complete = False
        if not rules.sent:
            return
        folder = folders.verified_role(client, "sent")
        if not folder:
            return
        folders.allow(account.email, folder, client)
        box = client.select_folder(folder, readonly=True)
        validity, ceiling = box[b"UIDVALIDITY"], box[b"UIDNEXT"]
        if (
            folder != self.folder
            or validity != self.validity
            or ceiling < self.next_uid
        ):
            self.peers.clear()
            self.links.clear()
            self.malformed = False
            self.folder, self.validity, self.next_uid = folder, validity, 1
        if self.next_uid < ceiling:
            ids = list(uid_pages(client, ceiling, ["UNDELETED"], self.next_uid))
            seen = set()
            for uid, row, msg in headers(client, ids):
                seen.add(uid)
                # All sent-folder copies count conservatively, including aliases.
                self.links.update(identifiers(msg))
                try:
                    self.peers.update(contacts(msg, ("To", "Cc", "Bcc")))
                except ValueError:
                    self.malformed = True
                    continue
                if visit:
                    visit(row, msg, True)
            if set(ids) != seen:
                raise Problem("История Отправленных изменилась при чтении.")
            self.next_uid = ceiling
        self.complete = not self.malformed


def discover(client, account, rules):
    stats = defaultdict(
        lambda: {
            "incoming": 0,
            "outgoing": 0,
            "days": set(),
            "threads": set(),
            "last": date.min,
        }
    )
    examples = defaultdict(lambda: [0, ""])

    def visit(row, msg, sent):
        day = row[b"INTERNALDATE"].date()
        keys = contacts(msg, ("To", "Cc", "Bcc") if sent else ("From",))
        thread = (
            IDS.findall(str(msg.get("References", "")))
            or IDS.findall(str(msg.get("In-Reply-To", "")))
            or IDS.findall(str(msg.get("Message-ID", "")))
        )
        for peer in keys - {account.email}:
            s = stats[peer]
            s["outgoing" if sent else "incoming"] += 1
            s["last"] = max(s["last"], day)
            s["days"].add(day)
            if thread:
                s["threads"].add(thread[0])
            if not sent and (msg.get("List-ID") or msg.get("List-Unsubscribe")):
                examples[peer][0] += 1
                examples[peer][1] = str(msg.get("Subject", ""))

    history = History()
    history.update(client, account, rules, visit)
    for folder in permitted_sources(client, account.email, rules.folders):
        box = client.select_folder(folder, readonly=True)
        for _, row, msg in headers(
            client, uid_pages(client, box[b"UIDNEXT"], ["UNDELETED"])
        ):
            try:
                visit(row, msg, False)
            except ValueError:
                continue
    old = [
        (peer, s)
        for peer, s in stats.items()
        if s["incoming"] and s["outgoing"] and s["last"] < years_ago(3)
    ]
    old.sort(
        key=lambda entry: (-len(entry[1]["threads"]), -len(entry[1]["days"]), entry[0])
    )
    return old, sorted(examples.items(), key=lambda e: (-e[1][0], e[0]))


def inventory(client, account, rules, limit=None):
    for folder in permitted_sources(client, account.email, rules.folders):
        box = client.select_folder(folder, readonly=True)
        cutoff = years_ago(1)
        start = None
        end = min(date(2000, 1, 1), cutoff) if limit else cutoff
        while True:
            criteria = ["UNDELETED", "UNSEEN", "BEFORE", end]
            if start:
                criteria += ["SINCE", start]
            count = 0
            for chunk in batched(uid_pages(client, box[b"UIDNEXT"], criteria), 1000):
                rows = client.fetch(list(chunk), ["INTERNALDATE"])
                pairs = [(uid, stamp(row)) for uid, row in rows.items()]
                count += len(pairs)
                yield folder, box[b"UIDVALIDITY"], pairs
            if end == cutoff or limit and count >= limit:
                break
            start, end = end, min(date(end.year + 1, 1, 1), cutoff)


@dataclass
class Decision:
    action: str
    reason: str
    subject: str = ""
    text: str = ""
    mid: str = ""


class NewsletterText(VisibleText):
    incomplete = False

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        data = dict(attrs)
        if tag in {"iframe", "object", "canvas", "svg", "video", "audio"}:
            self.incomplete = True
        if tag == "img" and (data.get("width"), data.get("height")) not in {
            ("1", "1"),
            ("0", "0"),
        }:
            self.incomplete = True


def inspect(client, account, rules, ref, history, *, with_text=True):
    if ref.folder not in permitted_sources(client, account.email, rules.folders):
        return Decision("keep", "Папка исключена из разбора")
    current_box = client.select_folder(ref.folder, readonly=True)
    if current_box[b"UIDVALIDITY"] != ref.validity:
        return Decision("stale", "UIDVALIDITY изменилась")
    entry = next(headers(client, [ref.uid]), None)
    if not entry:
        return Decision("stale", "Письмо перемещено")
    _, row, msg = entry
    subject = " ".join(str(msg.get("Subject", "")).split())[:400]
    mid = str(msg.get("Message-ID", "")).strip()

    def choice(action, reason, text=""):
        return Decision(action, reason, subject, text, mid)

    flags = {v.lower() for v in row[b"FLAGS"]}
    if flags & {rb"\seen", rb"\flagged", rb"\deleted", rb"\answered"} or row[
        b"INTERNALDATE"
    ].date() >= years_ago(1):
        return choice("keep", "Прочитано, отмечено, получило ответ или моложе порога")
    try:
        participants = contacts(msg, ("From", "To", "Cc", "Reply-To", "Bcc"))
        senders = contacts(msg, ("From",))
    except ValueError:
        return choice("keep", "Не удалось надёжно определить корреспондента")
    if participants.intersection(rules.protected):
        return choice("keep", "Защищённый корреспондент")
    if not IDS.fullmatch(mid) or len(senders) != 1:
        return choice("keep", "Недостаточно данных для проверки личной переписки")
    protection = history.protection(client, account, rules, participants, msg)
    if protection:
        return choice("keep", protection)
    sender = next(iter(senders))
    if sender in rules.review or sender not in rules.approved:
        return choice("review", "Отправитель требует согласования")
    if not (msg.get("List-ID") or msg.get("List-Unsubscribe")):
        return choice("review", "У письма нет признака рассылки")
    if IMPORTANT.search(subject):
        return choice("review", "В теме есть сведения, требующие внимания: " + subject)
    if row[b"RFC822.SIZE"] > MAX_BYTES:
        return choice("review", "Письмо превышает размер полного анализа")
    if not with_text:
        return choice("classify", "Одобренная рассылка")
    mail.checked_folder(client, ref, account)
    raw = client.fetch([ref.uid], ["BODY.PEEK[]"]).get(ref.uid, {}).get(b"BODY[]", b"")
    if not raw or len(raw) != row[b"RFC822.SIZE"]:
        return choice("review", "Содержимое получено не полностью")
    body = mail.PARSER.parsebytes(raw)
    if any(mail.parts(body)) or any(p.defects for p in body.walk()):
        return choice("review", "Есть вложение или неопределённая структура письма")
    # Consider all text alternatives: a short plain placeholder may hide an invoice in HTML.
    texts = []
    for part in body.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() not in ("text/html", "text/plain"):
            return choice("review", "Неизвестный тип содержимого")
        try:
            value = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            return choice("review", "Не удалось декодировать текст письма")
        if part.get_content_type() == "text/html":
            parser = NewsletterText()
            parser.feed(value)
            parser.close()
            if parser.incomplete:
                return choice(
                    "review",
                    "Письмо содержит графический или встроенный материал, который не проверен",
                )
            value = " ".join(parser.text)
        value = " ".join(value.split())
        if value and value not in texts:
            texts.append(value)
    value = "\n".join(texts)
    if not value or len(value) > MAX_CHARS or "\ufffd" in value:
        return choice("review", "Текст не удалось полностью проверить")
    match = IMPORTANT.search(value)
    if match:
        return choice(
            "review",
            "Обратите внимание: "
            + value[max(0, match.start() - 24) : match.end() + 85],
        )
    return choice("classify", "Требуется оценка содержания", value)
