"""Advisory release checks. Never run installers or send mailbox data."""

import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

import httpx

from .accounts import database

RELEASES = "https://github.com/grebennikovxyz/ya-mail-plugin/releases"
LATEST = "https://api.github.com/repos/grebennikovxyz/ya-mail-plugin/releases/latest"
INTERVAL = 86400


def installed():
    try:
        return version("grebennikov-ya-mail")
    except PackageNotFoundError:
        return "unknown"


def numbers(value):
    if isinstance(value, str) and re.fullmatch(
        r"(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})", value
    ):
        return tuple(map(int, value.split(".")))
    return ()


def fetch():
    response = httpx.get(LATEST, timeout=4.0, follow_redirects=False, trust_env=False)
    response.raise_for_status()
    release = response.json()
    tag = release["tag_name"]
    if (
        release["draft"]
        or release["prerelease"]
        or not tag.startswith("v")
        or not numbers(tag[1:])
    ):
        raise ValueError("Unexpected release metadata")
    return tag[1:]


def row(db):
    db.execute("INSERT OR IGNORE INTO release_checks(id) VALUES(1)")
    return db.execute("SELECT * FROM release_checks WHERE id=1").fetchone()


def state(db):
    entry = row(db)
    current = installed()
    latest = entry["latest"]
    available = bool(numbers(current) and numbers(latest) > numbers(current))
    return {
        "installed": current,
        "latest": latest,
        "available": available,
        "automatic_check": bool(entry["enabled"]),
        "last_success": datetime.fromtimestamp(entry["success"], UTC).isoformat()
        if entry["success"]
        else None,
        "check_failed": bool(entry["failed"]),
        "release_url": f"{RELEASES}/tag/v{latest}" if latest else RELEASES,
    }


def check(*, force=False):
    now = int(time.time())
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        entry = row(db)
        if not force and (
            not entry["enabled"] or 0 <= now - entry["attempt"] < INTERVAL
        ):
            return state(db)
        db.execute("UPDATE release_checks SET attempt=? WHERE id=1", (now,))
    try:
        latest = fetch()
    except (httpx.HTTPError, ValueError, KeyError, AttributeError, TypeError):
        with database() as db:
            db.execute(
                "UPDATE release_checks SET failed=1 WHERE id=1 AND attempt=?", (now,)
            )
            return state(db)
    with database() as db:
        db.execute(
            "UPDATE release_checks SET success=?, latest=?, failed=0 WHERE id=1 AND attempt=?",
            (now, latest, now),
        )
        return state(db)


def start():
    def background():
        while True:
            try:
                check()
                with database() as db:
                    last = row(db)["attempt"]
                delay = (
                    min(INTERVAL, max(60, last + INTERVAL - time.time()))
                    if last
                    else INTERVAL
                )
            except (OSError, sqlite3.Error):
                delay = INTERVAL
            time.sleep(delay)

    threading.Thread(target=background, name="ya-mail-updates", daemon=True).start()


def configure(enabled):
    with database() as db:
        row(db)
        db.execute("UPDATE release_checks SET enabled=? WHERE id=1", (enabled,))
        return state(db)


def notice():
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        entry = row(db)
        data = state(db)
        if (
            entry["enabled"]
            and data["available"]
            and entry["notified"] != data["latest"]
        ):
            db.execute(
                "UPDATE release_checks SET notified=? WHERE id=1", (data["latest"],)
            )
            return {
                "installed": data["installed"],
                "available_version": data["latest"],
                "release_url": data["release_url"],
            }
    return None
