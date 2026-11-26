import asyncio
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import consent
from test_cleanup_memory import message, roles

from ya_mail import accounts, mail, outbox, server


def test_known_reply_similar_domain_and_account_isolation(env):
    acc, box = env
    roles(box)
    message(box, acc, "Sent", "buyer@printing.example", 1)
    original = message(box, acc, "INBOX", "buyer@printing.example", 2, True)
    ref = mail.Ref(**server.prepare_reply(original, "Ответ")["ref"])
    ctx = consent()
    assert asyncio.run(server.send_draft(ref, ctx))["state"] == "queued"
    ctx.elicit.assert_not_called()
    lookalike = outbox.Letter(
        to=["buyer@printlng.example"], subject="Расчёт", text="Текст"
    )
    deny = consent()
    deny.elicit.return_value = SimpleNamespace(action="decline", data=None)
    with pytest.raises(accounts.Problem, match="не запланирована"):
        asyncio.run(server.send_email(acc.email, uuid4(), lookalike, deny))
    prompt = deny.elicit.call_args.args[0]
    assert "buyer@printing.example" in prompt and "buyer@printlng.example" in prompt
    other = accounts.Account("other@example.org", "read", "fixture")
    accounts.save(other)
    # The other account cannot inherit trust from the first even when the same IMAP
    # fixture contains a Sent message bearing the first account's From address.
    assert server.resolve_recipient(other.email, "buyer@printing.example")["total"] == 0
    server.mail_learning(acc.email, "contacts")
    with pytest.raises(accounts.Problem, match="памяти"):
        server.save_mail_fact(
            original,
            "preference",
            "Бумага",
            "buyer@printing.example",
            date.today(),
            "Бумага",
        )
