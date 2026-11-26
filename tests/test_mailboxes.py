import asyncio
import json
import sqlite3
from contextlib import nullcontext
from email import policy
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import Box, Vault, consent, send_now
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from ya_mail import accounts, mail, native, server, transport


def test_unblock_requires_client_interaction(env):
    account, _ = env
    args = {"account": account.email, "folder": "INBOX"}
    answers = iter(["decline", "accept"])

    async def answer(_, params):
        assert account.email in params.message and "INBOX" in params.message
        return types.ElicitResult(action=next(answers), content={"allow_access": True})

    async def check():
        async with create_connected_server_and_client_session(
            server.mcp, elicitation_callback=answer
        ) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools["set_folder_rule"].inputSchema["properties"]) == {
                "account",
                "folder",
                "mode",
            }
            blocked = await client.call_tool(
                "set_folder_rule", args | {"mode": "blocked"}
            )
            assert not blocked.isError
            assert (
                await client.call_tool("set_folder_rule", args | {"mode": "normal"})
            ).isError
            assert (
                await client.call_tool("search_messages", args | {"filters": {}})
            ).isError
            assert not (
                await client.call_tool("set_folder_rule", args | {"mode": "normal"})
            ).isError
            assert not (
                await client.call_tool("search_messages", args | {"filters": {}})
            ).isError

    asyncio.run(check())


def test_upgrade_preserves_pending_batch_credentials_and_send_record(
    tmp_path, monkeypatch
):
    email = "legacy@example.org"
    vault = Vault()
    vault.set_password(
        accounts.SERVICE, "old-key", json.dumps([email, "send", "fixture"])
    )
    monkeypatch.setattr(accounts, "vault", lambda: vault)
    with sqlite3.connect(tmp_path / "mail.sqlite3") as db:
        db.executescript("""
            CREATE TABLE accounts(email TEXT PRIMARY KEY, mode TEXT NOT NULL, secret TEXT NOT NULL);
            CREATE TABLE feeds(email TEXT REFERENCES accounts(email) ON DELETE CASCADE,folder TEXT,name TEXT,
                validity INTEGER NOT NULL,position INTEGER NOT NULL,batch_end INTEGER,receipt TEXT,
                PRIMARY KEY(email,folder,name));
            CREATE TABLE sends(email TEXT REFERENCES accounts(email) ON DELETE CASCADE,request TEXT,digest TEXT,
                message_id TEXT,result TEXT,PRIMARY KEY(email,request));
        """)
        db.execute("INSERT INTO accounts VALUES(?,?,?)", (email, "send", "old-key"))
        db.execute(
            "INSERT INTO feeds VALUES(?,?,?,?,?,?,?)",
            (email, "INBOX", "daily", 50, 1, 2, "old-receipt"),
        )
        db.execute(
            "INSERT INTO sends VALUES(?,?,?,?,?)",
            (email, "request", "digest", "<old@example.org>", "{}"),
        )
    box = Box()
    box.add()
    box.add()
    monkeypatch.setattr(server, "mailbox", lambda _: nullcontext(box))
    group = server.poll_mailboxes("daily", [server.MailboxScope(account=email)])[
        "mailboxes"
    ][0]
    pending = group["folders"][0]
    assert (
        pending["receipt"] == "old-receipt"
        and pending["messages"][0]["ref"]["uid"] == 2
    )
    assert server.acknowledge_batch(email, "daily", pending["receipt"])["advanced"]
    assert accounts.current(email).password == "fixture"
    with accounts.database() as db:
        assert (
            db.execute("SELECT message_id FROM sends").fetchone()[0]
            == "<old@example.org>"
        )


@pytest.fixture
def boxes(env, monkeypatch):
    first, a = env
    second = accounts.Account("other@example.org", "send", "synthetic")
    accounts.save(second)
    b = Box()
    boxes = {first.email: a, second.email: b}
    monkeypatch.setattr(
        server, "mailbox", lambda account: nullcontext(boxes[account.email])
    )
    return first, second, a, b


def rule(email, folder, mode, *, consent=None):
    ctx = SimpleNamespace(
        elicit=AsyncMock(
            return_value=SimpleNamespace(
                action="accept", data=SimpleNamespace(allow_access=consent)
            )
        )
    )
    result = asyncio.run(server.set_folder_rule(email, folder, mode, ctx))
    assert ctx.elicit.await_count == (1 if consent is not None else 0)
    return result


def test_blocked_folder_rename_and_aggregate(boxes, monkeypatch):
    first, second, box, _ = boxes
    box.data.update({"Private": {}, "All Mail": {}, "Архив": {}})
    flags = {"All Mail": (rb"\All",), "Archive": (rb"\Archive",)}
    monkeypatch.setattr(
        box, "list_folders", lambda: [(flags.get(k, ()), b"/", k) for k in box.data]
    )
    rule(first.email, "Private", "blocked")
    modes = {f["folder"]: f["mode"] for f in server.list_folders(first.email)}
    assert modes["All Mail"] == "blocked" and modes["Архив"] == "normal"
    with pytest.raises(accounts.Problem):
        server.search_messages(first.email, mail.Filter(), "All Mail")
    box.data["Renamed"] = box.data.pop("Private")
    box.add("Renamed")
    groups = server.mailbox_overview(
        scopes=[
            server.MailboxScope(account=first.email),
            server.MailboxScope(account=second.email),
        ]
    )["mailboxes"]
    assert groups[0]["error"] and "error" not in groups[1]
    with pytest.raises(accounts.Problem):
        server.search_messages(first.email, mail.Filter(), "Renamed")
    rule(first.email, "Renamed", "blocked")
    rule(first.email, "Private", "inherit", consent=True)
    assert (
        "error"
        not in server.mailbox_overview(
            scopes=[server.MailboxScope(account=first.email)]
        )["mailboxes"][0]
    )
    with pytest.raises(accounts.Problem):
        server.search_messages(first.email, mail.Filter(), "Renamed")


def test_bcc_overview_and_chosen_smtp_identity(boxes, monkeypatch):
    first, second, a, b = boxes
    a.data["Заказы"] = {}
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"], msg["To"] = "buyer@example.org", "undisclosed-recipients:;"
    msg["Reply-To"], msg["Message-ID"], msg["Subject"] = (
        "quote@example.org",
        "<quote@example.org>",
        "Запрос",
    )
    msg.set_content("Print estimate")
    for box, folder in [(a, "Заказы"), (b, "INBOX")]:
        box.data[folder][1] = msg.as_bytes()
    rule(first.email, "Заказы", "priority")
    server.set_mailbox_name(first.email, "Работа")
    groups = server.mailbox_overview()["mailboxes"]
    work = next(g for g in groups if g["account"] == first.email)
    assert work["name"] == "Работа" and work["folders"][0]["folder"] == "Заказы"
    copies = [m for g in groups for f in g["folders"] for m in f["messages"]]
    assert len(copies) == 2 and {m["ref"]["account"] for m in copies} == {
        first.email,
        second.email,
    }
    ref = mail.Ref(**work["folders"][0]["messages"][0]["ref"])
    smtp = MagicMock()
    smtp.send_message.return_value = {}
    monkeypatch.setattr(
        transport.smtplib, "SMTP_SSL", lambda *args, **kwargs: nullcontext(smtp)
    )
    for sender, box, override in [(first, a, None), (second, b, second.email)]:
        draft = server.prepare_reply(ref, "Ответ", override)
        assert (draft["from"], draft["to"], draft["subject"]) == (
            sender.email,
            ["quote@example.org"],
            "Re: Запрос",
        )
        assert (
            draft["ref"]["account"] == sender.email
            and draft["ref"]["folder"] == "Drafts"
        )
        stored = mail.PARSER.parsebytes(box.data["Drafts"][draft["ref"]["uid"]])
        assert (
            stored["In-Reply-To"] == msg["Message-ID"]
            and str(stored["References"]) == msg["Message-ID"]
        )
        send_now(server.send_draft(mail.Ref(**draft["ref"]), consent()))
        smtp.login.assert_called_with(sender.login, sender.password)
        assert smtp.send_message.call_args.kwargs["from_addr"] == sender.email
    accounts.save(accounts.Account(second.email, "read", "synthetic"))
    with pytest.raises(accounts.Problem):
        server.prepare_reply(ref, "Ответ", second.email)
    assert len(b.data["Drafts"]) == 1 and smtp.login.call_count == 2


def test_folder_rules_cover_subtrees_old_refs_and_native(boxes, monkeypatch):
    first, second, a, b = boxes
    for box in (a, b):
        box.data.update({"Отдел": {}, "Отдел.Личное": {}, "Отдел.Другое": {}})
        box.add("Отдел.Личное")
        monkeypatch.setattr(
            box, "list_folders", lambda box=box: [((), b".", k) for k in box.data]
        )
    name = "Отдел.Личное"
    ref = mail.Ref(account=first.email, folder=name, uid=1, validity=50)
    rule(first.email, "Отдел", "priority")
    rule(first.email, name, "skip")
    modes = {f["folder"]: f["mode"] for f in server.list_folders(first.email)}
    assert (
        modes[name] == "skip"
        and modes["Отдел.Другое"] == "priority"
        and modes["Trash"] == "skip"
    )
    assert not server.search_messages(first.email, mail.Filter())["messages"]
    assert server.search_messages(first.email, mail.Filter(), name)["messages"]
    with pytest.raises(accounts.Problem):
        server.poll_new(first.email, "job", name)
    rule(first.email, "Отдел", "blocked")
    monkeypatch.setattr(native, "record", lambda: ["organize", "synthetic"])
    assert asyncio.run(native.list_tools()) == []
    with pytest.raises(accounts.Problem, match="360"):
        asyncio.run(native.call_tool("mail.read_message", {"id": "1"}))
    assert (
        next(f for f in server.list_folders(first.email) if f["folder"] == name)["mode"]
        == "blocked"
    )
    a.calls.clear()
    for action in [
        lambda: server.read_message(ref),
        lambda: server.read_thread(ref),
        lambda: server.count_messages(first.email, mail.Filter(), name),
        lambda: server.search_messages(first.email, mail.Filter(), name),
        lambda: server.read_attachment(ref, 1),
        lambda: server.list_labels(first.email, name),
        lambda: server.route_attachments(ref, "42", "/", [], False),
        lambda: server.prepare_reply(ref, "reply", second.email),
        lambda: server.modify_messages([ref], mail.Changes(read=True)),
        lambda: asyncio.run(server.move_messages([ref], consent(), folder="INBOX")),
        lambda: asyncio.run(
            server.move_messages(
                [ref.model_copy(update={"folder": "INBOX"})], consent(), folder=name
            )
        ),
        lambda: server.create_folder(first.email, "Отдел.Ещё"),
    ]:
        with pytest.raises(accounts.Problem, match="запрещён"):
            action()
    assert not a.calls and b.data["Drafts"] == {}
    assert (
        server.read_message(ref.model_copy(update={"account": second.email}))["ref"][
            "account"
        ]
        == second.email
    )
    with pytest.raises(accounts.Problem, match="сохранён"):
        rule(first.email, "Отдел", "normal", consent=False)
    rule(first.email, "Отдел", "normal", consent=True)
    assert server.read_message(ref)["ref"] == ref.model_dump()


def test_scoped_poll_preserves_existing_cursor_and_survives_account_failure(
    boxes, monkeypatch
):
    first, second, a, b = boxes
    feed = "orders"
    a.add()
    assert server.poll_new(first.email, feed)["start_after_uid"] == 1
    a.add()
    a.data["Заказы"] = {}
    rule(first.email, "Заказы", "priority")
    scopes = [
        server.MailboxScope(account=second.email, folders=["INBOX", "Trash"]),
        server.MailboxScope(account=first.email),
    ]
    initial = server.poll_mailboxes(feed, scopes)
    assert initial["mailboxes"][0]["excluded"] == ["Trash"]
    rows = initial["mailboxes"][1]["folders"]
    assert [r["folder"] for r in rows] == ["Заказы", "INBOX"]
    receipt = rows[1]["receipt"]
    assert [m["ref"]["uid"] for m in rows[1]["messages"]] == [2]
    b.add()
    a.data["Авто"] = {}
    a.add("Авто")
    a.add("Заказы")

    def offline():
        raise accounts.Problem("недоступен")

    with monkeypatch.context() as m:
        m.setattr(b, "list_folders", offline)
        group = server.poll_mailboxes(feed, scopes, limit_per_folder=1)["mailboxes"]
        assert group[0]["error"] == "недоступен"
        rows = group[1]["folders"]
        assert rows[0]["folder"] == "Заказы" and all(
            len(r["messages"]) == 1 for r in rows
        )
        assert next(r for r in rows if r["folder"] == "INBOX")["receipt"] == receipt
        for r in rows:
            assert server.acknowledge_batch(
                first.email, feed, r["receipt"], r["folder"]
            )["advanced"]
    recovered = server.poll_mailboxes(feed, scopes)["mailboxes"]
    assert len(recovered[0]["folders"][0]["messages"]) == 1
    assert all(not r["messages"] for r in recovered[1]["folders"])
