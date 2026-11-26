import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_single_version_bump_and_release_checks(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    import marketplaces as m

    for path in (
        *m.MANIFESTS,
        "pyproject.toml",
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, tmp_path / path)
    m.sync(tmp_path, new_version="7.2.4")
    assert m.sync(tmp_path, tag="v7.2.4") == "7.2.4"
    with pytest.raises(ValueError, match="Tag"):
        m.sync(tmp_path, tag="v7.2.5")
    path = tmp_path / "n8n/package-lock.json"
    data = json.loads(path.read_bytes())
    data["packages"][""]["version"] = "7.2.3"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Version mismatch"):
        m.sync(tmp_path)


def test_skill_retry_requires_exact_version_and_bytes(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    import clawhub_release as c

    version = "0.5.1"
    file = c.ROOT / c.SKILL / "SKILL.md"
    item = {"path": "SKILL.md", "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    response = {"version": {"version": version, "files": [item]}}
    monkeypatch.setattr(
        c.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(response)),
    )
    assert c.skill_visible("clawhub", {}, "owner", version)
    assert not c.skill_visible("clawhub", {}, "owner", "0.5.2")
    item["sha256"] = "wrong"
    with pytest.raises(SystemExit, match="different content"):
        c.skill_visible("clawhub", {}, "owner", version)
    response.clear()
    response["publication"] = {"stage": "blocked"}
    with pytest.raises(SystemExit, match="blocked"):
        c.skill_visible("clawhub", {}, "owner", version)
