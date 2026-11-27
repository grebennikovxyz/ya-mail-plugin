import base64
import json
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from ya_mail import accounts, chat_setup, pairing


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue(email="qa@example.org"):
    link = chat_setup.begin(email, False)["url"]
    data = json.loads(decode(urlsplit(link).fragment))
    key = serialization.load_der_public_key(decode(data["key"]))
    return data, key


def seal(data, key, email=None):
    label = f"Ya-Ma/1:{data['id']}:{email or data['email']}".encode()
    encrypted = key.encrypt(
        b'["read","fixturepassword",false]',
        padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=label
        ),
    )
    return f"YM1.{data['id']}.{pairing.encode(encrypted)}"


def test_encrypted_pairing_binding_expiry_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "DATA", tmp_path)
    calls = []
    monkeypatch.setattr(pairing, "connect", lambda *a, **kw: calls.append((a, kw)))
    data, key = issue()
    code = seal(data, key)
    with pytest.raises(accounts.Problem):
        pairing.finish(seal(data, key, "other@example.org"))
    assert not calls and "fixturepassword" not in code
    assert pairing.finish(code) == {
        "account": "qa@example.org",
        "mode": "read",
        "connected": True,
    }
    assert calls == [
        (("qa@example.org", "fixturepassword", "read"), {"local_store": False})
    ]
    with pytest.raises(accounts.Problem):
        pairing.finish(code)
    data, key = issue()
    with accounts.database() as db:
        db.execute("UPDATE mailbox_pairings SET expires=0")
    with pytest.raises(accounts.Problem):
        pairing.finish(seal(data, key))
    assert len(calls) == 1


def test_private_linux_storage(tmp_path, monkeypatch):
    import sys

    if sys.platform != "linux":
        pytest.skip("Linux headless storage")
    monkeypatch.setattr(accounts, "DATA", tmp_path)
    tmp_path.chmod(0o700)
    account = accounts.Account("qa@example.org", "read", "fixturepassword")
    accounts.save(account, local_store=True)
    assert accounts.current(account.email) == account
    with accounts.database() as db:
        record = db.execute("SELECT secret FROM accounts").fetchone()[0]
        assert record.startswith("local:") and account.password not in record
    assert (tmp_path / "mail-vault.key").stat().st_mode & 0o777 == 0o600
    accounts.forget(account.email)
    assert accounts.profiles() == []
