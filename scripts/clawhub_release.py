"""Preview or publish one verified Ya-Ma release channel using the ClawHub CLI."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from http.client import HTTPSConnection
from pathlib import Path

from marketplaces import PLUGIN, ROOT, sync

REGISTRY = "https://clawhub.ai"
SKILL = PLUGIN / "skills/yandex-mail"


def skill_visible(cli, env, owner, version):
    """Require the public file list to match the local skill before reporting success."""
    result = subprocess.run(
        [cli, "inspect", f"@{owner}/ya-ma", "--version", version, "--files", "--json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode or not result.stdout.strip():
        return False
    data = json.loads(result.stdout)
    stage = (data.get("publication") or {}).get("stage")
    if stage in {"blocked", "failed", "expired", "checks_failed", "checks_incomplete"}:
        raise SystemExit(f"ClawHub skill {version}: {stage}")
    release = data.get("version") or {}
    if release.get("version") != version:
        return False
    expected = hashlib.sha256((ROOT / SKILL / "SKILL.md").read_bytes()).hexdigest()
    files = {f["path"]: f["sha256"] for f in release["files"]}
    if files != {"SKILL.md": expected}:
        raise SystemExit(f"Existing skill {version} has different content")
    return True


def run(channel, publish, config_dir):
    version = sync(ROOT, tag=os.environ.get("GITHUB_REF_NAME") if publish else None)
    cli = shutil.which("clawhub")
    if not cli:
        raise SystemExit("Install clawhub@0.27.0 to run this check")
    env = os.environ.copy()
    token = env.pop("CLAWHUB_TOKEN", "")
    config = config_dir / "config.json"
    env.update(CLAWHUB_CONFIG_PATH=str(config), CLAWHUB_REGISTRY=REGISTRY)
    owner = None
    if publish:
        if not token:
            raise SystemExit(
                "Add the CLAWHUB_TOKEN repository Actions secret, then rerun the failed job"
            )
        connection = HTTPSConnection("clawhub.ai", timeout=20)
        try:
            connection.request(
                "GET",
                "/api/v1/whoami",
                headers={
                    "Authorization": "Bearer " + token,
                    "User-Agent": "Ya-Ma-release",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise SystemExit(
                    f"ClawHub rejected the publisher lookup (HTTP {response.status})"
                )
            owner = json.load(response)["user"]["handle"]
        finally:
            connection.close()
        if not owner:
            raise SystemExit("ClawHub did not identify the token's publisher")
        config.write_text(
            json.dumps({"registry": REGISTRY, "token": token}), encoding="utf-8"
        )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    source = [
        "--source-repo",
        "grebennikovxyz/ya-mail-plugin",
        "--source-commit",
        commit,
    ]
    if publish:
        source += ["--source-ref", f"refs/tags/v{version}", "--owner", owner]
    if channel == "bundle":
        archive = ROOT / "dist" / f"yandex-mail-{version}-clawhub.tgz"
        if not archive.is_file():
            raise SystemExit(f"Build or download {archive.name} first")
        if (
            publish
            and subprocess.run(
                [
                    cli,
                    "package",
                    "verify",
                    str(archive),
                    "--package",
                    "yandex-mail",
                    "--version",
                    version,
                ],
                env=env,
                cwd=ROOT,
            ).returncode
            == 0
        ):
            return
        command = [
            "package",
            "publish",
            str(archive),
            "--family",
            "bundle-plugin",
            "--display-name",
            "Ya-Ma",
            "--source-path",
            PLUGIN.as_posix(),
        ]
        if publish:
            command += ["--wait", "--wait-timeout", "1800"]
    else:
        if publish and skill_visible(cli, env, owner, version):
            return
        command = [
            "skill",
            "publish",
            str(ROOT / SKILL),
            "--name",
            "Ya-Ma",
            "--slug",
            "ya-ma",
            "--source-path",
            SKILL.as_posix(),
        ]
    if not publish:
        command.append("--dry-run")
    result = subprocess.run(
        [cli, "--no-input", *command, "--version", version, *source], env=env, cwd=ROOT
    )
    if publish and channel == "skill":
        # A previous upload can have succeeded while its response was lost.
        # Only verified registry bytes can turn that ambiguous result into success.
        deadline = time.monotonic() + (1800 if result.returncode == 0 else 60)
        while not skill_visible(cli, env, owner, version):
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"ClawHub has not published the expected skill {version}; check the release in ClawHub"
                )
            time.sleep(15)
    else:
        result.check_returncode()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", choices=("bundle", "skill"))
    parser.add_argument(
        "--publish", action="store_true", help="Upload to the public registry"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        run(args.channel, args.publish, Path(tmp))
