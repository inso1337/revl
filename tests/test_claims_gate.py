"""The repository gate for `revl:claim` (issue #2846).

`tests/test_claims.py` holds the compiler-side rules. This module runs the real
entry point: the self-test that plants each failure, the production selection
path, the repository's own annotations, and a claim whose witness disappears.
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

from revl.claims import load_registry
from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_claims.py"
REGISTRY = ROOT / "claims.toml"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(cwd or ROOT), capture_output=True, text=True,
        timeout=300)


def _tool():
    """The gate module itself, for the one test that injects a selector."""
    return load_by_path("check_claims_gate_tool", TOOL)


def test_the_self_test_passes():
    result = _run("--self-test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "claims self-test: OK" in result.stdout


def test_the_repository_claims_all_resolve():
    # No affected selection here: resolving the real tree's selection is the
    # lint job's half of the check, and a unit test should not pay for it.
    result = _run("--check", "--no-selection")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_the_demo_escape_scenarios_each_carry_a_claim():
    registry = load_registry(REGISTRY)
    for name, claim_id in (
        ("g4_emission_widens_declared_capability.rvl",
         "g4-emission-widens-refused"),
        ("g9_service_return_launders_taint.rvl",
         "g9-service-return-launders-refused"),
        ("gsecret_service_return_discloses.rvl",
         "gsecret-service-return-discloses-refused"),
    ):
        text = (ROOT / "examples" / "rejections" / name).read_text(encoding="utf-8")
        assert f"revl:claim {claim_id}" in text, name
        assert claim_id in registry, name


def test_the_docs_carry_a_claim():
    text = (ROOT / "docs" / "rejections.md").read_text(encoding="utf-8")
    assert "revl:claim guarantee-table-generated" in text
    assert "guarantee-table-generated" in load_registry(REGISTRY)


def _claim_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "comp.rvl").write_text(
        "// revl:claim known\nfn f() -> Int { return 1 }\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_comp.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "claims.toml").write_text(
        '[claims]\nknown = "test:tests/test_comp.py"\n', encoding="utf-8")
    return tmp_path


def test_a_claim_whose_witness_disappears_fails(tmp_path):
    root = _claim_tree(tmp_path)
    clean = _run("--check", "--no-selection", "--root", str(root))
    assert clean.returncode == 0, clean.stdout + clean.stderr
    (root / "tests" / "test_comp.py").unlink()
    broken = _run("--check", "--no-selection", "--root", str(root))
    assert broken.returncode == 1
    assert "claim-witness-missing" in broken.stdout
    assert "src/comp.rvl:1" in broken.stdout


def test_an_unknown_claim_id_fails(tmp_path):
    root = _claim_tree(tmp_path)
    (root / "src" / "comp.rvl").write_text(
        "// revl:claim absent\nfn f() -> Int { return 1 }\n", encoding="utf-8")
    result = _run("--check", "--no-selection", "--root", str(root))
    assert result.returncode == 1
    assert "claim-unknown-id" in result.stdout


def test_an_annotation_without_a_registry_fails(tmp_path):
    root = _claim_tree(tmp_path)
    (root / "claims.toml").unlink()
    result = _run("--check", "--no-selection", "--root", str(root))
    assert result.returncode == 1
    assert "claim-registry-missing" in result.stdout


def test_the_gate_runs_the_production_selection_path(monkeypatch, tmp_path):
    """The other tests pass --no-selection or inject a selector into
    `claims.check`; this one drives `_run_check(selection=True)` through the
    gate's own `_selection` seam, so an always-None regression is a red."""
    tool = _tool()
    root = _claim_tree(tmp_path)
    monkeypatch.setattr(tool, "_selection", lambda _root: (lambda _path: set()))
    buffer = io.StringIO()
    assert tool._run_check(root, selection=True, stream=buffer) == 1
    assert "claim-witness-not-selected" in buffer.getvalue()


def test_an_alternate_root_without_no_selection_is_an_error(tmp_path):
    root = _claim_tree(tmp_path)
    result = _run("--check", "--root", str(root))
    assert result.returncode == 2
    assert "claim-selection-unavailable" in result.stdout
