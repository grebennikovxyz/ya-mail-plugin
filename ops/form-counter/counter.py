"""Count Ya-Ma form openings. Run behind an HTTPS proxy with access logs disabled."""

import argparse
import base64
import os
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ORIGIN = "https://grebennikovxyz.github.io"
ROUTE = "/ya-ma/open.gif"
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def initialize(database):
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS openings (id INTEGER PRIMARY KEY CHECK (id = 1), total INTEGER NOT NULL)"
        )
        db.execute("INSERT OR IGNORE INTO openings VALUES (1, 0)")


class Counter(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # BaseHTTPRequestHandler otherwise records the client IP and request.
        pass

    def do_GET(self):
        if self.path != ROUTE or self.headers.get("Origin") != ORIGIN:
            self.send_error(404)
            return
        try:
            with closing(sqlite3.connect(self.server.database)) as db, db:
                db.execute("UPDATE openings SET total = total + 1 WHERE id = 1")
        except sqlite3.Error:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/gif")
        self.send_header("Content-Length", str(len(PIXEL)))
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(PIXEL)

    def setup(self):
        self.request.settimeout(5)
        super().setup()


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # The default error logger includes the client address on disconnect.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "total"))
    parser.add_argument(
        "database",
        type=Path,
        help="Database in the service account's private directory",
    )
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    if args.command == "total":
        with closing(
            sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            print(db.execute("SELECT total FROM openings WHERE id = 1").fetchone()[0])
        return
    os.umask(0o077)
    initialize(args.database)
    with Server(("127.0.0.1", args.port), Counter) as server:
        server.database = args.database
        server.serve_forever()


if __name__ == "__main__":
    main()
