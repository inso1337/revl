"""truc pins the bytes it vendors, not the registry index (#2555).

`truc add` plans on one read of the registry entry: the planner checks its
`component.rvl` against the index row's `sourceHash` and the gate admits that
text. `commit_add` then copied a second read of the entry into `trucs/<name>/`
and copied `sourceHash` and `manifestHash` out of a second read of the index
row, so the bytes written and the hashes pinned could both differ from what
was checked. `assemble` compared only `component.rvl` with its pin; the
vendored `manifest.json` and `dossier.json` were never checked.

The host half drives `_host.commit_add` and `_host.write_assembly`, the entry
points the `truc add` and `truc assemble` verbs end in, against a throwaway
copy of the repo registry, and needs no runtime. The console half drives the
real `truc` command and skips without the cordis-py runtime (never reported
as passing).
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from revl.truc import _host
from test_truc_add_assemble import CORDIS_PY, REGISTRY, _new_project, _truc

ENTRY = "user_cache"
PLAN = json.dumps({"lockAdd": {"name": ENTRY, "registry": "local",
                               "admission": "profiled"}})
#: the composition manifest a clean gate verdict hands `write_assembly`
ADMITTED = json.dumps({"components": [], "loadOrder": []})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _registry(tmp_path: Path) -> Path:
    """A writable copy of the repo registry's index and ENTRY, with a dossier."""
    reg = tmp_path / "reg"
    (reg / "components").mkdir(parents=True)
    shutil.copy(REGISTRY / "index.json", reg / "index.json")
    shutil.copytree(REGISTRY / "components" / ENTRY, reg / "components" / ENTRY)
    (reg / "components" / ENTRY / "dossier.json").write_text('{"dossier": 1}\n')
    return reg


@pytest.fixture
def world(tmp_path):
    reg = _registry(tmp_path)
    proj = _new_project(tmp_path)
    toml = proj / "truc.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(str(REGISTRY), str(reg)))
    return reg, proj


def _plan(reg: Path) -> None:
    """The reads `truc add` plans on, and the planner's own check on them."""
    row = json.loads(_host.index_row(str(reg), ENTRY))
    entry = json.loads(_host.entry_read(str(reg), ENTRY))
    assert _sha(entry["source"]) == row["sourceHash"]


def _lock_text(proj: Path) -> str | None:
    lock = proj / "truc.lock"
    return lock.read_text(encoding="utf-8") if lock.exists() else None


def _lock_row(proj: Path) -> dict:
    lock = json.loads((proj / "truc.lock").read_text(encoding="utf-8"))
    (row,) = [r for r in lock["trucs"] if r["name"] == ENTRY]
    return row


def _refused_commit(proj: Path) -> str:
    before = _lock_text(proj)
    toml_before = (proj / "truc.toml").read_text(encoding="utf-8")
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.commit_add(str(proj), PLAN)
    # a refusal leaves nothing behind
    assert not (proj / "trucs" / ENTRY).exists()
    assert _lock_text(proj) == before
    assert (proj / "truc.toml").read_text(encoding="utf-8") == toml_before
    return str(raised.value)


def _added(world) -> Path:
    reg, proj = world
    _plan(reg)
    assert _host.commit_add(str(proj), PLAN) == "committed"
    return proj


def _refused_assembly(proj: Path) -> str:
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.write_assembly(str(proj), ADMITTED)
    assert not (proj / "build" / "assembly.json").exists()
    return str(raised.value)


# ================================================================ the host half

def test_entry_replaced_between_plan_and_commit_is_refused(world):
    """The defect: on base the second read is vendored and the index hash,
    which no longer describes it, is pinned."""
    reg, proj = world
    _plan(reg)
    source = reg / "components" / ENTRY / "component.rvl"
    source.write_text(source.read_text(encoding="utf-8") + "\n// not the text the gate admitted\n")

    message = _refused_commit(proj)
    assert ENTRY in message
    assert "changed between plan and commit" in message


def test_index_hash_edited_after_plan_is_refused_not_pinned(world):
    """The defect: on base the edited index value is copied into the lock."""
    reg, proj = world
    _plan(reg)
    index = json.loads((reg / "index.json").read_text(encoding="utf-8"))
    index["components"][ENTRY]["sourceHash"] = "0" * 64
    (reg / "index.json").write_text(json.dumps(index, indent=2))

    message = _refused_commit(proj)
    assert ENTRY in message
    assert "changed between plan and commit" in message


def test_manifest_replaced_between_plan_and_commit_is_refused(world):
    """The registry's manifest.json changes after planning and its index row
    keeps the old `manifestHash`: the add is refused, not pinned."""
    reg, proj = world
    _plan(reg)
    manifest = reg / "components" / ENTRY / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n")

    message = _refused_commit(proj)
    assert ENTRY in message and "manifest.json" in message
    assert "changed between plan and commit" in message


def test_vendored_manifest_edited_after_add_is_drift_at_assemble(world):
    """The defect: on base `manifestHash` is never compared at assemble."""
    proj = _added(world)
    vendored = proj / "trucs" / ENTRY / "manifest.json"
    vendored.write_text(vendored.read_text(encoding="utf-8") + "\n")

    message = _refused_assembly(proj)
    assert "hash drift" in message
    assert f"trucs/{ENTRY}/manifest.json" in message


def test_vendored_dossier_edited_after_add_is_drift_at_assemble(world):
    """The defect: on base the vendored dossier is pinned by nothing."""
    proj = _added(world)
    vendored = proj / "trucs" / ENTRY / "dossier.json"
    vendored.write_text('{"dossier": 2}\n')

    message = _refused_assembly(proj)
    assert "hash drift" in message
    assert f"trucs/{ENTRY}/dossier.json" in message


def test_unchanged_add_pins_the_vendored_bytes_and_assembles(world):
    """Control: an untouched entry adds, pins what was written, and assembles."""
    proj = _added(world)
    vendored = proj / "trucs" / ENTRY
    row = _lock_row(proj)
    assert row["sourceHash"] == _sha((vendored / "component.rvl").read_text(encoding="utf-8"))
    assert row["manifestHash"] == _sha((vendored / "manifest.json").read_text(encoding="utf-8"))
    assert _host.write_assembly(str(proj), ADMITTED) == "written"
    assert (proj / "build" / "assembly.json").exists()


# ============================================================= the console half

_cli = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


@_cli
def test_cli_assemble_names_a_drifted_vendored_manifest(tmp_path):
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0
    assert _truc(proj, "add", "pg_database").returncode == 0
    vendored = proj / "trucs" / "user_cache" / "manifest.json"
    vendored.write_text(vendored.read_text(encoding="utf-8") + "\n")

    r = _truc(proj, "assemble")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "trucs/user_cache/manifest.json" in r.stdout + r.stderr
    assert not (proj / "build" / "assembly.json").exists()
