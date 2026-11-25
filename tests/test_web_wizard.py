import http.client
import threading
from http.server import HTTPServer
from urllib.parse import urlencode

from ya_mail import web_wizard


def test_phone_form_requires_pairing_and_same_origin(monkeypatch):
    writes = []
    monkeypatch.setattr(web_wizard, "connect", lambda *args: writes.append(args))
    with HTTPServer(("127.0.0.1", 0), web_wizard.handler("test-pair")) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"

        def call(method, body=None, **headers):
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=4
            )
            conn.request(method, "/", body, headers)
            response = conn.getresponse()
            data = response.read().decode()
            conn.close()
            return response, data

        try:
            response, html = call("GET")
            assert response.getheader("Referrer-Policy") == "same-origin"
            assert (
                response.status == 200
                and response.getheader("Cache-Control") == "no-store"
            )
            assert 'type="password"' in html and "https://id.yandex.ru/" in html
            assert call("GET", Host="unrelated.example")[0].status == 404
            fields = {
                "email": "qa@example.org",
                "password": "sample",
                "mode": "read",
                "pair": "wrong",
            }
            headers = {
                "Origin": origin,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            assert call("POST", urlencode(fields), **headers)[0].status == 403
            fields["pair"] = "test-pair"
            assert (
                call(
                    "POST",
                    urlencode(fields),
                    **{**headers, "Origin": "https://other.example"},
                )[0].status
                == 403
            )
            assert not writes
            response, html = call("POST", urlencode(fields), **headers)
            assert response.status == 200 and "sample" not in html
            assert server.connected and writes == [("qa@example.org", "sample", "read")]
        finally:
            server.shutdown()
            thread.join()
