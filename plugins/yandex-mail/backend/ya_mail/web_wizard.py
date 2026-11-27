"""Temporary setup form on loopback, also usable via a personal SSH tunnel."""

import hmac
import time
import secrets
import sys
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from keyring.errors import KeyringError

from . import wizard
from .accounts import MODES, Problem
from .transport import connect

STYLE = """
:root{font:17px/1.5 system-ui;color:#183044;background:#f7f8fa}
main{max-width:37rem;margin:auto;padding:.8rem 1rem 3rem} h1{line-height:1.2}
h2{font-size:1.2rem;margin-bottom:.3rem} p{margin:.3rem 0 .8rem}
label{display:block;margin:1rem 0 .25rem} input,select,button{font:inherit;box-sizing:border-box}
input,select{width:100%;padding:.55rem;border:1px solid #6e7f89;border-radius:.3rem}
a{color:#125686}button{margin-top:1rem;padding:.65rem 1rem;background:#194568;border:0;color:white;border-radius:.3rem}
"""
STEPS = "".join(
    f"<section><h2>{i}. {escape(title)}</h2><p>{escape(hint)}</p>"
    f'<a href="{url}" target="_blank" rel="noreferrer">{link}</a></section>'
    for i, ((title, hint, url), link) in enumerate(
        zip(
            wizard.STEPS, ("Настройки почтовых программ", "Пароли приложений Яндекс ID")
        ),
        1,
    )
)
OPTIONS = "".join(
    f'<option value="{mode}">{name}</option>'
    for mode, name in zip(MODES, wizard.MODENAMES)
)
FORM = (
    STEPS
    + f"""
<h2>3. Подключите ящик к агенту</h2>
<form method="post" action="/" autocomplete="off">
<label for="pair">Код мастера из вашего терминала</label><input id="pair" name="pair" required>
<label for="email">Полный адрес ящика</label><input id="email" name="email" type="email" required>
<label for="password">Пароль приложения из шага 2</label><input id="password" name="password" type="password" required>
<label for="mode">Что разрешить агенту</label><select id="mode" name="mode">{OPTIONS}</select>
<button>Проверить и подключить</button></form>"""
)


def handler(pairing_code, *, public_origin=None, path="/", email="", deadline=None):
    form = FORM.replace('action="/"', f'action="{escape(path, quote=True)}"')
    if deadline is not None:
        form = form.replace("из вашего терминала", "из вашего личного чата")

    if sys.platform == "linux":
        checkbox = (
            '<label><input style="width:auto" type="checkbox" name="local" value="yes"> '
            "Разрешить хранение с локальным ключом пользователя службы на личном Linux-сервере.</label>"
        )
        form = form.replace("<button>", checkbox + "<button>", 1)

    class Setup(BaseHTTPRequestHandler):
        timeout = 8

        def log_message(self, *args):
            pass  # Never log requests or form fields.

        @property
        def origin(self):
            return public_origin or f"http://127.0.0.1:{self.server.server_port}"

        def page(self, status, body):
            html = (
                f'<!doctype html><html lang="ru"><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>Подключение Ya-Ma</title><style>{STYLE}</style>"
                f"<main><h1>Подключение Яндекс Почты</h1>{body}</main></html>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            # A no-referrer policy also suppresses Origin on browser form posts.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def allowed(self):
            return (
                self.path == path
                and self.headers.get("Host") == urlsplit(self.origin).netloc
            )

        def do_GET(self):
            if not self.allowed():
                return self.page(404, "Страница не найдена.")
            self.page(200, form)

        def do_POST(self):
            if not self.allowed() or self.headers.get("Origin") != self.origin:
                return self.page(403, "Откройте страницу по адресу из терминала.")
            if getattr(self.server, "connected", False) or (
                deadline is not None and time.monotonic() >= deadline
            ):
                return self.page(
                    410, "Срок ссылки истёк. Запросите подключение ещё раз."
                )
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return self.page(415, "Используйте форму мастера.")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError
                fields = parse_qs(
                    self.rfile.read(size).decode(),
                    strict_parsing=True,
                    max_num_fields=5,
                )
                local = fields.pop("local", None)
                if local is not None and (sys.platform != "linux" or local != ["yes"]):
                    raise ValueError
                if set(fields) != {"pair", "email", "password", "mode"} or any(
                    len(v) != 1 for v in fields.values()
                ):
                    raise ValueError
            except (ValueError, UnicodeError):
                return self.page(400, "<p>Заполните все поля.</p>" + form)
            if not hmac.compare_digest(
                fields["pair"][0].encode(), pairing_code.encode()
            ):
                return self.page(
                    403,
                    "<p>Введите код, полученный при запуске мастера.</p>" + form,
                )
            if email and email != fields["email"][0].strip().lower():
                return self.page(400, "Адрес не совпадает с выбранным ящиком.")
            if deadline is not None and time.monotonic() >= deadline:
                return self.page(410, "Срок подключения истёк.")
            try:
                connect(
                    fields["email"][0],
                    fields["password"][0],
                    fields["mode"][0],
                    **({"local_store": True} if local else {}),
                )
            except Problem as exc:
                return self.page(400, f"<p role='alert'>{escape(str(exc))}</p>" + form)
            except KeyringError:
                return self.page(
                    503,
                    "Хранилище паролей на хосте агента недоступно. Попросите администратора открыть его, затем повторите подключение.",
                )
            self.server.connected = True
            self.page(
                200,
                "<p>Ящик подключён. Закройте эту вкладку и попросите агента показать входящие.</p>",
            )

    return Setup


def serve(port):
    pairing_code = secrets.token_urlsafe(18)
    try:
        server = HTTPServer(("127.0.0.1", port), handler(pairing_code))
    except OSError:
        raise Problem("Порт мастера занят. Выберите другой через --port.") from None
    with server:
        server.connected = False
        url = f"http://127.0.0.1:{server.server_port}/"
        print(
            f"Откройте {url}\nКод мастера: {pairing_code}\nОстановка: Ctrl+C.",
            flush=True,
        )
        while not server.connected:
            server.handle_request()
