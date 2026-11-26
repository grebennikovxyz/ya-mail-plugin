import asyncio
import json
from datetime import date
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from conftest import consent
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from ya_mail import accounts, economy, mail, native, outbox, removal, server


def roles(box):
    special = {
        "Sent": rb"\Sent",
        "Drafts": rb"\Drafts",
        "Trash": rb"\Trash",
        "Bulk": rb"\Junk",
    }
    box.data.update({"Sent": {}, "Bulk": {}})
    box.list_folders = lambda: [
        ((special[k],) if k in special else (), b"/", k) for k in box.data
    ]

    def expunge(uids):
        for uid in uids:
            assert rb"\Deleted" in box.flags[uid]
            del box.data[box.selected][uid]

    box.uid_expunge = expunge


@pytest.mark.parametrize("action", ["decline", "arrival", "uidvalidity"])
def test_bulk_trash_button_snapshot(env, action):
    acc, box = env
    roles(box)
    refs = [
        mail.Ref(
            account=acc.email, folder="INBOX", validity=50, uid=box.add(subject=s)
        ).model_dump()
        for s in ("Счёт 1", "Счёт 2")
    ]

    async def answer(_, params):
        assert all(
            s in params.message
            for s in ["Счёт 1", "Счёт 2", "INBOX", "Trash", acc.email]
        )
        box.add(subject="Позже")
        if action == "uidvalidity":
            box.validity += 1
        return types.ElicitResult(
            action="decline" if action == "decline" else "accept",
            content={"move_listed_messages_to_trash": True},
        )

    async def check():
        async with create_connected_server_and_client_session(
            server.mcp, elicitation_callback=answer
        ) as client:
            result = await client.call_tool(
                "move_messages", {"refs": refs, "folder": "Trash"}
            )
            assert result.isError == (action != "arrival")

    asyncio.run(check())
    assert set(box.data["INBOX"]) == ({3} if action == "arrival" else {1, 2, 3})
    for name in ["mail.move_messages", "mail_delete_message"]:
        assert not native.allowed(name, "organize")


def test_cleanup_requires_actual_role_age_and_precise_expunge(env, monkeypatch):
    acc, box = env
    box.add("Trash")
    with pytest.raises(accounts.Problem, match="роль"):
        server.cleanup_folder(acc.email, "trash")
    roles(box)
    now = [1_800_000_000]
    monkeypatch.setattr(removal.time, "time", lambda: now[0])
    for folder in ["Bulk", "Drafts"]:
        box.add(folder)
    assert server.cleanup_folder(acc.email, "spam")["deleted"] == 1
    assert server.cleanup_folder(acc.email, "trash")["deleted"] == 0
    assert server.cleanup_folder(acc.email, "drafts")["deleted"] == 0
    now[0] += 7 * 86400
    assert server.cleanup_folder(acc.email, "drafts")["deleted"] == 0
    now[0] += 1
    assert server.cleanup_folder(acc.email, "drafts")["deleted"] == 1
    now[0] += 3 * 86400 - 1
    assert server.cleanup_folder(acc.email, "trash")["deleted"] == 0
    box.add("Trash")
    now[0] += 1
    assert server.cleanup_folder(acc.email, "trash")["deleted"] == 1
    assert list(box.data["Trash"]) == [2]
    box.has_capability = lambda _: False
    with pytest.raises(accounts.Problem, match="UIDPLUS"):
        server.cleanup_folder(acc.email, "trash", days=0)
    assert box.data["Trash"]


def test_cleanup_protects_pending_and_edited_drafts(env):
    acc, box = env
    roles(box)
    d = server.create_draft(
        acc.email, outbox.Letter(to=["peer@example.org"], text="текст", subject="заказ")
    )
    q = asyncio.run(server.send_draft(mail.Ref(**d["ref"]), consent()))
    assert server.cleanup_folder(acc.email, "drafts", 0)["protected"] == 1
    d2 = server.edit_queued_email(acc.email, q["job_id"], text="исправлено")
    result = server.cleanup_folder(acc.email, "drafts", 0)
    assert result["protected"] == 1 and list(box.data["Drafts"]) == [d2["ref"]["uid"]]


def message(box, acc, folder, addr, day, incoming=False):
    msg = EmailMessage(policy=policy.SMTP)
    peer = f"Иван <{addr}>"
    msg["From"], msg["To"] = (peer, acc.email) if incoming else (acc.email, peer)
    msg["Subject"] = "Заказ " + str(day)
    msg["Date"] = f"Tue, {day:02d} Nov 2026 12:00:00 +0000"
    uid = len(box.data[folder]) + 1
    msg["Message-ID"] = f"<{uid}-{folder}@example.org>"
    msg.set_content("Бумага 200 г/м²")
    box.data[folder][uid] = msg.as_bytes()
    return mail.Ref(account=acc.email, folder=folder, validity=50, uid=uid)


def test_two_ivans_header_lookup_frequency_and_trust(env):
    acc, box = env
    roles(box)
    for day, addr in [
        (1, "one@example.org"),
        (2, "one@example.org"),
        (3, "two@example.org"),
    ]:
        message(box, acc, "Sent", addr, day)
    for _ in range(2):
        hits = server.resolve_recipient(acc.email, "Ивану")
        assert hits["needs_choice"] and [
            (c["address"], c["frequency"]) for c in hits["matches"]
        ] == [("two@example.org", 1), ("one@example.org", 2)]
    assert all(
        "BODY.PEEK[]" not in entry[1] for entry in box.calls if entry[0] == "fetch"
    )
    assert any("SINCE" in e[1] for e in box.calls if e[0] == "search")
    forged = message(box, acc, "INBOX", "forged@example.org", 4, incoming=True)
    server.read_message(forged)
    c = server.resolve_recipient(acc.email, "forged@example.org")["matches"][0]
    assert not c["trusted"] and c["frequency"] == 1
    server.forget_mail_memory(acc.email, contact_address="one@example.org")
    assert not server.resolve_recipient(acc.email, "one@example.org")["matches"]
    asyncio.run(server.set_folder_rule(acc.email, "Sent", "blocked", consent()))
    assert not server.resolve_recipient(acc.email, "two@example.org")["matches"]


def test_memory_modes_revisions_citations_revocation_disconnect(env):
    acc, box = env
    box.data["Clients"] = {}
    ref = message(box, acc, "Clients", "customer@example.org", 2, incoming=True)
    args = (ref, "specification", "200 г/м²", "customer@example.org", date(2026, 11, 2))
    assert server.read_message(ref)["memory"]["mode"] == "contacts_and_facts"
    with pytest.raises(accounts.Problem, match="Цитата"):
        server.save_mail_fact(*args, "несуществующая цитата")
    first = server.save_mail_fact(*args, "Бумага 200 г/м²")
    revised = server.save_mail_fact(
        ref,
        "specification",
        "Бумага 200 г/м²",
        args[3],
        args[4],
        "Бумага 200 г/м²",
        first["id"],
    )
    assert revised["revision"] == 2
    assert server.read_message(ref)["memory"]["facts"][0]["id"] == first["id"]
    exported = server.export_mail_memory(acc.email)
    md, data = Path(exported["markdown"]), Path(exported["jsonl"])
    records = [json.loads(s) for s in data.read_text(encoding="utf-8").splitlines()]
    f = next(r for r in records if r["type"] == "fact")
    assert f["source"] == ref.model_dump() and f["history"][0]["revision"] == 1
    assert "customer@example.org" in md.read_text(encoding="utf-8")
    asyncio.run(server.set_folder_rule(acc.email, "Clients", "blocked", consent()))
    assert not server.recall_mail_memory(acc.email, "Бумага")["items"]
    assert (
        "customer@example.org" not in md.read_text(encoding="utf-8")
        and data.read_bytes() == b""
    )
    server.mail_learning(acc.email, "off")
    assert not server.read_message(
        message(box, acc, "INBOX", "third@example.org", 4, True)
    )["memory"]["facts"]
    accounts.forget(acc.email)
    assert not md.exists() and not data.exists()


def test_economy_uses_visible_catalog_and_lowest_supported_effort():
    def item(slug, priority, visibility="list"):
        return {
            "slug": slug,
            "priority": priority,
            "visibility": visibility,
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "minimal"}],
        }

    data = {
        "models": [
            item("gpt-5.6-luna", 10),
            item("large", 1),
            item("test-nano", 0, "hide"),
            item("gpt-6-luna", 7),
        ]
    }
    assert economy.select(data) == ("gpt-6-luna", "minimal")
    data["models"].pop()
    assert economy.select(data)[0] == "gpt-5.6-luna"
