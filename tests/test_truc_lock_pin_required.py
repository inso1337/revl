"""A truc.lock pin is REQUIRED, not optional (the F3 supply-chain regression).

`plan_drift` — truc's tamper check — used to look up each vendored truc's pin
and, finding none, skip the truc entirely. Nothing anywhere required that a
truc named in `truc.toml` carry a lock row at all. So the tamper check was
opt-out by *deletion*: substitute the vendored bytes for anything you like,
delete the truc's lock row (or blank its `sourceHash`), and `truc assemble`
admitted the substitute and reported "every one admitted through the gate".

The gate is not what failed there — the substitute below is genuinely
admissible, it just reaches an unscoped emission the original never did. The
tamper check is the thing that was supposed to notice, and it opted out.

These drive the REAL `truc` CLI end to end (the same discipline as
test_truc_add_assemble.py), because the defect was in shipped CLI behaviour.
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


# The substitute: same `db: Database` surface as PgDatabase, so it joins the
# assembly exactly where PgDatabase did — but it reaches an unscoped emission
# through a service PgDatabase never had. Structurally admissible, materially
# a different component. Only the pin can tell the two apart.
SUBSTITUTE = """
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}

service Exfil {
  emission fn send(payload: Str) -> Int
}

component ExfilDatabase requires wire: Exfil provides db: Database {
  let pool = effect Pool.open("pg://", 10) undo pool.close()

  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = emit wire.send(sql)
  }
}
"""


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
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


def _assembled_project(tmp_path: Path) -> Path:
    """A project with two trucs added and pinned, the honest starting state."""
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0
    assert _truc(proj, "add", "pg_database").returncode == 0
    assert _truc(proj, "assemble").returncode == 0
    return proj


def _substitute_pg(proj: Path) -> None:
    """Swap the vendored PgDatabase source for the substitute, leaving
    truc.toml and the vendored directory layout untouched."""
    (proj / "trucs" / "pg_database" / "component.rvl").write_text(SUBSTITUTE)


def _lock(proj: Path) -> dict:
    return json.loads((proj / "truc.lock").read_text())


def _write_lock(proj: Path, lock: dict) -> None:
    (proj / "truc.lock").write_text(json.dumps(lock, indent=2))


def _drop_lock_row(proj: Path, name: str) -> None:
    lock = _lock(proj)
    lock["trucs"] = [r for r in lock["trucs"] if r["name"] != name]
    _write_lock(proj, lock)


def _blank_pin(proj: Path, name: str) -> None:
    lock = _lock(proj)
    for row in lock["trucs"]:
        if row["name"] == name:
            row["sourceHash"] = ""
    _write_lock(proj, lock)


# --------------------------------------------------- the substitute is admissible

def test_the_substitute_would_pass_the_gate(tmp_path):
    """The premise of every exploit below: the gate is NOT at fault. The
    substitute compiles clean and provides exactly the `db: Database` the
    assembly wired PgDatabase into — it is structurally admissible. What tells
    it apart from PgDatabase is the pin and nothing else."""
    from revl.compiler import compile_files
    from revl.registry import _capabilities_of, _audit_document

    path = tmp_path / "substitute.rvl"
    path.write_text(SUBSTITUTE)
    ir = compile_files([str(path)])
    provides = {}
    for comp in ir.get("components") or []:
        provides.update(comp.get("provides") or {})
    assert provides == {"db": "Database"}
    # and it reaches wider authority than PgDatabase ever did.
    caps, _ = _capabilities_of(_audit_document(ir).get("boundary") or {})
    assert caps == ("*",)


# ------------------------------------------------------ the control (was green)

def test_substitution_is_refused_when_the_pin_exists(tmp_path):
    """The behaviour that already worked: with the lock row intact, the
    substituted bytes do not hash to the pin and assemble refuses."""
    proj = _assembled_project(tmp_path)
    _substitute_pg(proj)

    result = _truc(proj, "assemble")
    assert result.returncode == 1, result.stdout
    assert "hash drift" in result.stdout
    assert "pg_database" in result.stdout


# ------------------------------------------------------------- the exploits

def test_substitution_with_the_lock_row_deleted_is_refused(tmp_path):
    """Deleting the truc's lock row must not delete the check. A truc named in
    truc.toml with no lock row is UNPINNED, and an unpinned truc is refused."""
    proj = _assembled_project(tmp_path)
    _substitute_pg(proj)
    _drop_lock_row(proj, "pg_database")

    result = _truc(proj, "assemble")
    assert result.returncode == 1, result.stdout
    assert "refused" in result.stdout
    assert "unpinned truc" in result.stdout
    assert "pg_database" in result.stdout
    # all-or-nothing: the substitute never reaches the composition manifest.
    assembly = proj / "build" / "assembly.json"
    assert "ExfilDatabase" not in (assembly.read_text() if assembly.exists() else "")


def test_substitution_with_a_blank_source_hash_is_refused(tmp_path):
    """A lock row that is present but whose `sourceHash` is "" is the same
    exploit wearing a lock row: a blank pin is no pin."""
    proj = _assembled_project(tmp_path)
    _substitute_pg(proj)
    _blank_pin(proj, "pg_database")

    result = _truc(proj, "assemble")
    assert result.returncode == 1, result.stdout
    assert "refused" in result.stdout
    assert "unpinned truc" in result.stdout
    assert "blank sourceHash" in result.stdout
    assembly = proj / "build" / "assembly.json"
    assert "ExfilDatabase" not in (assembly.read_text() if assembly.exists() else "")


def test_an_unpinned_truc_is_refused_even_when_its_bytes_are_honest(tmp_path):
    """The pin requirement is not "refuse when we notice a substitution", it is
    "refuse when nothing pinned these bytes". Untampered vendored source with no
    lock row is refused too — otherwise the check is still opt-out, it just
    needs one extra step."""
    proj = _assembled_project(tmp_path)
    _drop_lock_row(proj, "pg_database")

    result = _truc(proj, "assemble")
    assert result.returncode == 1, result.stdout
    assert "unpinned truc" in result.stdout


def test_assemble_check_refuses_an_unpinned_truc_too(tmp_path):
    """`assemble --check` runs the identical resolve/verify/admit chain, so the
    dry-run must answer "invalid" for exactly the same reason."""
    proj = _assembled_project(tmp_path)
    _substitute_pg(proj)
    _drop_lock_row(proj, "pg_database")

    result = _truc(proj, "assemble", "--check")
    assert result.returncode == 1, result.stdout
    assert "invalid" in result.stdout
    assert "unpinned truc" in result.stdout


# --------------------------------------------------- what `add` does on a first add

def test_add_mints_the_pin_on_a_first_add(tmp_path):
    """A first-time `add` GENERATES the pin — it never tolerates its absence.
    The row `add` commits carries the registry index's `sourceHash`, which
    `plan_add_report` has already re-verified against the fetched bytes, so
    every truc that enters an assembly enters it pinned."""
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "pg_database").returncode == 0

    rows = {r["name"]: r for r in _lock(proj)["trucs"]}
    assert "pg_database" in rows
    pin = rows["pg_database"]["sourceHash"]
    assert pin, "a first add must mint a non-empty pin"

    import hashlib
    vendored = (proj / "trucs" / "pg_database" / "component.rvl").read_bytes()
    assert pin == hashlib.sha256(vendored).hexdigest()

    # and the freshly added truc assembles: the pin requirement refuses only
    # what is actually unpinned.
    assert _truc(proj, "assemble").returncode == 0
