"""Short-lived private mailbox setup, independent of the MCP client's lifetime."""

import json
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from . import accounts
from .accounts import Problem, address
from .web_wizard import handler

PATH = "/ya-ma/connect/"
TTL = 600
PORT = 8776


def origin(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or not url.netloc.isascii()
        or any(c.isspace() or c in '\\"' for c in value)
    ):
        raise Problem("Укажите HTTPS-адрес своего мастера без пути и параметров.")
    try:
        _ = url.port
    except ValueError:
        raise Problem("Проверьте номер порта в HTTPS-адресе.") from None
    return "https://" + url.netloc


def configure(value):
    value = origin(value)
    accounts.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)

    with NamedTemporaryFile(
        dir=accounts.DATA, delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write(value)
        tmp = Path(f.name)
    try:
        tmp.replace(accounts.DATA / "setup-origin")
    finally:
        tmp.unlink(missing_ok=True)


def begin(account, same_device):
    account = address(account).lower() if account else ""
    config = accounts.DATA / "setup-origin"
    public = (
        origin(config.read_text(encoding="utf-8").strip()) if config.exists() else ""
    )
    if not public and not same_device:
        from .pairing import challenge

        if not account:
            raise Problem(
                "Уточните полный адрес ящика для индивидуальной ссылки подключения."
            )
        return challenge(account)
    code = secrets.token_urlsafe(20)
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "ya_mail.chat_setup"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **(
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        ),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with process.stdin:
                process.stdin.write(
                    json.dumps([code, account, public]).encode() + b"\n"
                )
            ready = pool.submit(process.stdout.readline).result(timeout=12)
            if ready.rstrip(b"\r\n") != b"ready":
                raise Problem(
                    "Мастер не запущен. Завершите действующую форму на порту 8776 или повторите через 10 минут."
                )
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
    return {
        "url": (public or f"http://127.0.0.1:{PORT}") + PATH,
        "pairing_code": code,
        "expires_in_seconds": TTL,
        "account": account,
        "after_submit": "list_accounts",
    }


def worker():
    code, account, public = json.loads(sys.stdin.buffer.readline(2048))
    end = time.monotonic() + TTL
    with HTTPServer(
        ("127.0.0.1", PORT),
        handler(
            code, public_origin=public or None, path=PATH, email=account, deadline=end
        ),
    ) as server:
        server.connected = False
        print("ready", flush=True)
        sys.stdout.close()
        while not server.connected and (remaining := end - time.monotonic()) > 0:
            server.timeout = min(remaining, 2)
            server.handle_request()


if __name__ == "__main__":
    worker()
