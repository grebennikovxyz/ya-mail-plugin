import concurrent.futures

import httpx

from ya_mail import accounts, server, updates


def test_shared_daily_check_one_notice_and_failed_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(updates, "installed", lambda: "0.9.9")
    monkeypatch.setattr(updates, "fetch", lambda: calls.append(1) or "0.10.0")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: updates.check(), range(4)))
    assert calls == [1]
    first = server.list_accounts()
    assert first["plugin_update"]["available_version"] == "0.10.0"
    assert "plugin_update" not in server.list_accounts()

    def offline():
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(updates, "fetch", offline)
    state = server.version_status(refresh=True)
    assert state["check_failed"] and state["available"] and state["last_success"]
    monkeypatch.setattr(updates, "installed", lambda: "0.10.0")
    assert not server.version_status()["available"]


def test_disable_and_stable_release_only(monkeypatch):
    assert not updates.configure(False)["automatic_check"]
    monkeypatch.setattr(
        updates, "fetch", lambda: (_ for _ in ()).throw(AssertionError("offline mode"))
    )
    assert not updates.check()["available"]
    assert updates.notice() is None
    with accounts.database() as db:
        assert updates.state(db)["latest"] is None
    assert not updates.numbers("1.5.0-rc.2")
    assert not updates.numbers("v9.0.0/elsewhere")
