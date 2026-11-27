import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from counter import ORIGIN, PIXEL, ROUTE, Counter, Server, initialize


class CounterTests(unittest.TestCase):
    def test_public_requests_and_private_total(self):
        with TemporaryDirectory() as tmp, redirect_stderr(StringIO()) as logs:
            database = Path(tmp) / "count.sqlite3"
            initialize(database)
            with Server(("127.0.0.1", 0), Counter) as server:
                server.database = database
                thread = Thread(target=server.serve_forever)
                thread.start()

                def request(path=ROUTE, origin=ORIGIN, method="GET"):
                    headers = {"Origin": origin} if origin else {}
                    with closing(
                        HTTPConnection(*server.server_address, timeout=5)
                    ) as connection:
                        connection.request(method, path, headers=headers)
                        response = connection.getresponse()
                        return response.status, response.headers, response.read()

                try:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        for status, headers, body in pool.map(
                            lambda _: request(), range(20)
                        ):
                            self.assertEqual((status, body), (200, PIXEL))
                            self.assertIn("no-store", headers["Cache-Control"])
                            self.assertEqual(
                                headers["Access-Control-Allow-Origin"], ORIGIN
                            )
                            self.assertNotIn("Set-Cookie", headers)
                    for path, origin, method in (
                        ("/total", ORIGIN, "GET"),
                        (ROUTE + "?private", ORIGIN, "GET"),
                        (ROUTE, "", "GET"),
                        (ROUTE, "https://example.org", "GET"),
                        (ROUTE, ORIGIN, "HEAD"),
                        (ROUTE, ORIGIN, "POST"),
                    ):
                        self.assertGreaterEqual(request(path, origin, method)[0], 400)
                finally:
                    server.shutdown()
                    thread.join()
            initialize(database)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute("SELECT * FROM openings").fetchall(), [(1, 20)]
                )
            self.assertEqual(logs.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
