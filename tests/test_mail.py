import asyncio
import base64
import smtplib
from contextlib import nullcontext
from email import policy
from email.message import EmailMessage
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from conftest import consent, send_now
from pydantic import ValidationError

from ya_mail import accounts, mail, native, outbox, server, transport


def test_search_ids_and_transitive_threads_without_search_header(env):
    account, box = env

    def insert(folder, mid, parent=None):
        msg = EmailMessage(policy=policy.SMTP)
        msg["Message-ID"] = f"<{mid}@example.org>"
        if parent:
            msg["In-Reply-To"] = f"<{parent}@example.org>"
        msg.set_content("synthetic")
        uid = max(box.data[folder], default=0) + 1
        box.data[folder][uid] = msg.as_bytes()
        return mail.Ref(
            account=account.email, folder=folder, validity=box.validity, uid=uid
        )

    root = insert("INBOX", "root")
    box.data["Projects"] = {}
    insert("Projects", "reply", "root")
    insert("INBOX", "follow-up", "reply")
    insert("INBOX", "unrelated-root")
    duplicate = insert("INBOX", "root")
    filters = mail.Filter(message_id="root@example.org")
    page = server.search_messages(account.email, filters, "INBOX", 1)
    assert page["messages"][0]["ref"] == duplicate.model_dump()
    second = server.search_messages(
        account.email, filters, "INBOX", 1, mail.Ref(**page["next_before"])
    )
    assert (
        second["messages"][0]["ref"] == root.model_dump()
        and second["next_before"] is None
    )
    assert server.count_messages(account.email, filters)["total"] == 2
    result = server.read_thread(root)
    assert {m["Message-ID"] for m in result["messages"]} == {
        "<root@example.org>",
        "<reply@example.org>",
        "<follow-up@example.org>",
    }
    assert server.read_thread(root, limit=1)["truncated"]
    assert all("HEADER" not in call[1] for call in box.calls if call[0] == "search")


def test_header_index_resumes_and_tracks_uidvalidity(env, monkeypatch):
    account, box = env
    monkeypatch.setattr(mail, "THREAD_BATCH", 1)
    for _ in range(4):
        box.add()
    root = mail.Ref(account=account.email, folder="INBOX", validity=box.validity, uid=1)
    assert not server.read_thread(root, "INBOX")["index_complete"]
    result = server.read_thread(root, "INBOX")
    assert (
        result["index_complete"] and result["messages"][0]["ref"] == root.model_dump()
    )
    exact = mail.Filter(message_id="<1-INBOX@example.org>")
    box.calls.clear()
    assert server.count_messages(account.email, exact)["total"] == 1
    assert all(call[0] != "fetch" for call in box.calls)
    box.validity += 1
    with pytest.raises(accounts.Problem, match="Осталось проверить"):
        server.count_messages(account.email, exact)
    assert server.count_messages(account.email, exact)["total"] == 1
    with accounts.database() as db:
        assert all(
            "example.org" not in r[0]
            for r in db.execute("SELECT hashes FROM thread_headers")
        )
    accounts.forget(account.email)
    with accounts.database() as db:
        assert db.execute("SELECT count(*) FROM thread_headers").fetchone()[0] == 0


def test_profile_and_permissions(env):
    account, box = env
    accounts.save(accounts.Account(account.email, "read", "private-test"))
    assert server.list_accounts()["accounts"] == [
        {"email": account.email, "mode": "read"}
    ]
    for level in ("organize", "send"):
        with pytest.raises(accounts.Problem):
            accounts.current(account.email, level)
    assert "private-test" not in repr(accounts.current(account.email))
    assert b"private-test" not in (accounts.DATA / "mail.sqlite3").read_bytes()
    accounts.forget(account.email)
    assert accounts.profiles() == []


def test_wizard_checks_before_saving(env, monkeypatch):
    a, box = env
    monkeypatch.setattr(transport, "mailbox", lambda _: nullcontext(box))

    def fail(_):
        raise accounts.Problem("SMTP unavailable")

    monkeypatch.setattr(transport, "outgoing", fail)
    with pytest.raises(accounts.Problem):
        transport.connect(a.email, "newpass", "send")
    assert accounts.current(a.email).password == a.password
    transport.connect(a.email, "newpass", "read")
    assert accounts.current(a.email).mode == "read"
    assert not box.writable


def test_filters_and_stable_pages(env):
    a, b = env
    for _ in range(3):
        b.add()
    filters = mail.Filter(
        subject="Счёт", unread=True, since="2026-01-01", starred=False
    )
    page = server.search_messages(a.email, filters, "INBOX", 2)
    assert [m["ref"]["uid"] for m in page["messages"]] == [3, 2]
    criteria = b.calls[0]
    assert criteria[2] == "UTF-8" and criteria[1][:3] == [
        "UNDELETED",
        "SUBJECT",
        "Счёт",
    ]
    b.add()
    ref = mail.Ref(**page["next_before"])
    assert [
        m["ref"]["uid"]
        for m in server.search_messages(a.email, filters, "INBOX", 2, ref)["messages"]
    ] == [1]
    b.validity += 1
    with pytest.raises(accounts.Problem):
        server.search_messages(a.email, filters, "INBOX", 2, ref)
    with pytest.raises(ValidationError):
        mail.Filter(extra_filter="typo")


def test_read_search_all_attachment_and_stale_uid(env):
    a, b = env
    b.add()
    b.data["Projects"] = {}
    b.add("Projects")
    first = server.search_messages(a.email, mail.Filter(), limit=1)
    ref = mail.Ref(**first["messages"][0]["ref"])
    page = server.search_messages(
        a.email, mail.Filter(), limit=1, before=mail.Ref(**first["next_before"])
    )
    assert page["messages"][0]["ref"]["folder"] == "Projects"
    message = server.read_message(ref, count=3)
    assert message["body"] == "Зак" and message["next_offset"] == 3
    assert message["attachments"][0]["name"] == "Счёт.pdf"
    data = server.read_attachment(ref, 1, count=3)
    assert base64.b64decode(data["base64"]) == b"fix" and data["next_offset"] == 3
    assert not b.writable and not b.flags
    b.validity += 1
    with pytest.raises(accounts.Problem):
        server.read_message(ref)


def test_batch_flags_are_real_and_round_trip(env):
    a, b = env
    for _ in range(2):
        b.add()
    refs = [
        mail.Ref(account=a.email, folder="INBOX", validity=50, uid=i) for i in (1, 2)
    ]
    changes = mail.Changes(add_labels=["orders", "Заказ готов"], starred=True)
    results = server.modify_messages(refs, changes)
    assert set(results[0]["labels"]) == {"orders", "Заказ готов"}
    assert b"orders" in b.flags[2] and rb"\Flagged" in b.flags[1]
    assert (
        len(
            server.search_messages(a.email, mail.Filter(label="Заказ готов"), "INBOX")[
                "messages"
            ]
        )
        == 2
    )
    server.modify_messages(refs, mail.Changes(remove_labels=["orders"]))
    assert b"orders" not in b.flags[1]
    b.permanent = ()
    changed = server.modify_messages(refs, mail.Changes(add_labels=["more"]))
    assert changed[0]["local_labels"] == ["more"] and b"more" not in b.flags[1]
    assert (
        len(
            server.search_messages(a.email, mail.Filter(label="more"), "INBOX")[
                "messages"
            ]
        )
        == 2
    )


def test_move_reject_mixed_refs_before_change(env):
    a, b = env
    b.add()
    r = mail.Ref(account=a.email, folder="INBOX", validity=50, uid=1)
    with pytest.raises(accounts.Problem):
        asyncio.run(
            server.move_messages(
                [r, r.model_copy(update={"account": "other@example.org"})],
                consent(),
                role="trash",
            )
        )
    assert b.data["INBOX"]
    assert (
        asyncio.run(server.move_messages([r], consent(), role="archive"))["destination"]
        == "Archive"
    )
    assert not b.data["INBOX"] and b.data["Archive"]


def test_poll_retry_acknowledge_and_account_isolation(env):
    a, b = env
    b.add()
    assert not server.poll_new(a.email, "daily")["messages"]
    b.add()
    b.add()
    p = server.poll_new(a.email, "daily", limit=1)
    assert p["messages"][0]["ref"]["uid"] == 2 and p["has_more"]
    assert server.poll_new(a.email, "daily", limit=2) == p
    other = server.poll_new(a.email, "other", include_existing=True)
    assert not server.acknowledge_batch(a.email, "daily", other["receipt"])["advanced"]
    assert server.acknowledge_batch(a.email, "daily", p["receipt"])["advanced"]
    end = server.poll_new(a.email, "daily")
    assert end["messages"][0]["ref"]["uid"] == 3
    assert not server.acknowledge_batch(a.email, "daily", p["receipt"])["advanced"]
    b.validity = 51
    with pytest.raises(accounts.Problem):
        server.poll_new(a.email, "daily")


def test_draft_reply_forward_and_bcc_delivery(env, monkeypatch):
    a, b = env
    b.add()
    ref = mail.Ref(account=a.email, folder="INBOX", validity=50, uid=1)
    letter = outbox.Letter(
        to=["to@example.org"],
        bcc=["private@example.org"],
        subject="Ответ",
        text="Текст",
        reply_to_message=ref,
    )
    saved = server.create_draft(a.email, letter)
    draft = mail.Ref(**saved["ref"])
    msg = mail.raw_message(b, draft, a)
    assert msg["In-Reply-To"] == "<1-INBOX@example.org>"
    smtp = MagicMock(spec=smtplib.SMTP)
    smtp.send_message.side_effect = lambda msg, **kw: smtplib.SMTP.send_message(
        smtp, msg, **kw
    )
    smtp.sendmail.return_value = {}
    monkeypatch.setattr(outbox, "outgoing", lambda _: nullcontext(smtp))
    receipt = send_now(server.send_draft(draft, consent()))
    assert receipt["result"]["accepted"] == ["to@example.org", "private@example.org"]
    assert b"Bcc:" not in smtp.sendmail.call_args.args[2]
    assert (
        asyncio.run(server.send_draft(draft, consent())) == receipt
        and smtp.sendmail.call_count == 1
    )
    forward = outbox.build(
        b,
        a,
        letter.model_copy(update={"reply_to_message": None, "forward_message": ref}),
    )
    assert list(forward.iter_attachments())[0].get_content_type() == "message/rfc822"


def test_send_reservation_blocks_ambiguous_retry(env, monkeypatch):
    a, b = env
    smtp = MagicMock()
    smtp.send_message.side_effect = OSError("connection lost")
    monkeypatch.setattr(outbox, "outgoing", lambda _: nullcontext(smtp))
    letter = outbox.Letter(to=["recipient@example.org"], subject="subject", text="text")
    key = uuid4()
    result = send_now(server.send_email(a.email, key, letter, consent()))
    assert (
        result["state"] == "uncertain"
        and result["result"]["check_sent_for"] == result["message_id"]
    )
    assert asyncio.run(server.send_email(a.email, key, letter, consent())) == result
    with pytest.raises(accounts.Problem, match="ключ"):
        asyncio.run(
            server.send_email(
                a.email, key, letter.model_copy(update={"text": "other"}), consent()
            )
        )
    assert smtp.send_message.call_count == 1


def test_native_allowlist():
    assert native.allowed("mail.list_labels", "read")
    assert not native.allowed("mail.add_message_labels", "read")
    assert native.allowed("mail_add_message_labels", "organize")
    assert not native.allowed("mail.unknown_action", "organize")
