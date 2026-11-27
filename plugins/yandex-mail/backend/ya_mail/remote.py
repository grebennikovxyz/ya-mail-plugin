"""Single-owner Streamable HTTP behind a local HTTPS reverse proxy."""

import os
import re
import secrets
from urllib.parse import urlsplit

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import Response

from .accounts import Problem
from .server import mcp


def application(public_origin, token):
    if not token or not re.fullmatch(r"[A-Za-z0-9_\-]{43,256}", token):
        raise Problem(
            "Задайте YA_MAIL_MCP_TOKEN в окружении сервиса. Используйте случайный ключ из 43–256 символов base64url."
        )
    url = urlsplit(public_origin)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise Problem("Укажите HTTPS-адрес своего шлюза без пути, логина и параметров.")
    origin = f"https://{url.netloc}"
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[url.netloc],
        allowed_origins=[origin],
    )
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    app = mcp.streamable_http_app()
    expected = f"Bearer {token}".encode("ascii")

    async def authenticated(scope, receive, send):
        if scope["type"] == "http":
            values = Headers(scope=scope).getlist("authorization")
            if len(values) != 1 or not secrets.compare_digest(
                values[0].encode("latin-1"), expected
            ):
                response = Response(
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
                return await response(scope, receive, send)
        await app(scope, receive, send)

    return authenticated


def serve(public_origin, port):
    app = application(public_origin, os.environ.get("YA_MAIL_MCP_TOKEN"))
    from .cli import start_background

    start_background()
    uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False)
