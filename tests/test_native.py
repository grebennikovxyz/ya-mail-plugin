import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import types

from ya_mail import native
from ya_mail.accounts import Problem


def test_official_discovery_filters_modes_and_unknown_tools(monkeypatch):
    page1 = types.Tool(name="mail.list_labels", inputSchema={"type": "object"})
    page2 = types.Tool(name="mail.add_message_labels", inputSchema={"type": "object"})
    unknown = types.Tool(name="mail.future_action", inputSchema={"type": "object"})
    calls = []

    class Session:
        async def list_tools(self, cursor=None):
            if cursor:
                return SimpleNamespace(tools=[page2, unknown], nextCursor=None)
            return SimpleNamespace(tools=[page1], nextCursor="page2")

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="ok")]
            )

    @asynccontextmanager
    async def remote(token):
        assert token == "fixture"
        yield Session()

    monkeypatch.setattr(native, "remote", remote)
    monkeypatch.setattr(native, "record", lambda: ["read", "fixture"])
    assert [t.name for t in asyncio.run(native.list_tools())] == ["mail.list_labels"]
    with pytest.raises(Problem):
        asyncio.run(native.call_tool("mail.add_message_labels", {}))
    assert not calls
    monkeypatch.setattr(native, "record", lambda: ["organize", "fixture"])
    tools = asyncio.run(native.list_tools())
    assert [t.annotations.readOnlyHint for t in tools] == [True, False]
    assert (
        asyncio.run(native.call_tool(page2.name, {"ids": ["1"]})).content[0].text
        == "ok"
    )
    assert calls == [(page2.name, {"ids": ["1"]})]
