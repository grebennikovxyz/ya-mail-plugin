"""Submit the pairing form in Android Chromium and iOS WebKit emulation."""

from http.server import HTTPServer
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from ya_mail import web_wizard


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
