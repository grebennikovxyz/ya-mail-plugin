"""Exercise cold and offline restarts of the published OpenClaw launcher on Linux."""

import asyncio
import json
import shutil
import tarfile
import tempfile
from datetime import timedelta
from pathlib import Path

from marketplaces import PLUGIN, ROOT, sync
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(root, entry, env):
    script = entry["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(root))
    params = StdioServerParameters(
        command=shutil.which("node"), args=[str(root / script), "serve"], env=env
    )
    async with (
        stdio_client(params) as (r, w),
        ClientSession(r, w, read_timeout_seconds=timedelta(minutes=4)) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {
            "begin_mailbox_connection",
            "finish_mailbox_connection",
            "list_accounts",
        } <= names
        response = await session.call_tool("list_accounts", {})
        assert (
            not response.isError
            and json.loads(response.content[0].text)["accounts"] == []
        )


async def main():
    version = sync(ROOT)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        home = folder / "clean home"
        home.mkdir(mode=0o700)
        env = {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "PATH": "/usr/bin:/bin",
        }
        archive = ROOT / "dist" / f"yandex-mail-{version}-clawhub.tgz"
        with tarfile.open(archive) as tar:
            tar.extractall(folder, filter="data")
        bundle = folder / "package"
        entry = json.loads((bundle / ".mcp.json").read_bytes())["mcpServers"][
            "yandex-mail"
        ]
        await check(bundle, entry, env)
        private_bin = home / "data/YaMailPlugin/runtime" / version / "bin/ya-mail"
        assert private_bin.exists()
        native = ROOT / PLUGIN
        entry = json.loads((native / "openclaw.plugin.json").read_bytes())[
            "mcpServers"
        ]["yandex-mail"]
        await check(native, entry, {**env, "UV_OFFLINE": "true"})
        print(
            "Both OpenClaw manifests returned the mailbox tools with an isolated home."
        )


if __name__ == "__main__":
    asyncio.run(main())
