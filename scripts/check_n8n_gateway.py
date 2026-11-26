"""Exercise the compiled n8n node against a real isolated HTTPS Ya-Ma gateway."""

import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import trustme
import uvicorn

from ya_mail import accounts, remote

ROOT = Path(__file__).resolve().parents[1]


def check(work):
    cert, key, ca_file = work / "cert.pem", work / "key.pem", work / "ca.pem"
    ca = trustme.CA()
    localhost = ca.issue_cert("127.0.0.1")
    ca.cert_pem.write_to_path(ca_file)
    localhost.private_key_pem.write_to_path(key)
    localhost.cert_chain_pems[0].write_to_path(cert)
    accounts.DATA = work / "profile"
    with accounts.database() as db:
        db.execute(
            "INSERT INTO accounts VALUES(?,?,?)",
            ("fixture@example.org", "read", "fixture"),
        )
    token = secrets.token_urlsafe(32)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        origin = f"https://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(
                remote.application(origin, token),
                ssl_certfile=cert,
                ssl_keyfile=key,
                access_log=False,
            )
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Gateway startup failed")
                time.sleep(0.05)
            subprocess.run(
                ["node", "tests/gateway.cjs", origin + "/mcp"],
                cwd=ROOT / "n8n",
                check=True,
                env=os.environ
                | {"NODE_EXTRA_CA_CERTS": str(ca_file), "TEST_YA_MAIL_TOKEN": token},
                timeout=45,
            )
        finally:
            server.should_exit = True
            thread.join(15)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        check(Path(directory))
