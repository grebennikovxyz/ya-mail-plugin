"""Optional stdio adapter for the official Yandex 360 MCP (one separate personal connection)."""

import asyncio
import getpass
import json
import sys
import webbrowser
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .accounts import DATA, SERVICE, Problem, vault

URL = "https://mcp.360.yandex.net/v1/mail/mcp"
AUTHORIZE = "https://oauth.yandex.ru/authorize?client_id=355734217047470d8064b5dbbf579fee&response_type=token"
READ = frozenset(
    [
        "list_folders",
        "list_labels",
        "search_messages",
        "list_messages_in_folder",
        "list_messages_by_label",
        "get_message",
        "read_message",
    ]
)
WRITE = frozenset(
    [
        "create_draft",
        "update_draft",
        "move_messages",
        "add_message_labels",
        "remove_message_labels",
        "delete_message",
    ]
)
KEY = "official-360"
server = Server("yandex-mail-360")


def allowed(name, mode):
    suffix = name.removeprefix("mail.").removeprefix("mail_")
    return suffix in READ or (mode == "organize" and suffix in WRITE)


@asynccontextmanager
async def remote(token):
    async with httpx.AsyncClient(
        headers={"Authorization": "OAuth " + token},
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    ) as http:
        async with streamable_http_client(URL, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                yield session


async def discover(token, mode):
    async with remote(token) as session:
        result = []
        cursor = None
        while True:
            page = await session.list_tools(cursor=cursor)
            result.extend(t for t in page.tools if allowed(t.name, mode))
            cursor = page.nextCursor
            if not cursor:
                return result


def record():
    # Without an opted-in connection even discovery works on headless systems.
    if not (DATA / "native-enabled").is_file():
        return None
    value = vault().get_password(SERVICE, KEY)
    return json.loads(value) if value else None


def connect():
    if not sys.stdin.isatty():
        raise Problem("Откройте ya-mail connect-360 в своём интерактивном терминале.")
    print(
        "Шаг 1. Откройте Яндекс ID и выберите ящик для официального MCP Яндекс 360.\n",
        AUTHORIZE,
    )
    if input("Enter — открыть ссылку, минус — открыть самостоятельно: ").strip() != "-":
        webbrowser.open(AUTHORIZE)
    print(
        "Шаг 2. Проверьте имя аккаунта и запрошенные права. Разрешите доступ, скопируйте токен."
    )
    token = getpass.getpass(
        "Шаг 3. Вставьте только значение OAuth-токена (скрытый ввод): "
    ).strip()
    mode = (
        input(
            "Чтение [1] или чтение и изменение меток, черновиков, папок писем [2]: "
        ).strip()
        or "1"
    )
    if (
        mode not in ("1", "2")
        or not token
        or not token.isascii()
        or not token.isprintable()
        or " " in token
    ):
        raise Problem("Проверьте выбор режима и значение токена.")
    mode = "read" if mode == "1" else "organize"
    try:
        tools = asyncio.run(discover(token, mode))
    except Exception:
        raise Problem(
            "Яндекс 360 не подтвердил соединение. Проверьте токен и доступ вашего аккаунта."
        ) from None
    if not tools:
        raise Problem("Яндекс 360 не вернул совместимые инструменты.")
    vault().set_password(SERVICE, KEY, json.dumps([mode, token]))
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    (DATA / "native-enabled").touch()
    print(
        f"Подключён официальный сервер Яндекса. Доступных инструментов: {len(tools)}. Перезапустите чат."
    )


def disconnect():
    if record():
        vault().delete_password(SERVICE, KEY)
    (DATA / "native-enabled").unlink(missing_ok=True)


@server.list_tools()
async def list_tools():
    data = record()
    if not data:
        return []
    mode, token = data
    try:
        tools = await discover(token, mode)
    except Exception:
        raise Problem("Не удалось получить список инструментов Яндекс 360.") from None
    for tool in tools:
        read = allowed(tool.name, "read")
        tool.annotations = types.ToolAnnotations(
            readOnlyHint=read, destructiveHint=not read, openWorldHint=True
        )
    return tools


@server.call_tool()
async def call_tool(name, arguments):
    data = record()
    if not data or not allowed(name, data[0]):
        raise Problem("Эта операция не разрешена для подключения Яндекс 360.")
    try:
        async with remote(data[1]) as session:
            return await session.call_tool(name, arguments)
    except Exception:
        raise Problem(
            "Ошибка связи с Яндекс 360. Проверьте состояние операции перед повтором."
        ) from None


async def serve():
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
