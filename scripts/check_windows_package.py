import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


async def verify(folder):
    import tkinter

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from ya_mail.accounts import vault

    assert tkinter.Tcl().eval("info patchlevel")
    assert vault().priority > 0
    config = json.loads((folder / "mcp.json").read_bytes())
    for key, entry in config["mcpServers"].items():
        command = (folder / entry["command"]).resolve()
        assert command == Path(sys.executable).resolve()
        # Introspecting the optional 360 server would use a previously configured token.
        if key == "yandex-mail-360":
            continue
        # Exercise the packaged entry point with an empty store. A normal serve
        # invocation also resumes real pending sends for the current OS user.
        args = entry["args"]
        assert args == ["-I", "-B", "-m", "ya_mail.cli", "serve"]
        bootstrap = (
            "import sys,runpy; from pathlib import Path; import ya_mail.accounts as a; "
            "a.DATA=Path(sys.argv.pop(1)); runpy.run_module('ya_mail.cli',run_name='__main__')"
        )
        params = StdioServerParameters(
            command=str(command),
            args=["-I", "-B", "-c", bootstrap, str(folder / "qa-data"), "serve"],
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {
                "poll_new",
                "prepare_reply",
                "mailbox_overview",
                "poll_mailboxes",
                "set_folder_rule",
                "route_attachments",
                "read_message",
                "modify_messages",
                "send_email",
                "cancel_send",
                "edit_queued_email",
                "cleanup_folder",
                "resolve_recipient",
                "export_mail_memory",
            } <= names
            print(f"Standalone plugin returned {len(names)} tools.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(verify(Path(sys.argv[1])))
    else:
        root = Path(__file__).resolve().parents[1]
        version = json.loads((root / "plugins/yandex-mail/plugin.json").read_bytes())[
            "version"
        ]
        archive = root / "dist" / f"yandex-mail-{version}-windows-x64.zip"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Ya Mail QA"
            with ZipFile(archive) as z:
                assert not any(p.endswith("direct_url.json") for p in z.namelist())
                assert {
                    "connect.cmd",
                    "connect-360.cmd",
                    "runtime/LICENSE.txt",
                    "assets/ya-ma.png",
                    "NOTICE",
                } <= set(z.namelist())
                z.extractall(folder)
            subprocess.run(
                [folder / "runtime/python.exe", "-I", "-B", __file__, folder],
                check=True,
                timeout=90,
            )
