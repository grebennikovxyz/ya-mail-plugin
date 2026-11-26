"""Protocol checks with synthetic data. Production endpoints are never contacted."""

import asyncio
import json
import os
import socketserver
import sys
import threading
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from imapclient import IMAPClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ya_mail import accounts, mail

ROOT = Path(__file__).resolve().parents[1]


def test_imap_fetch_header_literal_and_unicode_search():
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "source@example.org"
    msg["Subject"] = "Счёт № 12"
    msg["Message-ID"] = "<m@example.org>"
    msg.set_content("Тест")
    raw = msg.as_bytes().split(b"\r\n\r\n")[0] + b"\r\n\r\n"
    seen = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"* OK test IMAP ready\r\n")
            while line := self.rfile.readline():
                tag, cmd = line.rstrip(b"\r\n").split(b" ", 1)
                if cmd == b"CAPABILITY":
                    self.wfile.write(b"* CAPABILITY IMAP4rev1 LITERAL+ UIDPLUS\r\n")
                elif cmd.startswith(b"LOGIN "):
                    pass
                elif cmd.startswith(b"EXAMINE "):
                    self.wfile.write(
                        b"* FLAGS (\\Seen)\r\n* 1 EXISTS\r\n* OK [UIDVALIDITY 7] valid\r\n* OK [UIDNEXT 43] next\r\n"
                    )
                elif cmd.startswith(b"UID SEARCH "):
                    assert b"{4+}" in cmd
                    seen.append(self.rfile.readline().rstrip(b"\r\n"))
                    self.wfile.write(b"* SEARCH 42\r\n")
                elif cmd.startswith(b"UID FETCH "):
                    assert b"BODY.PEEK[" in cmd
                    self.wfile.write(
                        b"* 1 FETCH (UID 42 FLAGS () RFC822.SIZE 250 "
                        + b"BODY[HEADER.FIELDS (IN-REPLY-TO REFERENCES MESSAGE-ID SUBJECT REPLY-TO CC TO FROM DATE)]"
                        + b" {"
                        + str(len(raw)).encode()
                        + b"}\r\n"
                        + raw
                        + b")\r\n"
                    )
                elif cmd == b"LOGOUT":
                    self.wfile.write(b"* BYE logout\r\n" + tag + b" OK logout\r\n")
                    return
                else:
                    self.wfile.write(tag + b" BAD unknown\r\n")
                    continue
                self.wfile.write(tag + b" OK done\r\n")

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as fixture:
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        try:
            with IMAPClient(
                "127.0.0.1", fixture.server_address[1], ssl=False, timeout=5
            ) as client:
                client.login("fixture", "fixture")
                account = accounts.Account("test@example.org", "read", "fixture")
                result = mail.search_folder(
                    client, account, "INBOX", mail.Filter(subject="ЯЯ"), 5
                )
                assert result["messages"][0]["Subject"] == "Счёт № 12"
                assert result["messages"][0]["ref"]["uid"] == 42
                assert seen == ["ЯЯ".encode()]
        finally:
            fixture.shutdown()
            thread.join()


@pytest.mark.parametrize("server_name", ["yandex-mail", "yandex-mail-360"])
def test_manifest_stdio_handshake(server_name, tmp_path):
    bundle = ROOT / "plugins/yandex-mail"
    marketplace = json.loads(
        (ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
    )
    entry = marketplace["plugins"][0]
    assert ROOT / entry["source"]["path"] == bundle
    manifest = json.loads(
        (bundle / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    config = json.loads((bundle / manifest["mcpServers"]).read_text(encoding="utf-8"))[
        "mcpServers"
    ][server_name]
    assert config["command"] == "ya-mail"
    env = {**os.environ, "PYTHONUTF8": "1"}
    # Windows platformdirs can use Known Folder APIs rather than LOCALAPPDATA.
    # Isolate the test store directly so CI never queries an actual user vault.
    bootstrap = "from pathlib import Path; import ya_mail.accounts as a; a.DATA=Path(__import__('sys').argv.pop(1)); import ya_mail.updates as u; u.start=lambda:None; from ya_mail.cli import main; main()"

    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", bootstrap, str(tmp_path), *config["args"]],
            env=env,
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            tools = (await session.list_tools()).tools
            if server_name == "yandex-mail-360":
                assert tools == []
                return
            by_name = {t.name: t for t in tools}
            assert {
                "read_message",
                "modify_messages",
                "send_draft",
                "poll_new",
                "route_attachments",
                "prepare_reply",
                "mailbox_overview",
                "poll_mailboxes",
                "set_folder_rule",
                "acknowledge_batch",
            } <= by_name.keys()
            for name in (
                "search_messages", "read_message", "read_messages", "read_thread",
                "mailbox_overview", "list_drafts", "resolve_recipient", "export_mail_memory",
            ):
                hints = by_name[name].annotations
                assert not hints.readOnlyHint
                assert not hints.destructiveHint
                assert hints.openWorldHint
            assert by_name["read_attachment"].annotations.readOnlyHint
            assert by_name["forget_mail_memory"].annotations.destructiveHint
            assert manifest["interface"]["displayName"] == "Ya-Ma"
            assert (bundle / manifest["interface"]["composerIcon"]).is_file()
            assert not by_name["send_email"].annotations.readOnlyHint
            result = await session.call_tool("list_accounts", {})
            assert (
                not result.isError
                and json.loads(result.content[0].text)["accounts"] == []
            )
            bad = await session.call_tool(
                "search_messages", {"account": "unknown@example.org", "filters": {}}
            )
            assert bad.isError

    asyncio.run(check())
