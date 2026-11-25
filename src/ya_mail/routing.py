"""Route MIME attachments to the Disk owner and folder chosen in a task."""

import hashlib
import json
import unicodedata
from html.parser import HTMLParser
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import BaseModel, ConfigDict
from ydisk_plugin import account as disk_account
from ydisk_plugin.disk import Disk, disk_url, path_of

from . import mail
from .accounts import Problem
from .mail import Short


class CalendarDeadline(BaseModel):
    """Values supplied by the owner's task. Ya-Cal validates the time interval."""

    model_config = ConfigDict(extra="forbid")
    account: Short
    calendar_id: Short
    start: Short
    end: Short


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"style", "script", "head"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"style", "script", "head"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def normalized(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def matches(msg, phrases):
    needles = [normalized(p) for p in phrases]
    if any(not p for p in needles):
        raise Problem("Укажите непустые фразы для отбора писем.")
    if not needles:
        return True
    text, mime = mail.body(msg)
    if mime == "text/html":
        parser = VisibleText()
        parser.feed(text)
        parser.close()
        text = " ".join(parser.text)
    fields = [normalized(str(msg.get("Subject", ""))), normalized(text)]
    return any(p in field for p in needles for field in fields)


def attachment_name(part):
    # Treat both path separators as untrusted, including on POSIX.
    name = (part.get_filename() or "attachment").replace("\\", "/").split("/")[-1]
    name = "".join(
        "_" if unicodedata.category(c).startswith("C") or c in ':*?"<>|' else c
        for c in name
    ).strip(" .")
    return name.encode("utf-8")[:160].decode("utf-8", "ignore") or "attachment"


def store_files(owner_uid, folder, plans):
    try:
        with disk_account.account_lock():
            owner = disk_account.current()
            if owner.uid != owner_uid or not owner.writable:
                raise Problem("Проверьте владельца Ya-Di и разрешение на запись.")
            with httpx.Client(
                timeout=httpx.Timeout(45, connect=10), trust_env=False
            ) as http:
                disk = Disk(http, owner.token)
                if disk.meta(folder).get("type") != "dir":
                    raise Problem("Выберите существующую папку Диска.")
                for entry, part in plans:
                    data = mail.attachment_content(part)
                    digest = hashlib.sha256(data).hexdigest()
                    try:
                        item = disk.upload(entry["path"], data)
                        status = "uploaded"
                    except disk_account.Problem as exc:
                        if not str(exc).startswith("HTTP 409."):
                            raise
                        item = disk.meta(entry["path"])
                        status = "already_saved"
                    if (
                        item.get("type") != "file"
                        or (item.get("sha256") or "").lower() != digest
                    ):
                        raise Problem(
                            "Содержимое файла на Диске отличается. Проверьте указанный путь."
                        )
                    entry.update(status=status, sha256=digest)
    except disk_account.Problem as exc:
        raise Problem(str(exc)) from None
    except httpx.HTTPError:
        raise Problem(
            "Соединение с Диском прервано. Повторите операцию с теми же параметрами."
        ) from None


def route(msg, ref, owner_uid, folder, phrases, preview, deadline):
    if not folder.startswith(("/", "disk:/")):
        raise Problem("Укажите полный путь к папке на Диске.")
    folder = path_of(folder)
    matched = matches(msg, phrases)
    result = {
        "matched": matched,
        "preview": preview,
        "files": [],
        "calendar_request": None,
    }
    if not matched:
        return result
    source = uuid5(NAMESPACE_URL, "ya-ma:" + ref.model_dump_json())
    plans = []
    for number, part in enumerate(mail.parts(msg), 1):
        filename = f"{source.hex}-{number}-{attachment_name(part)}"
        path = str(PurePosixPath(folder) / filename)
        entry = {
            "part": number,
            "path": path,
            "url": disk_url(path),
            "status": "planned",
        }
        plans.append((entry, part))
        result["files"].append(entry)
    if plans and not preview:
        store_files(owner_uid, folder, plans)
    if deadline:
        # Same source and target calendar always get the same request id.
        key = json.dumps(
            [deadline.account.lower(), deadline.calendar_id], ensure_ascii=False
        )
        links = [entry["url"] for entry, _ in plans]
        result["calendar_request"] = {
            "account": deadline.account,
            "calendar_id": deadline.calendar_id,
            "request_id": str(uuid5(source, key)),
            "allow_conflicts": False,
            "event": {
                "summary": ("Расчёт · " + str(msg.get("Subject") or "Запрос"))[:512],
                "start": deadline.start,
                "end": deadline.end,
                "description": "\n".join(
                    [f"{ref.account} · {ref.folder} · UID {ref.uid}", *links]
                )[:60000],
            },
        }
    return result
