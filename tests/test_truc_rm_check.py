"""truc slice S3 — `truc rm <name>` + `truc assemble --check`.

Like the S1 suite, these drive the real `truc` console command through the
backend's own venv against the repo's `registry/`, and assert the two S3
behaviours end to end:

  * `truc rm <name>` un-vendors the truc, drops it from truc.toml and
    truc.lock, and then re-verifies the remainder still admits — a removal
    that strands a still-required provider is *refused* (the unmet requirement
    named) with the disk left untouched (S1's empty-guard pattern);
  * `truc assemble --check` resolves + admits every truc and reports the
    verdict (with the why-trace on invalid) while writing NOTHING to disk.

Without the runtime these skip (never reported as passing). Set it up with
`sh backends/python/setup.sh`.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the `truc` composition from `project` as its working directory."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


def _new_project(tmp_path: Path) -> Path:
    proj = tmp_path / "app"
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


def _seeded(tmp_path: Path) -> Path:
    """A project with user_cache (requires `db`) + pg_database (provides `db`)
    already added — the compatible arrangement S1 assembles clean."""
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0
    assert _truc(proj, "add", "pg_database").returncode == 0
    return proj


# --------------------------------------------------------------- rm: the happy path

def test_rm_unvendors_and_updates_toml_and_lock(tmp_path):
    proj = _seeded(tmp_path)

    # user_cache is the sole consumer of `db`; removing *it* strands nobody
    # (pg_database, a lone provider, is a valid composition on its own).
    r = _truc(proj, "rm", "user_cache")
    assert r.returncode == 0, r.stderr or r.stdout
    assert "removed" in r.stdout and "user_cache" in r.stdout

    # un-vendored from trucs/, dropped from both truc.toml and truc.lock
    assert not (proj / "trucs" / "user_cache").exists()
    assert _lock_names(proj) == ["pg_database"]
    toml = (proj / "truc.toml").read_text()
    assert "user_cache" not in toml
    assert 'pg_database = { registry = "local" }' in toml

    # and the remainder still assembles for real
    r = _truc(proj, "assemble")
    assert r.returncode == 0, r.stderr or r.stdout
    assert "assembled" in r.stdout


# -------------------------------------------------- rm of a still-required provider

def test_rm_of_still_required_provider_is_refused(tmp_path):
    proj = _seeded(tmp_path)

    # pg_database is the ONLY `db` provider, and user_cache still requires `db`
    # — removing it would strand a consumer. Honest refusal, disk untouched.
    r = _truc(proj, "rm", "pg_database")
    assert r.returncode == 1, r.stdout
    assert "refused" in r.stdout
    assert "unmet requirement" in r.stdout
    # the stranded consumer and the exact key are named
    assert "UserCache" in r.stdout and "db" in r.stdout

    # nothing touched: pg_database is still vendored, locked, and in truc.toml
    assert (proj / "trucs" / "pg_database" / "component.rvl").exists()
    assert set(_lock_names(proj)) == {"user_cache", "pg_database"}
    assert "pg_database" in (proj / "truc.toml").read_text()


def test_rm_unknown_component_is_refused(tmp_path):
    proj = _seeded(tmp_path)
    r = _truc(proj, "rm", "nonesuch")
    assert r.returncode == 1, r.stdout
    assert "not in the assembly" in r.stdout
    # the real trucs are untouched
    assert set(_lock_names(proj)) == {"user_cache", "pg_database"}


# --------------------------------------------------------------- assemble --check

def test_assemble_check_reports_valid_and_writes_nothing(tmp_path):
    proj = _seeded(tmp_path)

    r = _truc(proj, "assemble", "--check")
    assert r.returncode == 0, r.stderr or r.stdout
    assert "valid" in r.stdout
    # a dry-run writes NOTHING: no build/assembly.json (no build/ at all)
    assert not (proj / "build").exists()


def test_assemble_check_reports_invalid_with_why_trace_and_writes_nothing(tmp_path):
    proj = _seeded(tmp_path)

    # hand-introduce a second `db` provider (G2) without going through `add`
    shutil.copytree(REGISTRY / "components" / "mysql_database",
                    proj / "trucs" / "mysql_database")
    with (proj / "truc.toml").open("a") as handle:
        handle.write('mysql_database = { registry = "local" }\n')

    r = _truc(proj, "assemble", "--check")
    assert r.returncode == 1, r.stdout
    assert "invalid" in r.stdout
    assert "G2" in r.stdout
    assert "PgDatabase" in r.stdout and "MysqlDatabase" in r.stdout
    # still writes nothing on an invalid verdict
    assert not (proj / "build").exists()


def test_assemble_check_does_not_clobber_an_existing_assembly(tmp_path):
    proj = _seeded(tmp_path)

    # a real assemble writes build/assembly.json...
    assert _truc(proj, "assemble").returncode == 0
    before = (proj / "build" / "assembly.json").read_text()

    # ...and a later --check must not touch it (dry-run writes nothing)
    assert _truc(proj, "assemble", "--check").returncode == 0
    assert (proj / "build" / "assembly.json").read_text() == before


def test_assemble_check_flag_after_verb_is_a_distinct_verb(tmp_path):
    """`assemble --check` is the dry-run; plain `assemble` still writes. This
    guards the CLI flag parse (a distinct enum arm, not a swallowed argument)."""
    proj = _seeded(tmp_path)
    assert not (proj / "build").exists()
    assert _truc(proj, "assemble", "--check").returncode == 0
    assert not (proj / "build").exists()
    assert _truc(proj, "assemble").returncode == 0
    assert (proj / "build" / "assembly.json").exists()
