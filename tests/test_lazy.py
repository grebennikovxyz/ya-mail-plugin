import asyncio
import json
import threading
from contextlib import nullcontext
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage

import pytest
from conftest import Box

from ya_mail import lazy_codex as codex
from ya_mail import lazy_mail as lm
from ya_mail import lazy_store as store
from ya_mail import lazy_worker as worker

NOW = datetime.now(UTC)
OLD = NOW.replace(year=NOW.year - 10, day=1)


class Inbox(Box):
    def __init__(self):
        super().__init__()
        self.data.update({"Sent": {}, lm.REVIEW: {}})
        self.dates = {}
        self.seen_bodies = []
        self.moves = []

    def message(
        self,
        uid,
        *,
        folder="INBOX",
        sender="shop@example.org",
        to="qa@example.org",
        subject="Offers",
        body="Our seasonal catalogue",
        refs=None,
        flag=None,
        when=OLD,
    ):
        msg = EmailMessage(policy=policy.SMTP)
        for key, value in {
            "From": sender,
            "To": to,
            "Subject": subject,
            "Message-ID": f"<{folder}-{uid}@fixture.org>",
            "List-ID": "weekly.fixture.org",
        }.items():
            msg[key] = value
        if refs:
            msg["References"] = refs
        msg.set_content(body)
        self.data[folder][uid] = msg.as_bytes()
        self.dates[folder, uid] = when
        if flag:
            self.flags[uid] = {flag}

    def find_special_folder(self, flag):
        return "Sent" if flag == rb"\Sent" else super().find_special_folder(flag)

    def list_folders(self):
        roles = {"Sent": rb"\Sent", "Trash": rb"\Trash"}
        return [
            ((roles[name],) if name in roles else (), b"/", name) for name in self.data
        ]

    def folder_exists(self, name):
        return name in self.data

    def folder_status(self, name, fields):
        return {
            b"UIDNEXT": max(self.data[name], default=0) + 1,
            b"UIDVALIDITY": self.validity,
        }

    def fetch(self, ids, items):
        if "BODY.PEEK[]" in items:
            self.seen_bodies.extend(ids)
        rows = super().fetch(ids, items)
        for uid, r in rows.items():
            r[b"INTERNALDATE"] = self.dates.get((self.selected, uid), OLD)
        return rows

    def move(self, ids, dest):
        self.moves.extend((self.selected, uid, dest) for uid in ids)
        super().move(ids, dest)


@pytest.fixture
def inbox(env, monkeypatch):
    acc, _ = env
    box = Inbox()
    monkeypatch.setattr(worker, "mailbox", lambda _: nullcontext(box))
    # A real connection has its own selected folder. These synchronous fixture sessions
    # must not interleave SELECT/FETCH from separate worker threads.
    guard = threading.RLock()
    from contextlib import contextmanager

    @contextmanager
    def connection(_):
        with guard:
            yield box

    monkeypatch.setattr(worker, "mailbox", connection)
    rules = store.Rules(
        sent=True, approved=["shop@example.org"], protected=["friend@example.org"]
    )
    store.save(acc.email, rules)
    return acc, box, rules


def test_personal_invoice_advert_and_digest(inbox):
    acc, box, rules = inbox
    box.message(1, sender="friend@example.org")
    box.message(2)
    box.message(3, body="Ваш счёт на оплату доступен")
    box.message(4, sender="colleague@example.org")
    box.message(
        5,
        folder="Sent",
        sender=acc.email,
        to="colleague@example.org",
        refs="<INBOX-4@fixture.org>",
    )
    box.message(6, when=NOW)
    box.message(7, flag=b"\\Flagged")
    classified = []

    async def classify(items):
        classified.extend(item.mid for item in items)
        return {
            "decisions": [
                {"id": i, "kind": "advertisement", "reason": "Только каталог скидок"}
                for i in range(len(items))
            ]
        }

    r = asyncio.run(worker.run({acc.email: rules}, classify))
    assert classified == ["<INBOX-2@fixture.org>"]
    assert box.moves == [("INBOX", 3, lm.REVIEW)]
    assert worker.digest(acc.email, r["run"])["counts"]["awaiting_trash"] == 1
    from conftest import consent

    assert (
        asyncio.run(worker.confirm_trash(acc.email, r["run"], 25, consent()))["moved"]
        == 1
    )
    assert set(box.moves) == {("INBOX", 2, "Trash"), ("INBOX", 3, lm.REVIEW)}
    assert set(box.seen_bodies) == {2, 3}
    assert r["claimed"] >= 4
    report = worker.digest(acc.email, r["run"])
    assert report["attention"][0]["original_uid"] == 3
    assert report["counts"]["trash"] == report["counts"]["review"] == 1
    assert "qa@example.org" in store.skill_path(acc.email).read_text("utf-8")


def test_all_three_cancel_and_resume_without_second_move(inbox):
    acc, box, rules = inbox
    for uid in [1, 2, 3]:
        box.message(uid)

    async def scenario():
        allowed = True
        started = set()
        cancelled = set()

        async def classify(items):
            nonlocal allowed
            started.add(items[0].mid)
            if len(started) == 3:
                allowed = False
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.add(items[0].mid)

        report = await asyncio.wait_for(
            worker.run({acc.email: rules}, classify, guard=lambda: allowed), 4
        )
        assert "Пауза" in report["reason"]
        assert len(started) == len(cancelled) == 3
        assert not box.moves

        async def approve(items):
            return {
                "decisions": [
                    {"id": i, "kind": "advertisement", "reason": "Каталог"}
                    for i in range(len(items))
                ]
            }

        await worker.run({acc.email: rules}, approve)
        await worker.run({acc.email: rules}, approve)
        assert not box.moves
        with store.db() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM lazy_queue WHERE state='awaiting_trash'"
                ).fetchone()[0]
                == 3
            )

    asyncio.run(scenario())


def test_digest_pages_are_bounded_and_do_not_leak_blocked_folders(inbox):
    acc, _, _ = inbox
    run = store.start_run([acc.email])
    with store.db() as db:
        db.executemany(
            "INSERT INTO lazy_results VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    run,
                    acc.email,
                    "Private" if uid == 1 else "INBOX",
                    5,
                    uid,
                    uid,
                    "review",
                    str(uid),
                    "Отправитель требует согласования" if uid < 30 else "Счёт",
                    lm.REVIEW,
                )
                for uid in range(1, 31)
            ),
        )
    first = store.digest(acc.email, {"INBOX", lm.REVIEW}, run, 0, 20)
    assert first["counts"] == {"review": 29}
    assert len(first["attention"]) == 20 and first["next_offset"] == 20
    assert first["attention"][0]["subject"] == "30"
    second = store.digest(
        acc.email, {"INBOX", lm.REVIEW}, run, first["next_offset"], 20
    )
    assert len(second["attention"]) == 9 and second["next_offset"] is None


def test_dormant_contacts_rank_two_way_and_latest(inbox):
    acc, box, rules = inbox
    for uid, peer in [
        (1, "first@example.org"),
        (2, "first@example.org"),
        (3, "second@example.org"),
        (4, "recent@example.org"),
        (5, "only-news@example.org"),
    ]:
        box.message(uid, sender=peer)
    for uid, peer in [
        (10, "first@example.org"),
        (11, "second@example.org"),
        (12, "recent@example.org"),
    ]:
        box.message(
            uid,
            folder="Sent",
            sender=acc.email,
            to=peer,
            when=NOW if uid == 12 else OLD,
        )
    old, examples = lm.discover(box, acc, rules)
    assert [k for k, v in old] == ["first@example.org", "second@example.org"]
    assert dict(examples)["first@example.org"][0] == 2
    assert not box.seen_bodies


def test_ambiguous_move_reconciles_without_resending(inbox, monkeypatch):
    acc, box, rules = inbox
    box.message(10, sender="unapproved@example.org")
    move = box.move

    def lost_response(uids, dest):
        move(uids, dest)
        raise OSError("simulated lost response")

    monkeypatch.setattr(box, "move", lost_response)

    async def no_model(_):
        pytest.fail("unapproved sender went to the model")

    with pytest.raises(OSError):
        asyncio.run(worker.run({acc.email: rules}, no_model))
    with store.db() as con:
        row = con.execute("SELECT run,state FROM lazy_queue").fetchone()
        assert row["state"] == "moving_review"
    asyncio.run(worker.run({acc.email: rules}, no_model))
    assert len(box.moves) == 1
    assert worker.digest(acc.email, row["run"])["counts"] == {"review": 1}


def test_recheck_flags_after_classification_and_budget(inbox):
    acc, box, rules = inbox
    box.message(1)
    called = 0

    async def star_while_classifying(items):
        nonlocal called
        called += 1
        box.flags[1] = {rb"\Flagged"}
        return {"decisions": [{"id": 0, "kind": "advertisement", "reason": "Sale"}]}

    result = asyncio.run(
        worker.run({acc.email: rules}, star_while_classifying, token_budget=512)
    )
    assert "бюджет" in result["reason"]
    assert called == 0 and not box.moves
    asyncio.run(
        worker.run({acc.email: rules}, star_while_classifying, token_budget=5000)
    )
    assert called == 1 and not box.moves


def test_batched_decisions_reject_missing_or_duplicate_ids():
    ad = {"id": 1, "kind": "advertisement", "reason": "Каталог"}
    info = {"id": 0, "kind": "important", "reason": "Доставка"}
    assert [x[0] for x in codex.verdicts({"decisions": [ad, info]}, 2)] == [
        "review",
        "trash",
    ]
    assert all(x[0] == "review" for x in codex.verdicts({"decisions": [ad, ad]}, 2))
    assert all(x[0] == "review" for x in codex.verdicts({"decisions": [ad]}, 2))


def test_absolute_window_survives_restart_and_week_limit():
    now = 1800000000
    short = {"windowDurationMins": 300, "usedPercent": 60, "resetsAt": now + 1800}
    week = {"windowDurationMins": 10080, "usedPercent": 32, "resetsAt": now + 86400}
    data = {"accountId": "owner", "rateLimits": {"primary": short, "secondary": week}}
    assert codex.capacity(data, 1, now) == 3
    short["usedPercent"] = 94
    assert codex.capacity(json.loads(json.dumps(data)), 1, now + 50) == 1
    short["usedPercent"] = 96
    assert codex.capacity(data, 1, now) == 0
    short["usedPercent"] = 60
    week["usedPercent"] = 33
    assert codex.capacity(data, 1, now) == 0
    assert codex.capacity(data, 1, short["resetsAt"] + 1) == 0
    assert codex.capacity({"rateLimits": data["rateLimits"]}, 1, now) == 0


def test_unknown_history_and_oldest_order(inbox):
    acc, box, rules = inbox
    box.message(9, when=OLD)
    box.message(1, when=OLD.replace(year=OLD.year + 1))
    rules.sent = False
    rows = worker.preview(acc.email, rules, 2)
    assert [x["ref"]["uid"] for x in rows] == [9, 1]
    assert all(x["decision"] == "keep" for x in rows)
    assert not box.seen_bodies and not box.moves


def test_activity_requires_observed_anchor_and_quiet_period(monkeypatch):
    proxy = codex.Proxy()
    state = {"type": "idle"}

    async def pages(_):
        return ["real"]

    async def rpc(method, params):
        if method == "thread/read":
            return {"thread": {"status": state, "updatedAt": 1}}
        return {}

    monkeypatch.setattr(proxy, "pages", pages)
    monkeypatch.setattr(proxy, "rpc", rpc)
    monkeypatch.setattr(codex, "capacity", lambda *args: 3)

    async def scenario():
        await proxy.sample({"unknown"}, 1)
        assert not proxy.allowed()
        await proxy.sample({"real"}, 1)
        assert not proxy.allowed()
        proxy.last_activity -= 901
        await proxy.sample({"real"}, 1)
        assert proxy.allowed()
        state["type"] = "active"
        await proxy.sample({"real"}, 1)
        assert not proxy.allowed()

    asyncio.run(scenario())


def test_approval_rechecks_star_and_binds_page_after_click(inbox):
    from types import SimpleNamespace

    acc, box, rules = inbox
    box.message(1)
    box.message(2)

    async def advertisement(items):
        return {
            "decisions": [
                {"id": i, "kind": "advertisement", "reason": "Sale"}
                for i in range(len(items))
            ]
        }

    run = asyncio.run(worker.run({acc.email: rules}, advertisement))["run"]
    assert not box.moves

    async def click(prompt, schema):
        assert (
            acc.email in prompt and "Offers" in prompt and "shop@example.org" in prompt
        )
        box.flags[1] = {rb"\Flagged"}
        box.message(3)
        return SimpleNamespace(
            action="accept", data=schema(move_listed_messages_to_trash=True)
        )

    async def decline(prompt, schema):
        return SimpleNamespace(action="decline", data=None)

    with pytest.raises(worker.Problem, match="Письма сохранены"):
        asyncio.run(
            worker.confirm_trash(acc.email, run, 100, SimpleNamespace(elicit=decline))
        )
    assert not box.moves
    result = asyncio.run(
        worker.confirm_trash(acc.email, run, 100, SimpleNamespace(elicit=click))
    )
    assert result["moved"] == 1 and box.moves == [("INBOX", 2, "Trash")]
    assert (
        asyncio.run(
            worker.confirm_trash(acc.email, run, 100, SimpleNamespace(elicit=click))
        )["moved"]
        == 0
    )


def test_manual_mcp_sampling_proposes_instead_of_moving(inbox):
    from types import SimpleNamespace

    from ya_mail import server

    acc, box, _ = inbox
    box.message(1)

    async def respond(messages, **options):
        assert options["include_context"] == "none"
        return SimpleNamespace(
            content=SimpleNamespace(
                text=json.dumps(
                    {
                        "decisions": [
                            {"id": 0, "kind": "advertisement", "reason": "Sale"}
                        ]
                    }
                )
            )
        )

    session = SimpleNamespace(
        check_client_capability=lambda _: True, create_message=respond
    )
    result = asyncio.run(
        server.lazy_run(acc.email, 1, 12000, SimpleNamespace(session=session))
    )
    assert worker.digest(acc.email, result["run"])["counts"] == {"awaiting_trash": 1}
    assert not box.moves
