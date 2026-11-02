"""The release manifest writer (tools/release_manifest.py).

The writer and the updater must read the same format. The strongest cheap
assertion of that is here: the tool writes a document, this test validates it
with the updater's own parser, and every declared sha256 is recomputed from the
file on disk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from _load_by_path import load_by_path
from revl.update.manifest import parse_manifest

ROOT = Path(__file__).resolve().parents[1]
tool = load_by_path("release_manifest_tool", ROOT / "tools" / "release_manifest.py")


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "revl"\nversion = "2.1.0"\n'
        'requires-python = ">=3.11"\n', encoding="utf-8")
    return root


def test_the_written_manifest_validates_and_pins_every_artifact(tmp_path):
    root = _checkout(tmp_path)
    sdist = tmp_path / "revl-2.1.0.tar.gz"
    sdist.write_bytes(b"sdist bytes")
    wheel = tmp_path / "revl-2.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    out = tmp_path / "release.json"
    rc = tool.main([str(sdist), str(wheel), "--root", str(root),
                    "--out", str(out), "--commit", "a" * 40])
    assert rc == 0
    manifest = parse_manifest(out.read_bytes())
    assert manifest.version == "2.1.0"
    assert manifest.tag == "v2.1.0"
    assert manifest.commit == "a" * 40
    assert manifest.host_changes is True
    assert manifest.live is False
    assert manifest.compat["gateApi"]
    by_name = {artifact.name: artifact for artifact in manifest.artifacts}
    assert by_name[sdist.name].kind == "sdist"
    assert by_name[wheel.name].kind == "wheel"
    for artifact in manifest.artifacts:
        assert artifact.sha256 == hashlib.sha256(
            (tmp_path / artifact.name).read_bytes()).hexdigest()
        assert artifact.url.startswith(
            "https://github.com/inso1337/revl/releases/download/v2.1.0/")


def test_validate_accepts_the_written_manifest_and_refuses_a_tampered_one(
        tmp_path, capsys):
    root = _checkout(tmp_path)
    artifact = tmp_path / "revl-2.1.0.tar.gz"
    artifact.write_bytes(b"bytes")
    out = tmp_path / "release.json"
    assert tool.main([str(artifact), "--root", str(root), "--out", str(out),
                      "--commit", "a" * 40]) == 0
    capsys.readouterr()
    assert tool.main(["--validate", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    document = json.loads(out.read_text(encoding="utf-8"))
    document["artifacts"][0]["sha256"] = "not-a-sha256"
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(document), encoding="utf-8")
    assert tool.main(["--validate", str(broken)]) == 1
    assert "MANIFEST" in capsys.readouterr().err


def test_a_release_with_no_artifact_is_refused(tmp_path):
    import pytest

    root = _checkout(tmp_path)
    with pytest.raises(SystemExit) as raised:
        tool.main(["--root", str(root), "--out",
                   str(tmp_path / "release.json"),
                   "--commit", "a" * 40])
    assert "artifact" in str(raised.value)


def test_the_live_flag_is_declared_only_when_asked(tmp_path):
    root = _checkout(tmp_path)
    artifact = tmp_path / "source.tar.gz"
    artifact.write_bytes(b"source")
    out = tmp_path / "release.json"
    assert tool.main([str(artifact), "--root", str(root), "--out", str(out),
                      "--commit", "a" * 40, "--live",
                      "--no-host-changes"]) == 0
    manifest = parse_manifest(out.read_bytes())
    assert manifest.live is True
    assert manifest.host_changes is False
