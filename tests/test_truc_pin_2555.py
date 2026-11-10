"""truc pins the bytes it vendors, not the registry index (#2555).

`truc add` plans on one read of the registry entry: the planner checks its
`component.rvl` against the index row's `sourceHash` and the gate admits that
text. `commit_add` then read the entry again to vendor it, so the bytes
written could differ from what was checked; it also copied `sourceHash` and
`manifestHash` out of a second read of the index row, and pinned no dossier
hash at all, so a registry that replaced an entry and its index row together
between plan and commit was vendored and pinned as if it had been admitted.
`assemble` compared only `component.rvl` with its pin; a vendored
`manifest.json` or `dossier.json` was never checked, and `assemble --check`
reported neither.

The fix: the commit plan carries the sha256 of every file the planner read and
the gate admitted ("" for a file the entry does not ship). `commit_add` hashes
the text it is about to write and refuses unless it equals the admitted hash,
even when the commit-time index row was rewritten to agree with it. Vendored
files that drift from their pin are refused by `assemble`, and reported by
`assemble --check` without writing.

The host half drives `_host.commit_add`, `_host.write_assembly` and
`_host.vendored_drift`, the entry points the `truc add`, `truc assemble` and
`truc assemble --check` verbs end in, against a throwaway copy of the repo
registry, and needs no runtime. The console half drives the real `truc`
command and skips without the cordis-py runtime (never reported as passing).
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from revl.truc import _host
from test_truc_add_assemble import CORDIS_PY, REGISTRY, _new_project, _truc

ENTRY = "user_cache"
#: the composition manifest a clean gate verdict hands `write_assembly`
ADMITTED = json.dumps({"components": [], "loadOrder": []})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _registry(tmp_path: Path, *, dossier: bool = True) -> Path:
    """A writable copy of the repo registry's index and ENTRY. `dossier` adds
    the optional third file every truc may ship."""
    reg = tmp_path / "reg"
    (reg / "components").mkdir(parents=True)
    shutil.copy(REGISTRY / "index.json", reg / "index.json")
    shutil.copytree(REGISTRY / "components" / ENTRY, reg / "components" / ENTRY)
    if dossier:
        (reg / "components" / ENTRY / "dossier.json").write_text('{"dossier": 1}\n')
    return reg


def _world(tmp_path: Path, *, dossier: bool = True) -> tuple[Path, Path]:
    reg = _registry(tmp_path, dossier=dossier)
    proj = _new_project(tmp_path)
    toml = proj / "truc.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(str(REGISTRY), str(reg)))
    return reg, proj


@pytest.fixture
def world(tmp_path):
    return _world(tmp_path)


def _admitted_plan(reg: Path, *, admission: str = "profiled") -> str:
    """The commit plan `plan_add_report` builds from the reads `truc add` makes:
    the sha256 of the text the planner checked and the gate admitted. This is
    the plan's whole job, so a test that hands `commit_add` anything less is not
    testing the add `truc add` performs."""
    row = json.loads(_host.index_row(str(reg), ENTRY))
    entry = json.loads(_host.entry_read(str(reg), ENTRY))
    assert _sha(entry["source"]) == row["sourceHash"]
    dossier = entry.get("dossier")
    return json.dumps({"lockAdd": {
        "name": ENTRY,
        "registry": "local",
        "admission": admission,
        "sourceHash": _sha(entry["source"]),
        "manifestHash": _sha(entry["manifest"]),
        "dossierHash": _sha(dossier) if dossier is not None else "",
    }})


def _lock_text(proj: Path) -> str | None:
    lock = proj / "truc.lock"
    return lock.read_text(encoding="utf-8") if lock.exists() else None


def _lock_row(proj: Path) -> dict:
    lock = json.loads((proj / "truc.lock").read_text(encoding="utf-8"))
    (row,) = [r for r in lock["trucs"] if r["name"] == ENTRY]
    return row


def _refused_commit(proj: Path, plan: str) -> str:
    before = _lock_text(proj)
    toml_before = (proj / "truc.toml").read_text(encoding="utf-8")
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.commit_add(str(proj), plan)
    # a refusal leaves nothing behind
    assert not (proj / "trucs" / ENTRY).exists()
    assert _lock_text(proj) == before
    assert (proj / "truc.toml").read_text(encoding="utf-8") == toml_before
    return str(raised.value)


def _added(world) -> Path:
    reg, proj = world
    assert _host.commit_add(str(proj), _admitted_plan(reg)) == "committed"
    return proj


def _refused_assembly(proj: Path) -> str:
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host.write_assembly(str(proj), ADMITTED)
    assert not (proj / "build" / "assembly.json").exists()
    return str(raised.value)


# ================================================================ the host half

def test_entry_replaced_between_plan_and_commit_is_refused(world):
    """The plan admitted one read; the commit's second read is a different
    entry. On base it was vendored and pinned anyway."""
    reg, proj = world
    plan = _admitted_plan(reg)
    source = reg / "components" / ENTRY / "component.rvl"
    source.write_text(source.read_text(encoding="utf-8")
                      + "\n// not the text the gate admitted\n")

    message = _refused_commit(proj, plan)
    assert ENTRY in message
    assert "changed between plan and commit" in message


def test_entry_and_index_replaced_together_are_refused(world):
    """The remaining gap: replace the entry AND rewrite its index hashes so the
    commit-time comparison against the index still passes. Only the plan's
    admitted hash can tell the two reads apart; on base this add committed the
    never-admitted bytes and recorded the rewritten index values as their pin.
    """
    reg, proj = world
    plan = _admitted_plan(reg)
    replaced = ('// a different component entirely\n'
                'service Other { fn go() -> Int }\n'
                'component OtherProvider provides other: Other {\n'
                '  provide other { fn go() = 1 }\n'
                '}\n')
    (reg / "components" / ENTRY / "component.rvl").write_text(replaced)
    index = json.loads((reg / "index.json").read_text(encoding="utf-8"))
    index["components"][ENTRY]["sourceHash"] = _sha(replaced)
    (reg / "index.json").write_text(json.dumps(index, indent=2))

    message = _refused_commit(proj, plan)
    assert ENTRY in message
    assert "changed between plan and commit" in message


def test_replaced_entry_is_not_admitted_by_its_planned_trust(world):
    """A plan admitted with `--trust-host-code` records that trust against the
    source hash it admitted. If the registry could swap in a host body and
    rewrite the index, the old code refused nothing and recorded the trust
    against the swapped bytes, which `assemble` then honoured without the flag.
    """
    reg, proj = world
    plan = _admitted_plan(reg, admission="trusted-host-code")
    replacing = ('service Danger { emission fn exec(cmd: Str) -> Int }\n'
                 'component DangerProvider provides danger: Danger {\n'
                 '  provide danger { fn exec(cmd) = emit host_print(cmd) }\n'
                 '}\n')
    (reg / "components" / ENTRY / "component.rvl").write_text(replacing)
    index = json.loads((reg / "index.json").read_text(encoding="utf-8"))
    index["components"][ENTRY]["sourceHash"] = _sha(replacing)
    (reg / "index.json").write_text(json.dumps(index, indent=2))

    message = _refused_commit(proj, plan)
    assert ENTRY in message
    assert "changed between plan and commit" in message


def test_index_hash_edited_after_plan_is_refused_not_pinned(world):
    """The defect: on base the edited index value is copied into the lock."""
    reg, proj = world
    plan = _admitted_plan(reg)
    index = json.loads((reg / "index.json").read_text(encoding="utf-8"))
    index["components"][ENTRY]["sourceHash"] = "0" * 64
    (reg / "index.json").write_text(json.dumps(index, indent=2))

    message = _refused_commit(proj, plan)
    assert ENTRY in message
    assert "changed between plan and commit" in message


def test_manifest_replaced_between_plan_and_commit_is_refused(world):
    """The registry's manifest.json changes after planning and its index row
    keeps the old `manifestHash`: the add is refused, not pinned."""
    reg, proj = world
    plan = _admitted_plan(reg)
    manifest = reg / "components" / ENTRY / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n")

    message = _refused_commit(proj, plan)
    assert ENTRY in message and "manifest.json" in message
    assert "changed between plan and commit" in message


def test_dossier_replaced_between_plan_and_commit_is_refused(world):
    """The remaining gap: the index carries no dossier hash, so on base the
    swapped dossier was vendored and pinned with no admission behind it."""
    reg, proj = world
    plan = _admitted_plan(reg)
    (reg / "components" / ENTRY / "dossier.json").write_text('{"dossier": 2}\n')

    message = _refused_commit(proj, plan)
    assert ENTRY in message and "dossier.json" in message
    assert "changed between plan and commit" in message


def test_dossier_appearing_after_plan_is_refused(tmp_path):
    """The plan asserts an absent dossier as absent: a dossier the entry did
    not ship at plan time is not silently vendored at commit."""
    reg, proj = _world(tmp_path, dossier=False)
    plan = _admitted_plan(reg)
    (reg / "components" / ENTRY / "dossier.json").write_text('{"dossier": 1}\n')

    message = _refused_commit(proj, plan)
    assert ENTRY in message and "dossier.json" in message
    assert "changed between plan and commit" in message


def test_a_re_add_drops_a_dossier_the_registry_no_longer_ships(world):
    """The other side of asserting an absent dossier: a re-add must remove the
    previous release's vendored dossier, or it would sit in `trucs/<name>/`
    unpinned (the new row has no `dossierHash`) and never checked."""
    reg, proj = world
    assert _host.commit_add(str(proj), _admitted_plan(reg)) == "committed"
    assert (proj / "trucs" / ENTRY / "dossier.json").exists()

    (reg / "components" / ENTRY / "dossier.json").unlink()
    assert _host.commit_add(str(proj), _admitted_plan(reg)) == "committed"

    assert not (proj / "trucs" / ENTRY / "dossier.json").exists()
    assert "dossierHash" not in _lock_row(proj)
    assert _host.write_assembly(str(proj), ADMITTED) == "written"


def test_index_row_removed_after_plan_is_refused(world):
    """A registry that drops the entry's index row between plan and commit is
    not the registry that admitted it: the add is refused, not written with
    empty metadata."""
    reg, proj = world
    plan = _admitted_plan(reg)
    index = json.loads((reg / "index.json").read_text(encoding="utf-8"))
    del index["components"][ENTRY]
    (reg / "index.json").write_text(json.dumps(index, indent=2))

    message = _refused_commit(proj, plan)
    assert ENTRY in message
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


def test_vendored_drift_reports_the_same_files_write_assembly_refuses(world):
    """`assemble --check` reads this value form instead of reaching
    `write_assembly` (a dry run writes nothing), so it must name the same drift.
    """
    proj = _added(world)
    assert json.loads(_host.vendored_drift(str(proj))) == {
        "ok": True, "message": ""}
    vendored = proj / "trucs" / ENTRY / "manifest.json"
    vendored.write_text(vendored.read_text(encoding="utf-8") + "\n")

    drift = json.loads(_host.vendored_drift(str(proj)))
    assert drift["ok"] is False
    assert "hash drift" in drift["message"]
    assert f"trucs/{ENTRY}/manifest.json" in drift["message"]


def test_vendored_drift_names_a_drifted_dossier(world):
    """The dossier is the file with no index hash, so the lock pin is the only
    record `assemble --check` can compare it with."""
    proj = _added(world)
    (proj / "trucs" / ENTRY / "dossier.json").write_text('{"dossier": 2}\n')

    drift = json.loads(_host.vendored_drift(str(proj)))
    assert drift["ok"] is False
    assert f"trucs/{ENTRY}/dossier.json" in drift["message"]


def test_unchanged_add_pins_the_vendored_bytes_and_assembles(world):
    """Control: an untouched entry adds, pins what was written, and assembles."""
    proj = _added(world)
    vendored = proj / "trucs" / ENTRY
    row = _lock_row(proj)
    assert row["sourceHash"] == _sha((vendored / "component.rvl").read_text(encoding="utf-8"))
    assert row["manifestHash"] == _sha((vendored / "manifest.json").read_text(encoding="utf-8"))
    assert row["dossierHash"] == _sha((vendored / "dossier.json").read_text(encoding="utf-8"))
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


@_cli
def test_cli_assemble_check_names_a_drifted_vendored_manifest(tmp_path):
    """The remaining gap: `assemble --check` wrote nothing and so never reached
    the host's drift check, reporting a drifted vendored manifest as valid."""
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0
    assert _truc(proj, "add", "pg_database").returncode == 0
    vendored = proj / "trucs" / "user_cache" / "manifest.json"
    vendored.write_text(vendored.read_text(encoding="utf-8") + "\n")

    r = _truc(proj, "assemble", "--check")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "invalid" in r.stdout + r.stderr
    assert "trucs/user_cache/manifest.json" in r.stdout + r.stderr
    assert not (proj / "build" / "assembly.json").exists()


@_cli
def test_cli_add_of_a_dossier_shipping_entry_pins_the_dossier(tmp_path):
    """The planner's `Some(text)` dossier branch is only reached by an entry
    that ships a dossier; the repo registry has none. Drive the real add, then
    a drifted vendored dossier through `assemble --check`."""
    reg = _registry(tmp_path)
    proj = _new_project(tmp_path)
    toml = proj / "truc.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(str(REGISTRY), str(reg)))

    r = _truc(proj, "add", ENTRY)
    assert r.returncode == 0, r.stdout + r.stderr
    vendored = proj / "trucs" / ENTRY / "dossier.json"
    assert vendored.read_text(encoding="utf-8") == '{"dossier": 1}\n'
    assert _lock_row(proj)["dossierHash"] == _sha(
        vendored.read_text(encoding="utf-8"))

    r = _truc(proj, "assemble", "--check")
    assert r.returncode == 0, r.stdout + r.stderr

    vendored.write_text('{"dossier": 2}\n')
    r = _truc(proj, "assemble", "--check")
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"trucs/{ENTRY}/dossier.json" in r.stdout + r.stderr
    assert not (proj / "build" / "assembly.json").exists()


@_cli
def test_cli_assemble_check_is_still_quiet_on_a_clean_assembly(tmp_path):
    proj = _new_project(tmp_path)
    assert _truc(proj, "add", "user_cache").returncode == 0

    r = _truc(proj, "assemble", "--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "valid" in r.stdout
    assert not (proj / "build" / "assembly.json").exists()
