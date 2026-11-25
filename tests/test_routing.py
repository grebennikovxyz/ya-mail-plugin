import hashlib
from contextlib import nullcontext
from email import policy
from email.message import EmailMessage
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from ya_mail import accounts, mail, routing, server


@pytest.fixture
def quote(env):
    account, box = env
    msg = EmailMessage(policy=policy.SMTP)
    msg["Subject"] = "Печать каталога"
    msg.set_content(
        "<head><style>only-in-css</style></head><p>Нужен РАСЧЁТ цены</p>",
        subtype="html",
    )
    for raw in (b"pdf-one", b"pdf-two"):
        msg.add_attachment(
            raw, maintype="application", subtype="pdf", filename="../outside/макет.pdf"
        )
    box.data["INBOX"][1] = msg.as_bytes()
    accounts.save(accounts.Account(account.email, "read", "test"))
    ref = mail.Ref(account=account.email, folder="INBOX", validity=box.validity, uid=1)
    deadline = routing.CalendarDeadline(
        account="calendar@example.org",
        calendar_id="work",
        start="2030-07-03T15:00:00+03:00",
        end="2030-07-03T15:30:00+03:00",
    )
    return ref, deadline


@pytest.fixture
def disk(monkeypatch):
    owner = SimpleNamespace(uid="42", writable=True, token="synthetic")
    monkeypatch.setattr(routing.disk_account, "current", lambda: owner)
    monkeypatch.setattr(routing.disk_account, "account_lock", nullcontext)
    files, transfers = {}, {}

    def handle(req):
        path = req.url.params.get("path")
        if req.url.host == "upload.yandex.net":
            assert "authorization" not in req.headers
            destination = transfers[req.url.path]
            assert destination not in files
            files[destination] = req.content
            return httpx.Response(201)
        assert req.url.host == "cloud-api.yandex.net"
        assert req.headers["Authorization"] == "OAuth synthetic"
        if req.url.path.endswith("/upload"):
            assert req.url.params["overwrite"] == "false"
            if path in files:
                return httpx.Response(409)
            ticket = "/" + str(len(transfers))
            transfers[ticket] = path
            return httpx.Response(
                200, json={"href": "https://upload.yandex.net" + ticket}
            )
        if path == "/В работу":
            return httpx.Response(200, json={"type": "dir"})
        if path in files:
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha256": hashlib.sha256(files[path]).hexdigest(),
                },
            )
        return httpx.Response(404)

    client = httpx.Client
    monkeypatch.setattr(
        routing.httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(handle), **kw),
    )
    return owner, files


def test_quote_routing_preview_retry_and_deadline(quote, disk, env):
    ref, deadline = quote
    owner, files = disk
    args = (ref, owner.uid, "/В работу", ["расчет цены"])
    plan = server.route_attachments(*args, deadline=deadline)
    assert plan["matched"] and plan["preview"] and not files
    assert not server.route_attachments(
        ref, owner.uid, "/В работу", ["only-in-css"], False
    )["matched"]
    saved = server.route_attachments(*args, preview=False, deadline=deadline)
    assert {f["status"] for f in saved["files"]} == {"uploaded"}
    assert all(
        PurePosixPath(path).parent == PurePosixPath("/В работу") for path in files
    )
    assert list(files.values()) == [b"pdf-one", b"pdf-two"]
    assert not env[1].writable and not env[1].flags
    # Simulate partial completion before the feed receipt was acknowledged.
    del files[saved["files"][1]["path"]]
    again = server.route_attachments(*args, preview=False, deadline=deadline)
    assert [f["status"] for f in again["files"]] == ["already_saved", "uploaded"]
    request = again["calendar_request"]
    assert plan["calendar_request"] == saved["calendar_request"] == request
    assert UUID(request["request_id"]).version == 5 and not request["allow_conflicts"]
    assert request["account"] == deadline.account and request["calendar_id"] == "work"
    assert (
        request["event"]["start"] == deadline.start
        and "attendees" not in request["event"]
    )
    assert all(f["url"] in request["event"]["description"] for f in again["files"])
    env[1].data["Projects"] = {17: env[1].data["INBOX"].pop(1)}
    moved = ref.model_copy(update={"folder": "Projects", "uid": 17})
    args = (moved, *args[1:])
    after_move = server.route_attachments(*args, preview=False, deadline=deadline)
    assert after_move["calendar_request"] == request
    assert [f["status"] for f in after_move["files"]] == [
        "already_saved",
        "already_saved",
    ]
    files[saved["files"][0]["path"]] = b"different"
    with pytest.raises(accounts.Problem, match="отличается"):
        server.route_attachments(*args, preview=False)
    assert files[saved["files"][0]["path"]] == b"different"


def test_no_transfer_to_wrong_profile_folder_or_readonly(quote, disk):
    ref, _ = quote
    owner, files = disk
    with pytest.raises(accounts.Problem, match="владельца"):
        server.route_attachments(ref, "another", "/В работу", [], False)
    with pytest.raises(accounts.Problem, match="404"):
        server.route_attachments(ref, owner.uid, "/missing", [], False)
    owner.writable = False
    with pytest.raises(accounts.Problem, match="запись"):
        server.route_attachments(ref, owner.uid, "/В работу", [], False)
    assert not files
