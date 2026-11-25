import asyncio
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from conftest import consent
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from ya_mail import accounts, delivery, mail, outbox, server


def enqueue(acc, ctx=None, key=None):
    letter = outbox.Letter(
        to=["ivan@example.org"],
        cc=["copy@example.org"],
        bcc=["hidden@example.org"],
        subject="Проба",
        text="Заказ",
        attachments=[outbox.Attachment(name="x.pdf", content_base64="YQ==")],
    )
    return asyncio.run(
        server.send_email(acc.email, key or uuid4(), letter, ctx or consent())
    )


def record(job):
    with accounts.database() as db:
        return db.execute("SELECT * FROM delivery_jobs WHERE id=?", (job,)).fetchone()


def test_cancel_at_29_edit_wait_and_fresh_send(env, monkeypatch):
    acc, box = env
    now = [1_800_000_000]
    monkeypatch.setattr(delivery.time, "time", lambda: now[0])
    smtp = MagicMock()
    monkeypatch.setattr(outbox, "outgoing", lambda _: nullcontext(smtp))
    key = uuid4()
    q = enqueue(acc, key=key)
    assert (
        q["from"] == acc.email
        and q["bcc"] == ["hidden@example.org"]
        and q["attachments"] == ["x.pdf"]
    )
    job = q["job_id"]
    stale = record(job)
    now[0] += 29
    draft = server.edit_queued_email(
        acc.email, job, text="Исправлено", to=["new@example.org"]
    )
    assert server.edit_queued_email(acc.email, job) == draft
    assert enqueue(acc, key=key)["state"] == "edited"
    second = server.edit_queued_email(acc.email, job, subject="Ещё одна правка")
    assert second["ref"] != draft["ref"] and second["subject"] == "Ещё одна правка"
    with pytest.raises(accounts.Problem, match="заменён"):
        asyncio.run(server.send_draft(mail.Ref(**draft["ref"]), consent()))
    draft = second
    ref = mail.Ref(**draft["ref"])
    msg = mail.raw_message(box, ref, acc)
    assert (
        "Исправлено" in msg.get_body().get_content()
        and next(msg.iter_attachments()).get_filename() == "x.pdf"
    )
    now[0] += 3600
    delivery.dispatch(stale, now[0])
    assert not smtp.send_message.called
    q2 = asyncio.run(server.send_draft(ref, consent()))
    assert record(q2["job_id"])["due"] == now[0] + 30
    now[0] += 29
    delivery.dispatch(record(q2["job_id"]), now[0])
    assert not smtp.send_message.called
    server.cancel_send(acc.email, q2["job_id"])
    assert record(q2["job_id"])["state"] == "stopped"


def test_edit_recovers_interrupted_append_without_another_copy(env, monkeypatch):
    acc, box = env
    job = enqueue(acc)["job_id"]
    append = box.append

    def interrupted(*args, **kwargs):
        append(*args, **kwargs)
        raise OSError("reply lost")

    monkeypatch.setattr(box, "append", interrupted)
    with pytest.raises(OSError):
        server.edit_queued_email(acc.email, job, subject="Вторая версия")
    assert record(job)["state"] == "saving_draft"
    recovered = server.edit_queued_email(acc.email, job, subject="Вторая версия")
    assert recovered["subject"] == "Вторая версия" and len(box.data["Drafts"]) == 1
    assert record(job)["state"] == "edited"


def test_worker_restart_sleep_and_smtp_takeover(env, monkeypatch):
    acc, _ = env
    now = [1_800_000_000]
    monkeypatch.setattr(delivery.time, "time", lambda: now[0])
    job = enqueue(acc)["job_id"]
    with accounts.database() as db:
        db.execute("UPDATE delivery_jobs SET started=1,due=0 WHERE id=?", (job,))
    due = []

    def tick(_):
        due.append(record(job)["due"])
        if len(due) == 1:
            now[0] += 600
        else:
            server.cancel_send(acc.email, job)

    monkeypatch.setattr(delivery.time, "sleep", tick)
    delivery.run(job)
    assert due == [now[0] - 600 + 30, now[0] + 30]
    job = enqueue(acc)["job_id"]
    now[0] += 30
    smtp = MagicMock()

    def accepted(*args, **kw):
        with pytest.raises(accounts.Problem, match="Отменить уже нельзя"):
            server.cancel_send(acc.email, job)
        return {}

    smtp.send_message.side_effect = accepted
    monkeypatch.setattr(outbox, "outgoing", lambda _: nullcontext(smtp))
    delivery.dispatch(record(job), now[0])
    assert record(job)["state"] == "finished" and smtp.send_message.call_count == 1


def test_interactive_recipient_guard_binds_exact_content(env):
    acc, box = env
    draft = server.create_draft(
        acc.email,
        outbox.Letter(to=["first@example.org"], subject="subject", text="body"),
    )
    ref = mail.Ref(**draft["ref"])

    async def answer(_, params):
        assert "first@example.org" in params.message and acc.email in params.message
        changed = mail.raw_message(box, ref, acc)
        changed.replace_header("To", "replacement@example.org")
        box.data[ref.folder][ref.uid] = changed.as_bytes()
        return types.ElicitResult(
            action="accept", content={"send_to_these_addresses": True}
        )

    async def check():
        async with create_connected_server_and_client_session(
            server.mcp, elicitation_callback=answer
        ) as session:
            assert (
                await session.call_tool("send_draft", {"ref": ref.model_dump()})
            ).isError

    asyncio.run(check())
    assert not server.list_outbox(acc.email)


def test_detached_sender_outlives_parent(env, tmp_path):
    # Exercise the production spawn flags, interpreter and worker loop. The child bootstrap
    # replaces only the vault and network transport. It cannot reach an actual SMTP endpoint.
    acc, _ = env
    job = enqueue(acc)["job_id"]
    fake = tmp_path / "worker.py"
    fake.write_text(
        """
import sys, smtplib
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock
from ya_mail import accounts, delivery, outbox
accounts.DATA = Path(sys.argv[1])
delivery.current = lambda email, access: accounts.Account(email, 'send', 'fixture')
smtp = Mock()
smtp.send_message.return_value = {}
outbox.outgoing = lambda a: nullcontext(smtp)
delivery.run(sys.argv[2])
""",
        encoding="utf-8",
    )
    parent = """
import sys
from ya_mail import delivery
popen = delivery.subprocess.Popen
def isolated(argv, **kw):
    return popen([argv[0], '-I', sys.argv[1], sys.argv[2], argv[-1]], **kw)
delivery.subprocess.Popen = isolated
delivery.spawn(sys.argv[3])
"""
    subprocess.run(
        [sys.executable, "-I", "-c", parent, str(fake), str(tmp_path), job],
        check=True,
        timeout=20,
    )
    assert record(job)["state"] == "queued"
    until = time.monotonic() + 50
    while time.monotonic() < until:
        row = record(job)
        if row["state"] == "finished":
            break
        time.sleep(0.3)
    assert row["state"] == "finished" and json.loads(row["result"])["accepted"] == [
        "ivan@example.org",
        "copy@example.org",
        "hidden@example.org",
    ]
