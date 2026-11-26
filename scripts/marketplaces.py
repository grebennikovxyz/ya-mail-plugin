"""Keep the installable Git marketplaces and release packages on the same version."""

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path("plugins/yandex-mail")
MANIFESTS = (
    PLUGIN / "plugin.json",
    PLUGIN / ".codex-plugin/plugin.json",
    PLUGIN / ".claude-plugin/plugin.json",
    Path("n8n/package.json"),
    Path("n8n/package-lock.json"),
)


def sync(root, new_version=None, tag=None):
    project = root / "pyproject.toml"
    toml = project.read_text(encoding="utf-8")
    current = tomllib.loads(toml)["project"]["version"]
    version = current if new_version is None else new_version
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}", version
    ):
        raise ValueError(
            f"Expected a stable major.minor.patch version, got {version!r}"
        )
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} must be v{version}")

    for catalog in (
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
    ):
        entries = json.loads((root / catalog).read_bytes())["plugins"]
        (entry,) = (p for p in entries if p["name"] == PLUGIN.name)
        source = entry["source"]
        path = source["path"] if isinstance(source, dict) else source
        if (root / path).resolve() != (root / PLUGIN).resolve():
            raise ValueError(f"{catalog} points to an unexpected plugin directory")

    # Stage all edits before changing a file so invalid metadata fails the whole bump.
    edits = {}
    if current != version:
        before = f'version = "{current}"'
        if toml.splitlines().count(before) != 1:
            raise ValueError("Cannot identify the project version in pyproject.toml")
        edits[project] = toml.replace(before, f'version = "{version}"', 1)
    for relative in MANIFESTS:
        path = root / relative
        data = json.loads(path.read_bytes())
        records = [data]
        if relative.name == "package-lock.json":
            records.append(data["packages"][""])
        if relative.is_relative_to(PLUGIN) and data["name"] != PLUGIN.name:
            raise ValueError(f"Unexpected plugin name in {relative}")
        if any(record["version"] != version for record in records):
            for record in records:
                record["version"] = version
            edits[path] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if edits and new_version is None:
        raise ValueError(
            "Version mismatch: " + ", ".join(str(p.relative_to(root)) for p in edits)
        )
    for path, content in edits.items():
        path.write_text(content, encoding="utf-8", newline="\n")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.add_mutually_exclusive_group()
    args.add_argument(
        "--set-version", help="Set the project and all published manifest versions"
    )
    args.add_argument("--tag", help="Require a matching stable release tag")
    options = parser.parse_args()
    try:
        print(sync(ROOT, options.set_version, options.tag))
    except ValueError as error:
        parser.exit(1, f"{error}\n")
