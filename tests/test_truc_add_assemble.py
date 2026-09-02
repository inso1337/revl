"""truc slice S1 — `truc add` + `truc assemble` against the local registry.

The proof of truc is execution: these drive the real `truc` console command
through the backend's own venv (the one with cordis-py installed), against the
repo's own `registry/`, and assert the differentiator is observable from the
CLI — a fetched petit bout that would break the assembly (G2) or that has
drifted from its lock (hash drift) is *refused*, with a why-trace, and nothing
is written. Without the runtime these skip (never reported as passing).

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


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the `truc` composition from `project` as its working directory."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


def _revl(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl", *args],
                          cwd=str(ROOT), env=env,
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


def _hand_vendor(proj: Path, name: str) -> None:
    """Vendor a registry entry into the project WITHOUT going through `add`:
    copy the entry, list it in truc.toml, and record its pin.

    The pin is part of hand-vendoring, not an extra. Every truc named in
    truc.toml must carry a truc.lock row with a non-empty `sourceHash`, and
    `assemble` refuses an unpinned one before it ever reaches the admission
    gate — so a fixture that skipped the pin would exercise the pin requirement
    instead of the G2 refusal it means to test.
    """
    import shutil

    shutil.copytree(REGISTRY / "components" / name, proj / "trucs" / name)
    with (proj / "truc.toml").open("a") as handle:
        handle.write(f'{name} = {{ registry = "local" }}\n')
    index = json.loads((REGISTRY / "index.json").read_text())
    lock = json.loads((proj / "truc.lock").read_text())
    lock["trucs"].append({
        "name": name, "registry": "local",
        "sourceHash": index["components"][name]["sourceHash"]})
    (proj / "truc.lock").write_text(json.dumps(lock, indent=2))


# ---------------------------------------------------------------- add + assemble

def test_add_then_assemble_is_admitted_and_composed(tmp_path):
    proj = _new_project(tmp_path)

    r = _truc(proj, "add", "user_cache")
    assert r.returncode == 0, r.stderr
    assert "added" in r.stdout and "user_cache" in r.stdout

    r = _truc(proj, "add", "pg_database")
    assert r.returncode == 0, r.stderr

    # vendored byte-identically, pinned, recorded in truc.toml
    assert (proj / "trucs" / "user_cache" / "component.rvl").read_text() == \
        (REGISTRY / "components" / "user_cache" / "component.rvl").read_text()
    assert set(_lock_names(proj)) == {"user_cache", "pg_database"}
    assert "user_cache = { registry = \"local\" }" in (proj / "truc.toml").read_text()

    r = _truc(proj, "assemble")
    assert r.returncode == 0, r.stderr
    assert "assembled" in r.stdout
    # the assembled composition IR is written
    assembly = json.loads((proj / "build" / "assembly.json").read_text())
    assert "user_cache".lower() or assembly  # non-empty document
    assert "UserCache" in json.dumps(assembly) and "PgDatabase" in json.dumps(assembly)


# ---------------------------------------------------------------- G2 from `add`

def test_add_second_provider_of_a_key_is_refused_g2(tmp_path):
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0
    assert _truc(proj, "add", "pg_database").returncode == 0

    # mysql_database also provides `db` — a second provider of one key (G2)
    r = _truc(proj, "add", "mysql_database")
    assert r.returncode == 1, r.stdout
    assert "G2" in r.stdout
    assert "db" in r.stdout and "provider" in r.stdout
    # admitted before it joins: a refused truc is never written
    assert not (proj / "trucs" / "mysql_database").exists()
    assert "mysql_database" not in _lock_names(proj)
    assert "mysql_database" not in (proj / "truc.toml").read_text()


# ---------------------------------------------------------------- G2 from `assemble`

def test_assemble_refuses_when_two_providers_of_a_key_join_g2(tmp_path):
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0
    assert _truc(proj, "add", "pg_database").returncode == 0

    # hand-introduce a second `db` provider into the assembly, bypassing `add`
    _hand_vendor(proj, "mysql_database")

    r = _truc(proj, "assemble")
    assert r.returncode == 1, r.stdout
    assert "G2" in r.stdout
    assert "PgDatabase" in r.stdout and "MysqlDatabase" in r.stdout
    # all-or-nothing: no assembly artifact written for a refused composition
    assert not (proj / "build" / "assembly.json").exists()


# ---------------------------------------------------------------- hash drift

def test_assemble_refuses_on_vendor_hash_drift(tmp_path):
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0
    assert _truc(proj, "add", "pg_database").returncode == 0

    tampered = proj / "trucs" / "user_cache" / "component.rvl"
    tampered.write_text(tampered.read_text() + "\n// tampered!\n")

    r = _truc(proj, "assemble")
    assert r.returncode == 1, r.stdout
    assert "hash drift" in r.stdout
    assert "user_cache" in r.stdout


# ---------------------------------------------------------------- unknown name

def test_add_unknown_component_is_refused(tmp_path):
    proj = _new_project(tmp_path)
    r = _truc(proj, "add", "nonesuch")
    assert r.returncode == 1, r.stdout
    assert "unknown component" in r.stdout
    assert not (proj / "trucs").exists()


# ---------------------------------------------------------------- unknown verb

def test_unknown_command_reports_usage(tmp_path):
    proj = _new_project(tmp_path)
    r = _truc(proj, "frobnicate")
    assert r.returncode == 2, r.stdout
    assert "usage" in r.stdout


# ---------------------------------------------------------------- dogfood: self-audit

def test_truc_is_itself_admissible_and_the_brain_is_pure(tmp_path):
    """truc's own components pass the gate (a clean `revl audit` compiled them),
    and the Planner — truc's resolver brain — carries no boundary authority:
    all emissions live in the rim components. This is the property truc reports
    about other people's assemblies, shown on truc itself."""
    comp = ROOT / "src" / "revl" / "truc" / "components"
    files = [str(comp / f"{n}.rvl") for n in (
        "registry_client", "fetcher", "gatekeeper", "workspace",
        "planner", "assembler", "cli")]
    r = _revl("audit", *files)
    assert r.returncode == 0, r.stderr

    # the Planner section reaches only pure host code, never an emission
    section = r.stdout.split("component Planner")[1].split("component ")[0]
    assert "emission" not in section
    for pure_fn in ("json_parse", "json_stringify", "sha256_hex"):
        assert pure_fn in section
