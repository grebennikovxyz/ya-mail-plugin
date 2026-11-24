"""Build a portable manifest archive or a self-contained Windows x64 plugin."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "yandex-mail"
DIST = ROOT / "dist"
VERSION = json.loads((PLUGIN / "plugin.json").read_bytes())["version"]
FILES = [
    "plugin.json",
    ".codex-plugin/plugin.json",
    "mcp.json",
    "skills/yandex-mail/SKILL.md",
]


def runtime_files(archive, runtime, packages):
    for path in runtime.rglob("*"):
        rel = path.relative_to(runtime)
        if (
            rel.parts[0] in {"include", "libs", "Scripts"}
            or rel.parts[:2] == ("Lib", "site-packages")
            or "__pycache__" in rel.parts
            or path.suffix == ".pyc"
        ):
            continue
        if path.is_file():
            archive.write(path, "runtime/" + rel.as_posix())
    for path in packages.rglob("*"):
        rel = path.relative_to(packages)
        if (
            rel.parts[0] in {"Scripts", "bin"}
            or "__pycache__" in rel.parts
            or path.suffix == ".pyc"
            or path.name in {"direct_url.json", "uv_cache.json", "uv_build.json"}
        ):
            continue
        if path.is_file():
            archive.write(path, "runtime/Lib/site-packages/" + rel.as_posix())


def package(runtime=None):
    DIST.mkdir(exist_ok=True)
    name = f"yandex-mail-{VERSION}" + ("-windows-x64" if runtime else "")
    path = DIST / (name + ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        packages = Path(tmp) / "packages"
        if runtime:
            if sys.platform != "win32" or not (runtime / "python.exe").is_file():
                raise SystemExit(
                    "Select a clean uv standalone Windows x64 runtime directory."
                )
            wheel = next(DIST.glob(f"grebennikov_ya_mail-{VERSION}-*.whl"))
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(runtime / "python.exe"),
                    "--target",
                    str(packages),
                    "--cache-dir",
                    str(ROOT / ".cache/uv"),
                    "--only-binary",
                    ":all:",
                    str(wheel),
                ],
                check=True,
            )
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for f in FILES:
                content = (PLUGIN / f).read_bytes()
                if runtime and f == "mcp.json":
                    config = json.loads(content)
                    for server in config["mcpServers"].values():
                        server["command"] = "./runtime/python.exe"
                        server["args"] = [
                            "-I",
                            "-B",
                            "-m",
                            "ya_mail.cli",
                            *server["args"],
                        ]
                    content = json.dumps(config, indent=2).encode()
                archive.writestr(f, content)
            for f in ("README.md", "LICENSE"):
                archive.write(ROOT / f, f)
            if runtime:
                runtime_files(archive, runtime, packages)
                for cmd in ("connect", "connect-360"):
                    archive.writestr(
                        cmd + ".cmd",
                        f'@echo off\r\n"%~dp0runtime\\python.exe" -I -B -m ya_mail.cli {cmd}\r\nif errorlevel 1 pause\r\n',
                    )
    print(path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-runtime", type=Path)
    args = parser.parse_args()
    package()
    if args.windows_runtime:
        package(args.windows_runtime.resolve())
    with (DIST / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as sums:
        for path in sorted(DIST.iterdir()):
            if path.suffix in {".zip", ".whl"}:
                with path.open("rb") as f:
                    sums.write(
                        f"{hashlib.file_digest(f, 'sha256').hexdigest()}  {path.name}\n"
                    )
