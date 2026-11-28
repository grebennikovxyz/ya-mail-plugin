"""Submit the pairing form in Android Chromium and iOS WebKit emulation."""

import re
from http.server import HTTPServer
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from ya_mail import web_wizard


def sealed_setup(pw, engine, device):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from urllib.parse import urlsplit

    from ya_mail import accounts, pairing

    docs = Path(__file__).resolve().parents[1] / "docs"
    with (
        TemporaryDirectory() as tmp,
        HTTPServer(
            ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(docs))
        ) as static,
    ):
        thread = Thread(target=static.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.object(accounts, "DATA", Path(tmp)),
                patch.object(pairing, "connect") as connect,
            ):
                fragment = urlsplit(pairing.challenge("qa@example.org")["url"]).fragment
                browser = engine.launch()
                try:
                    page = browser.new_page(**pw.devices[device])
                    page.goto(f"http://127.0.0.1:{static.server_port}/#{fragment}")
                    expect(
                        page.get_by_text("Вы подключаете qa@example.org")
                    ).to_be_visible()
                    expect(page.locator("#privacy")).to_be_visible()
                    requests = []
                    page.on("request", lambda request: requests.append(request.url))
                    page.get_by_label("Пароль приложения", exact=True).fill(
                        "fixturepass"
                    )
                    page.get_by_role("button", name="Получить код для чата").click()
                    expect(page.locator("#code")).to_have_value(re.compile(r"^YM1\."))
                    code = page.locator("#code").input_value()
                    assert code.startswith("YM1.") and "fixturepass" not in code
                    assert not requests, "Encrypting a password must not send a request"
                    assert pairing.finish(code)["connected"]
                    connect.assert_called_once_with(
                        "qa@example.org", "fixturepass", "read", local_store=False
                    )
                    print(f"{device}: encrypted app-password exchange OK")
                finally:
                    browser.close()
        finally:
            static.shutdown()
            thread.join()


def main():
    with HTTPServer(("127.0.0.1", 0), web_wizard.handler("fixture-code")) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.object(web_wizard, "connect") as connect,
                sync_playwright() as pw,
            ):
                for engine, device in (
                    (pw.chromium, "Pixel 7"),
                    (pw.webkit, "iPhone 13"),
                ):
                    server.connected = False
                    sealed_setup(pw, engine, device)
                    browser = engine.launch()
                    try:
                        page = browser.new_page(**pw.devices[device], locale="ru-RU")
                        page.goto(f"http://127.0.0.1:{server.server_port}/")
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        )
                        expect(
                            page.get_by_role("link", name="Пароли приложений Яндекс ID")
                        ).to_have_attribute("href", web_wizard.wizard.PASSWORDS)
                        page.get_by_label("Код мастера").fill("fixture-code")
                        page.get_by_label("Полный адрес").fill("qa@example.org")
                        page.get_by_label("Пароль приложения").fill("fixture")
                        page.get_by_role(
                            "button", name="Проверить и подключить"
                        ).click()
                        expect(
                            page.get_by_text("Ящик подключён.", exact=False)
                        ).to_be_visible()
                        connect.assert_called_once_with(
                            "qa@example.org", "fixture", "read"
                        )
                        connect.reset_mock()
                        print(f"{device} ({engine.name}): OK")
                    finally:
                        browser.close()
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
