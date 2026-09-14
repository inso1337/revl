"""`restore` re-checks the sidecar it is about to install (issue #1016).

`stdlib/fs.rvl`'s `restore` is the canonical inverse of the witnessed `write`.
It used to prove two things about the file it installed — that the SLOT is a
preimage sidecar this workspace owns (`resolve_sidecar`, item 422 F1) and that
the target is confined — and nothing at all about the FILE sitting in that slot.
A snapshot lives in `.revl-fs-preimage` for the whole life of an activation, so
a same-UID writer inside the workspace had that whole window to rewrite it, swap
it for another inode, or hardlink it out; the inverse then renamed the result
over the target and the abort reported a clean, residue-free reversal.

This is the inverse-path member of the family the forward path already has: the
write receipts bind facts to the ORIGINAL held descriptor (`original_receipt` /
`expect_existing`, issue #523) and `confirm_landed` re-establishes the written
inode's identity after the write (item 431(b)). `snapshot_preimage` now captures
the sidecar's own identity, the witness carries it, and `install_captured_sidecar`
re-checks it on the way in and re-checks the installed result on the way out.

The failure direction is the point, and both halves are pinned here:

* a sidecar that cannot be shown to be the captured one RAISES, which the
  teardown loop records as `restore-residue` through the existing merged-residue
  Record schema — it is never silently installed over the target;
* an untampered restore stays exactly as quiet as it always was (the non-vacuity
  control, which passes before this change as well as after it).
"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

from revl.compiler import compile_files

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the witnessed fs teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh`",
)

# The real module, compiled once, exactly as tests/test_fs_stdlib.py does it:
# the bodies under test are the ones stdlib/fs.rvl really ships, not a fixture.
_BASE = compile_files([str(_ROOT / "stdlib" / "fs.rvl")])


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


def _fs_module():
    """The emitted py module for stdlib/fs.rvl, so the real `write`/`restore`
    `@py` bodies can be called directly."""
    import emit  # noqa: PLC0415  (backends/python on path)
    ir = _ir(_component("Probe", [_effect("write", "artifact.txt", "x")]))
    module = types.ModuleType("fs_identity_probe_mod")
    sys.modules["fs_identity_probe_mod"] = module
    exec(compile(emit.emit(ir), "fs_identity_probe_mod.py", "exec"), module.__dict__)
    return module


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "artifact.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


def _written(mod, workspace):
    """Perform the witnessed write and hand back (witness, target, sidecar)."""
    result = mod.write("artifact.txt", "v2")
    assert isinstance(result, mod.Ok)
    witness = result.value
    sidecar = witness["preimage"]
    assert os.path.dirname(sidecar) == str(workspace / ws.PREIMAGE_DIRNAME)
    assert open(sidecar).read() == "v1"
    return witness, str(workspace / "artifact.txt"), sidecar


# ===========================================================================
# The non-vacuity control: an honest restore is unchanged and stays quiet.
# This test passes BEFORE the check existed as well as after it — without it,
# every assertion below could be satisfied by an inverse that refuses always.
# ===========================================================================

def test_an_untampered_restore_still_succeeds_silently(workspace):
    mod = _fs_module()
    witness, target, sidecar = _written(mod, workspace)
    assert open(target).read() == "v2"

    mod.restore(witness)                       # no raise, no diagnostics

    assert open(target).read() == "v1", "the honest preimage was not restored"
    assert not os.path.exists(sidecar), "the restore left its snapshot behind"
    assert not any((workspace / ws.PREIMAGE_DIRNAME).iterdir()), \
        "reversal is residue-free: the preimage directory is empty again"

    mod.restore(witness)                       # still idempotent on replay
    assert open(target).read() == "v1"


def test_a_created_file_is_still_deleted_by_its_inverse(workspace):
    """The `created` arm takes no sidecar at all, so the check must not reach
    it: a witness with an empty `preimage` still deletes the created file."""
    mod = _fs_module()
    result = mod.write("fresh.txt", "hello")
    assert isinstance(result, mod.Ok) and result.value["created"] is True
    mod.restore(result.value)
    assert not os.path.exists(str(workspace / "fresh.txt"))
    mod.restore(result.value)                  # idempotent


def test_a_witness_that_carries_no_capture_still_restores(workspace):
    """A durable record written before the capture key existed (an older
    install's WAL, replayed by `revl recover`) has nothing to compare against.
    It keeps the two caller-independent checks and restores; refusing it would
    strand a recoverable WAL, which is over-refusal, not hardening."""
    mod = _fs_module()
    witness, target, sidecar = _written(mod, workspace)
    legacy = {"path": witness["path"], "preimage": witness["preimage"],
              "created": witness["created"]}
    assert "capture" not in legacy

    mod.restore(legacy)

    assert open(target).read() == "v1"
    assert not os.path.exists(sidecar)


# ===========================================================================
# The three tampered sidecars. Each one FAILS on main (the sidecar is renamed
# over the target and the reversal reports success); each one is refused here.
# ===========================================================================

def test_a_tampered_sidecar_is_refused_rather_than_installed(workspace):
    """Rewritten IN PLACE, so the inode is the very one that was captured and
    the length is unchanged. `(dev, ino)` alone cannot see this, which is why
    the capture records the stamps as well."""
    mod = _fs_module()
    witness, target, sidecar = _written(mod, workspace)
    before = os.stat(sidecar)
    with open(sidecar, "r+b") as fh:
        fh.write(b"zz")                        # same inode, same length
    after = os.stat(sidecar)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

    with pytest.raises(ws.FsOpError) as ei:
        mod.restore(witness)
    assert ei.value.code == "EIDENTITY"

    assert open(target).read() == "v2", \
        "the tampered snapshot was installed over the target anyway"
    assert open(sidecar).read() == "zz", "the refused sidecar was consumed"


def test_a_swapped_sidecar_is_refused_rather_than_installed(workspace):
    """A different inode in the same slot: same name, same bytes even, but not
    the file that was captured."""
    mod = _fs_module()
    witness, target, sidecar = _written(mod, workspace)
    decoy = workspace / "decoy"
    decoy.write_text("v1", encoding="utf-8")   # same BYTES as the snapshot
    os.replace(str(decoy), sidecar)
    assert open(sidecar).read() == "v1"

    with pytest.raises(ws.FsOpError) as ei:
        mod.restore(witness)
    assert ei.value.code == "EIDENTITY", \
        "a same-bytes inode swap is exactly what an identity check is for"
    assert open(target).read() == "v2"
    assert os.path.exists(sidecar)


def test_a_relinked_sidecar_is_refused_rather_than_installed(workspace):
    """A second hardlink to the snapshot. Installing it would hand the caller a
    file another name still points at, so a later write through the restored
    target reaches the aliased inode — the same hazard the forward `write`
    refuses with `EMULTILINK`."""
    mod = _fs_module()
    witness, target, sidecar = _written(mod, workspace)
    alias = workspace / "alias"
    os.link(sidecar, str(alias))
    assert os.stat(sidecar).st_nlink == 2

    with pytest.raises(ws.FsOpError) as ei:
        mod.restore(witness)
    assert ei.value.code == "EMULTILINK"
    assert open(target).read() == "v2"
    assert alias.exists(), "the aliased snapshot was renamed away"


def test_a_sidecar_replaced_by_a_directory_is_refused(workspace):
    """Not a regular file at all. `resolve_sidecar` admits the slot; only a type
    check on the file itself refuses this."""
    mod = _fs_module()
    witness, target, sidecar = _written(mod, workspace)
    os.unlink(sidecar)
    os.mkdir(sidecar)

    with pytest.raises(ws.FsOpError) as ei:
        mod.restore(witness)
    assert ei.value.code == "EOUTSIDE"
    assert open(target).read() == "v2"


def test_the_installed_result_is_rechecked_after_the_rename(workspace, monkeypatch):
    """The check before the rename cannot be the only one: the rename is by
    NAME, so a writer that wins the window between them would have its file
    installed under a check that passed on someone else's. The installed result
    is re-checked for the same reason `confirm_landed` re-establishes the
    forward write's identity afterwards rather than trusting the pre-syscall
    check. The window is forced here rather than raced."""
    mod = _fs_module()
    witness, target, sidecar = _written(mod, workspace)
    real_replace = ws.replace_confined

    def _lose_the_window(src_real: str, dst_real: str) -> None:
        real_replace(src_real, dst_real)
        intruder = workspace / "intruder"
        intruder.write_text("v1", encoding="utf-8")
        os.replace(str(intruder), dst_real)    # a different inode, same bytes

    monkeypatch.setattr(ws, "replace_confined", _lose_the_window)

    with pytest.raises(ws.FsOpError) as ei:
        mod.restore(witness)
    assert ei.value.code == "EIDENTITY"
    assert "restored target" in ei.value.message or "restored" in ei.value.message


# ===========================================================================
# The report: a refusal reaches the audit surface as `restore-residue`, through
# the merged residue Record schema that already exists (item 247 gap 2 /
# docs/design/teardown-contract.md), with no new verdict vocabulary.
# ===========================================================================

def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


@needs_cordis
def test_a_drifted_sidecar_aborts_as_restore_residue(workspace, monkeypatch):
    """End to end on live cordis: the activation writes, a same-UID writer
    rewrites the snapshot while the activation is still running, and the abort's
    reversal refuses. The crossing is reported as `restore-residue` — the kind
    item 243 rule 6 already defines for a witnessed inverse that cannot
    complete — and the world is left with the residue the record names, rather
    than with a silently installed foreign file."""
    real_confirm = ws.confirm_landed

    def _tamper_during_the_activation(handle):
        real_confirm(handle)
        if handle.preimage:                    # a competing writer, mid-window
            with open(handle.preimage, "r+b") as fh:
                fh.write(b"zz")

    monkeypatch.setattr(ws, "confirm_landed", _tamper_during_the_activation)

    session = _session()
    report = session.load(_ir(_component(
        "Agent", [_effect("write", "artifact.txt", "v2")], abort=True)))
    assert report["components"] == [{"name": "Agent", "state": "FAILED"}]

    frame = _sole_frame(session)
    [residue] = [r for r in frame.compensation_residue
                 if r["kind"] == "restore-residue"]
    assert residue["state"] == "unresolved"
    assert residue["method"] == "restore"
    assert "EIDENTITY" in repr(residue["error"])

    # and the drifted snapshot was NOT installed: the abort left the forward
    # mutation in place and named it, which is what residue means.
    assert (workspace / "artifact.txt").read_text() == "v2"


@needs_cordis
def test_an_untampered_abort_reports_no_residue_at_all(workspace):
    """The control for the one above: the same activation with nobody touching
    the snapshot reverts cleanly and reports nothing. Passes before this change
    as well as after it."""
    session = _session()
    report = session.load(_ir(_component(
        "Agent", [_effect("write", "artifact.txt", "v2")], abort=True)))
    assert report["components"] == [{"name": "Agent", "state": "FAILED"}]

    frame = _sole_frame(session)
    assert frame.compensation_residue == []
    assert (workspace / "artifact.txt").read_text() == "v1"
    assert not any((workspace / ws.PREIMAGE_DIRNAME).iterdir())
