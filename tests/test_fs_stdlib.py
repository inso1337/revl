"""The stdlib FS module — witnessed filesystem operations (roadmap item 244,
the H1 north-star demo; stdlib/fs.rvl, docs/witnessed-fs.md).

This suite is the H1 proof: a `component` performs witnessed fs ops (a `write`
and an `rm`) inside an activation, run on the LIVE cordis-py tier, and

  * on a clean commit the files STAY mutated (the mutation is the deliverable);
  * on an abort the writes/removes REVERT residue-free (files back to preimage,
    the removed file back in place, no snapshot/garbage left behind);
  * the WAL/recover surface ENUMERATES exactly the witnessed fs crossings;
  * a witnessed write OUTSIDE the workspace root is REFUSED — with all three
    confinement musts proven (realpath-before-check, inverse-path guarded,
    garbage + preimage snapshots inside the root).

# Why the call sites are hand-built IR (and what that does NOT weaken)

The witnessed runtime seam (item 243, Slice 2a) is proven by handing the py
runtime the exact transactional IR step a witnessed effect lowers to — see
tests/test_witnessed_runtime.py, which this suite mirrors. lower.py's call-site
enablement admits `effect write(...)` when `write` is a witnessed extern
declared IN THE SAME FILE (tests/test_witnessed_lower_callsite.py). The
cross-module surface — `use "stdlib/fs.rvl" { write }` then `effect write(...)`
in a consumer — is NOT yet supported: the parser gates the missing-`undo`
effect on a per-file `_witnessed_names` set (parser.py), populated only from
same-file `extern` decls, so an imported witnessed extern is unknown at parse
time. That is a frontend slice (the parse surface belongs with the lower.py /
245 work), not the runtime seam this item consumes. So H1 is proven the Slice
2a way: over the REAL stdlib/fs.rvl `@py` bodies (compiled from the actual
module file), with the component's witnessed call sites built as the IR the
future cross-module lowerer will emit. The fs bodies, the confinement guard,
and the transactional teardown are all exercised for real on live cordis; only
the consumer's surface syntax is stood in for.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import replay  # noqa: E402
import revl_fs_workspace as ws  # noqa: E402  (the confinement helper under test)
from revl.recovery import recover  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the witnessed fs teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh`",
)

# The real module, compiled once. `compile_source` of the actual file text
# yields the externs (write/rm/move/mkdir + their inverses) with the witnessed
# classification and lowered `undo` the runtime seam keys off.
_FS_SRC = (_ROOT / "stdlib" / "fs.rvl").read_text(encoding="utf-8")
_BASE = compile_source(_FS_SRC, "fs.rvl")


# ---------------------------------------------------------------------------
# IR helpers: a component whose activation calls witnessed fs externs in effect
# position (the shape lower.py emits; args are literals)
# ---------------------------------------------------------------------------

def _lit(v: str) -> dict:
    return {"kind": "lit", "value": v}


def _effect(name: str, *args: str) -> dict:
    return {"step": "effect",
            "acquire": {"kind": "fn", "name": name, "args": [_lit(a) for a in args]}}


def _component(name: str, body: list, abort: bool = False) -> dict:
    steps = list(body)
    if abort:
        steps.append({"step": "fail", "message": _lit("boom")})
    return {"name": name, "source": "fs.rvl", "config": [],
            "requires": {}, "provides": {}, "body": steps}


def _ir(component: dict) -> dict:
    ir = copy.deepcopy(_BASE)
    ir["components"] = [component]
    return ir


def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A session workspace root with a pre-existing artifact and a stale file.
    The `@py` bodies read `REVL_FS_WORKSPACE`; paths passed to the externs are
    taken relative to it."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "artifact.txt").write_text("v1", encoding="utf-8")
    (root / "stale.txt").write_text("junk", encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


# ===========================================================================
# The three confinement musts, exercised on the REAL emitted @py bodies + the
# helper (no cordis needed; these are the guard, proven directly)
# ===========================================================================

import types  # noqa: E402


def _fs_module():
    """The emitted py module for stdlib/fs.rvl, so the real `write`/`restore`/
    ... `@py` bodies can be called directly. Registered in `sys.modules` before
    exec so the emitted `Ok`/`Err` dataclasses can resolve their annotations."""
    import emit  # noqa: PLC0415  (backends/python on path)
    ir = _ir(_component("Probe", [_effect("write", "artifact.txt", "x")]))
    module = types.ModuleType("fs_probe_mod")
    sys.modules["fs_probe_mod"] = module
    exec(compile(emit.emit(ir), "fs_probe_mod.py", "exec"), module.__dict__)
    return module


def test_must1_realpath_before_check_catches_a_symlink_escape(workspace, tmp_path):
    # A symlink INSIDE the root that points OUTSIDE it must be caught: the guard
    # realpaths (resolves the link) BEFORE testing membership, so it cannot be
    # fooled into following the link out of the workspace.
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside)  # a link inside root -> outside

    with pytest.raises(ws.ConfinementError) as ei:
        ws.resolve_within("link/escape.txt")
    assert ei.value.code == "EOUTSIDE"

    # and end-to-end through the real @py write body: an Err, nothing written
    mod = _fs_module()
    result = mod.write("link/escape.txt", "pwned")
    assert isinstance(result, mod.Err)
    assert result.value["code"] == "EOUTSIDE"
    assert not (outside / "escape.txt").exists()


def test_must2_inverse_path_is_guarded(workspace, tmp_path):
    # The guard applies to the INVERSE path: a witness whose target is outside
    # the root cannot make restore/unrm/unmove write outside. (A witness only
    # goes rogue through tampering or a moved root; the inverse re-checks anyway
    # rather than trusting the recorded path.)
    mod = _fs_module()
    outside = str(tmp_path / "outside.txt")

    for undo, witness in (
        (mod.restore, {"path": outside, "preimage": "", "created": True}),
        (mod.unrm, {"path": outside, "garbage": str(workspace / ".revl-fs-garbage/x")}),
        (mod.unmove, {"from": outside, "to": str(workspace / "a.txt")}),
        (mod.rmdir_if_empty, {"path": outside}),
    ):
        with pytest.raises(ws.ConfinementError) as ei:
            undo(witness)
        assert ei.value.code == "EOUTSIDE"
    assert not os.path.exists(outside)


def test_must3_garbage_and_preimage_live_inside_the_root(workspace):
    root = os.path.realpath(str(workspace))
    assert ws.garbage_dir().startswith(root + os.sep)
    assert ws.preimage_dir().startswith(root + os.sep)

    # and an actual rm parks the file inside the root (not in /tmp, not beside
    # it): reversal never has to reach outside the workspace.
    mod = _fs_module()
    result = mod.rm("stale.txt")
    assert isinstance(result, mod.Ok)
    assert result.value["garbage"].startswith(root + os.sep)
    assert os.path.exists(result.value["garbage"])


def test_missing_root_is_refused_not_silently_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv(ws.WORKSPACE_ENV, raising=False)
    with pytest.raises(ws.ConfinementError) as ei:
        ws.resolve_within("anything.txt")
    assert ei.value.code == "EWORKSPACE"


# ===========================================================================
# The catalog round-trips: each op's inverse restores its preimage, and every
# inverse is idempotent on replay (item 243 rule 5)
# ===========================================================================

def test_write_restore_roundtrip_and_idempotent(workspace):
    mod = _fs_module()
    art = str(workspace / "artifact.txt")
    w = mod.write("artifact.txt", "v2")
    assert isinstance(w, mod.Ok)
    assert open(art).read() == "v2"          # mutated
    assert w.value["created"] is False
    mod.restore(w.value)
    assert open(art).read() == "v1"          # preimage back
    mod.restore(w.value)                      # idempotent: second replay no-ops
    assert open(art).read() == "v1"


def test_write_of_new_file_undo_deletes_it(workspace):
    mod = _fs_module()
    new = str(workspace / "fresh.txt")
    w = mod.write("fresh.txt", "hello")
    assert isinstance(w, mod.Ok) and w.value["created"] is True
    assert os.path.exists(new)
    mod.restore(w.value)
    assert not os.path.exists(new)            # created file removed
    mod.restore(w.value)                      # idempotent


def test_rm_unrm_roundtrip(workspace):
    mod = _fs_module()
    stale = str(workspace / "stale.txt")
    r = mod.rm("stale.txt")
    assert isinstance(r, mod.Ok) and not os.path.exists(stale)
    mod.unrm(r.value)
    assert os.path.exists(stale) and open(stale).read() == "junk"
    mod.unrm(r.value)                          # idempotent


def test_move_unmove_and_mkdir_rmdir(workspace):
    mod = _fs_module()
    m = mod.move("stale.txt", "moved.txt")
    assert isinstance(m, mod.Ok)
    assert os.path.exists(str(workspace / "moved.txt"))
    assert not os.path.exists(str(workspace / "stale.txt"))
    mod.unmove(m.value)
    assert os.path.exists(str(workspace / "stale.txt"))

    d = mod.mkdir("newdir")
    assert isinstance(d, mod.Ok) and os.path.isdir(str(workspace / "newdir"))
    mod.rmdir_if_empty(d.value)
    assert not os.path.exists(str(workspace / "newdir"))
    mod.rmdir_if_empty(d.value)                # idempotent


def test_fallible_ops_return_err_and_register_nothing(workspace):
    mod = _fs_module()
    assert isinstance(mod.rm("does-not-exist.txt"), mod.Err)      # ENOENT
    assert isinstance(mod.mkdir("artifact.txt"), mod.Err)         # EEXIST
    assert isinstance(mod.move("nope.txt", "there.txt"), mod.Err)  # ENOENT source


# ===========================================================================
# H1, LIVE on cordis: persist on commit, revert on abort, refuse outside root
# ===========================================================================

@needs_cordis
def test_h1_persists_on_clean_commit(workspace):
    art = workspace / "artifact.txt"
    stale = workspace / "stale.txt"
    session = _session()
    session.load(_ir(_component(
        "Agent", [_effect("write", "artifact.txt", "v2"),
                  _effect("rm", "stale.txt")])))

    # the activation applied both mutations
    assert art.read_text() == "v2"
    assert not stale.exists()
    frame = _sole_frame(session)
    assert len(frame._transactional) == 2      # write + rm registered

    session.unload()                            # clean unload == implicit commit

    # the mutations ARE the deliverable: they persist, inverses discharged
    assert art.read_text() == "v2", "commit wrongly reverted the write"
    assert not stale.exists(), "commit wrongly restored the removed file"
    for entry in frame._transactional:
        assert entry.discharged is True and entry.replayed is False
        assert entry.witness is None            # witness GC'd


@needs_cordis
def test_h1_reverts_residue_free_on_abort(workspace):
    art = workspace / "artifact.txt"
    stale = workspace / "stale.txt"
    session = _session()

    report = session.load(_ir(_component(
        "Agent", [_effect("write", "artifact.txt", "v2"),
                  _effect("rm", "stale.txt")], abort=True)))
    assert report["components"] == [{"name": "Agent", "state": "FAILED"}]

    # abort replayed both inverses: files back to their preimage
    assert art.read_text() == "v1", "abort did not restore the write preimage"
    assert stale.read_text() == "junk", "abort did not un-remove the file"
    # residue-free: the snapshot + parked file the mutations created are gone
    garbage = workspace / ws.GARBAGE_DIRNAME
    preimage = workspace / ws.PREIMAGE_DIRNAME
    assert not any(garbage.iterdir()) if garbage.exists() else True
    assert not any(preimage.iterdir()) if preimage.exists() else True


@needs_cordis
def test_h1_write_outside_root_is_refused_live(workspace, tmp_path):
    # a witnessed write whose resolved target escapes the root: the @py body
    # returns Err, so the accumulator registers NOTHING (Ok-conditional) and
    # the activation touches nothing outside the workspace.
    escapee = tmp_path / "escape.txt"
    session = _session()
    session.load(_ir(_component("Rogue", [_effect("write", "../escape.txt", "pwned")])))

    frame = _sole_frame(session)
    assert frame._transactional == [], "an outside-root write must register no inverse"
    assert not escapee.exists(), "confinement failed: a file was written outside the root"
    session.unload()


# ===========================================================================
# The residue/audit surface ENUMERATES exactly the witnessed fs crossings
# (through the landed WAL/recover foundation — consumed, not modified)
# ===========================================================================

def _wal_before_apply(monkeypatch, wal_path: str) -> None:
    """Open the WAL just before `Recorder.instrument`, mirroring
    tests/test_witnessed_runtime.py's bridge-slice hook."""
    real = replay.Recorder.instrument

    def _open_then(self, *a, **k):
        self.open_wal(wal_path, generation=1)
        return real(self, *a, **k)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then)


@needs_cordis
def test_residue_surface_enumerates_committed_crossings(workspace, tmp_path, monkeypatch):
    wal_path = str(tmp_path / "commit.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    session.load(_ir(_component(
        "Agent", [_effect("write", "artifact.txt", "v2"),
                  _effect("rm", "stale.txt")])), record=True)

    # every witnessed fs crossing is a durable WAL descriptor naming the inverse
    # + the witness data — this IS the exact enumeration surface
    written = replay.WriteAheadLog.read(wal_path)
    descriptors = [r for r in written["records"]
                   if r.get("record") == "discharge-descriptor"]
    methods = sorted(d["call"]["method"] for d in descriptors)
    assert methods == ["restore", "unrm"]        # write's + rm's inverses
    for d in descriptors:
        assert d["entry"] == "transactional"
        assert d["witness"] is not None          # WAL-serializable data, not a handle

    session.unload()                              # commit: discharge records written

    report = recover(wal_path)
    # a committed transaction is enumerated as SKIPPED, never rolled back
    assert report["verdict"] == "rolled-back"
    assert sorted(s["seq"] for s in report["dischargedSkipped"]) == \
        sorted(d["seq"] for d in descriptors)
    assert report["transactionalRolledBack"] == []
    assert report["residue"]["clean"] is True


@needs_cordis
def test_residue_surface_enumerates_aborted_crossings(workspace, tmp_path, monkeypatch):
    wal_path = str(tmp_path / "abort.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    report = session.load(_ir(_component(
        "Agent", [_effect("write", "artifact.txt", "v2"),
                  _effect("rm", "stale.txt")], abort=True)), record=True)
    assert report["components"] == [{"name": "Agent", "state": "FAILED"}]

    session.recorder.wal.close()                  # crash: no activation-complete

    loaded = replay.WriteAheadLog.read(wal_path)
    descriptors = [r for r in loaded["records"]
                   if r.get("record") == "discharge-descriptor"]
    assert not [r for r in loaded["records"] if r.get("record") == "discharge"]

    rec = recover(wal_path)
    assert rec["verdict"] == "rolled-back"
    # both crossings enumerated as rolled back
    assert sorted(s["seq"] for s in rec["transactionalRolledBack"]) == \
        sorted(d["seq"] for d in descriptors)
    assert rec["dischargedSkipped"] == []
