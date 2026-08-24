"""truc slice S2 — the namespaced `revl truc <verb>` subcommand.

The human settled truc's second door as a `revl truc <verb>` subcommand group
(NOT flat `revl add` aliases): `src/revl/__main__.py` grows one REMAINDER-tail
forwarder that hands its tail verbatim to `revl.truc.main` — the very launcher
the standalone `truc` console script calls. So `revl truc add X` must behave
identically to `truc add X`, for every verb truc grows, with one implementation.

These drive the real CLI through the backend's own venv (the one with cordis-py
installed) against the repo's own `registry/`, and assert the two spellings are
observably the same engine. Without the runtime they skip (never a false pass).

Set up the runtime with `sh backends/python/setup.sh`.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _revl_truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    """`revl truc <verb> ...` from `project` as the working directory."""
    return subprocess.run([str(CORDIS_PY), "-m", "revl", "truc", *args],
                          cwd=str(project), env=_env(),
                          capture_output=True, text=True, timeout=300)


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    """The standalone `truc <verb> ...`, for the identical-behavior comparison."""
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=_env(),
                          capture_output=True, text=True, timeout=300)


def _new_project(tmp_path: Path, name: str = "app") -> Path:
    proj = tmp_path / name
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.rvl").write_text(
        "// the project's own composition entry (empty for this demo).\n")
    (proj / "truc.toml").write_text(
        '[assembly]\n'
        'name = "demo"\n'
        'entry = ["src/main.rvl"]\n\n'
        '[registries]\n'
        f'local = {{ path = "{REGISTRY}" }}\n\n'
        '[trucs]\n')
    return proj


def _lock_names(proj: Path) -> list[str]:
    lock = json.loads((proj / "truc.lock").read_text())
    return [r["name"] for r in lock["trucs"]]


# ---------------------------------------------------------------- add + assemble

def test_revl_truc_add_then_assemble_is_admitted_and_composed(tmp_path):
    """`revl truc add`/`assemble` vendors, locks and composes exactly as the
    standalone `truc` does — the dispatch reaches truc's real engine."""
    proj = _new_project(tmp_path)

    r = _revl_truc(proj, "add", "user_cache")
    assert r.returncode == 0, r.stderr
    assert "added" in r.stdout and "user_cache" in r.stdout

    assert _revl_truc(proj, "add", "pg_database").returncode == 0

    # vendored byte-identically and recorded, same as `truc add`
    assert (proj / "trucs" / "user_cache" / "component.rvl").read_text() == \
        (REGISTRY / "components" / "user_cache" / "component.rvl").read_text()
    assert set(_lock_names(proj)) == {"user_cache", "pg_database"}

    r = _revl_truc(proj, "assemble")
    assert r.returncode == 0, r.stderr
    assert "assembled" in r.stdout
    assembly = json.loads((proj / "build" / "assembly.json").read_text())
    assert "UserCache" in json.dumps(assembly) and "PgDatabase" in json.dumps(assembly)


# ----------------------------------------------- identical behavior to standalone

def test_revl_truc_add_matches_standalone_truc_add(tmp_path):
    """The two spellings are one engine: `revl truc add X` and `truc add X`
    against fresh identical projects leave identical exit code, output tail,
    and lock/vendor state."""
    proj_a = _new_project(tmp_path, "via_revl")
    proj_b = _new_project(tmp_path, "via_truc")

    a = _revl_truc(proj_a, "add", "user_cache")
    b = _truc(proj_b, "add", "user_cache")

    assert a.returncode == b.returncode == 0, (a.stderr, b.stderr)
    assert a.stdout == b.stdout
    assert _lock_names(proj_a) == _lock_names(proj_b) == ["user_cache"]
    assert (proj_a / "trucs" / "user_cache" / "component.rvl").read_text() == \
        (proj_b / "trucs" / "user_cache" / "component.rvl").read_text()


# ----------------------------------------------- the passthrough forwards G2 too

def test_revl_truc_add_refusal_is_forwarded_g2(tmp_path):
    """A refusal truc raises (G2: a second provider of a key) travels back
    through the passthrough unchanged — same nonzero exit, same why-trace,
    nothing written — proving the tail is truc's, not a reimplementation."""
    proj = _new_project(tmp_path)
    assert _revl_truc(proj, "add", "user_cache").returncode == 0
    assert _revl_truc(proj, "add", "pg_database").returncode == 0

    r = _revl_truc(proj, "add", "mysql_database")
    assert r.returncode == 1, r.stdout
    assert "G2" in r.stdout and "db" in r.stdout and "provider" in r.stdout
    assert not (proj / "trucs" / "mysql_database").exists()
    assert "mysql_database" not in _lock_names(proj)


# ----------------------------------------------- unknown verb reaches truc's usage

def test_revl_truc_unknown_verb_reaches_truc_usage(tmp_path):
    """An unknown verb is truc's to reject (usage, exit 2), not argparse's —
    the tail is forwarded rather than enumerated at the `revl` layer."""
    proj = _new_project(tmp_path)
    r = _revl_truc(proj, "frobnicate")
    assert r.returncode == 2, r.stdout
    assert "usage" in r.stdout
