"""`unrm` re-checks the sidecar it is about to install (issue #1038).

The `unrm` half of the family issue #1016 closed for `restore` (PR #1037).
`stdlib/fs.rvl`'s `unrm` is the canonical inverse of the witnessed `rm`, and it
reaches its sidecar through a SEPARATE witness and a SEPARATE inverse, so none
of the preimage-side check applied to it. It proved two things about the file it
installed back — that the SLOT is a garbage sidecar this workspace owns
(`resolve_sidecar`, item 422 F1) and that the target is confined — and nothing
at all about the FILE sitting in that slot. A parked file lives in
`.revl-fs-garbage` for the whole life of an activation, so a same-UID writer
inside the workspace had that whole window to rewrite it, swap it for another
inode, hardlink it out, or replace it with something that is not a file; the
inverse then renamed the result back over the original path and the abort
reported a clean, residue-free reversal.

`park_captured_sidecar` now records the parked file's identity, the witness
carries it as a superset `capture` key, and `install_parked_sidecar` re-checks
it on the way in and re-checks the installed result on the way out, with the
same three codes the preimage side spells (`EOUTSIDE`, `EMULTILINK`,
`EIDENTITY`).

The failure direction is the point, and both halves are pinned here:

* a sidecar that cannot be shown to be the parked one RAISES, which the teardown
  loop records as `restore-residue` through the existing merged-residue Record
  schema — it is never silently installed back over the target;
* an untampered `unrm` stays exactly as quiet as it always was, including for
  the cases the preimage side can refuse outright and this one must not: a
  parked DIRECTORY and a parked file that already carried a second hardlink
  (those controls pass before this change as well as after it).
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
# the bodies under test are the ones stdlib/fs.rvl really ships.
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
    """The emitted py module for stdlib/fs.rvl, so the real `rm`/`unrm` `@py`
    bodies can be called directly."""
    import emit  # noqa: PLC0415  (backends/python on path)
    ir = _ir(_component("Probe", [_effect("rm", "doomed.txt")]))
    module = types.ModuleType("fs_unrm_identity_probe_mod")
    sys.modules["fs_unrm_identity_probe_mod"] = module
    exec(compile(emit.emit(ir), "fs_unrm_identity_probe_mod.py", "exec"),
         module.__dict__)
    return module


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "doomed.txt").write_text("payload", encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


def _removed(mod, workspace, name: str = "doomed.txt"):
    """Perform the witnessed rm and hand back (witness, target, sidecar)."""
    result = mod.rm(name)
    assert isinstance(result, mod.Ok)
    witness = result.value
    sidecar = witness["garbage"]
    assert os.path.dirname(sidecar) == str(workspace / ws.GARBAGE_DIRNAME)
    assert not os.path.exists(str(workspace / name))
    return witness, str(workspace / name), sidecar


# ===========================================================================
# The non-vacuity controls. These pass BEFORE the check existed as well as
# after it — without them, every assertion below could be satisfied by an
# inverse that refuses always.
# ===========================================================================

def test_an_untampered_unrm_still_succeeds_silently(workspace):
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)

    mod.unrm(witness)                          # no raise, no diagnostics

    assert open(target).read() == "payload", "the parked file was not put back"
    assert not os.path.exists(sidecar), "the unrm left its sidecar behind"
    assert not any((workspace / ws.GARBAGE_DIRNAME).iterdir()), \
        "reversal is residue-free: the garbage directory is empty again"

    mod.unrm(witness)                          # still idempotent on replay
    assert open(target).read() == "payload"


def test_a_witness_that_carries_no_capture_still_unrms(workspace):
    """A durable record written before the capture key existed (an older
    install's WAL, replayed by `revl recover`) has nothing to compare against.
    It keeps the caller-independent symlink refusal and installs; refusing it
    would strand a recoverable WAL, which is over-refusal, not hardening."""
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)
    legacy = {"path": witness["path"], "garbage": witness["garbage"]}
    assert "capture" not in legacy

    mod.unrm(legacy)

    assert open(target).read() == "payload"
    assert not os.path.exists(sidecar)


def test_an_honest_rm_of_a_directory_still_reverses(workspace):
    """The over-refusal control, and the reason this inverse cannot copy the
    preimage side's flat `S_ISREG` assertion. `rm` parks a directory as readily
    as a file, so a check that demanded a regular file would refuse an honest
    reversal. The capture says `directory`, the sidecar IS a directory, so it
    installs."""
    mod = _fs_module()
    (workspace / "tree").mkdir()
    (workspace / "tree" / "leaf").write_text("inside", encoding="utf-8")

    witness, target, sidecar = _removed(mod, workspace, "tree")
    assert os.path.isdir(sidecar)

    mod.unrm(witness)

    assert os.path.isdir(target)
    assert open(os.path.join(target, "leaf")).read() == "inside"


def test_an_honest_rm_of_an_already_hardlinked_file_still_reverses(workspace):
    """The other over-refusal control. `rm` accepts a file that already carries
    a second name, so a flat `nlink == 1` demand would refuse an honest
    reversal. What is refused is a link ADDED since the park, which the capture
    is what makes visible."""
    mod = _fs_module()
    (workspace / "shared.txt").write_text("two names", encoding="utf-8")
    os.link(str(workspace / "shared.txt"), str(workspace / "alias.txt"))
    assert os.stat(str(workspace / "shared.txt")).st_nlink == 2

    witness, target, sidecar = _removed(mod, workspace, "shared.txt")
    assert os.stat(sidecar).st_nlink == 2

    mod.unrm(witness)

    assert open(target).read() == "two names"


# ===========================================================================
# The tampered sidecars. Each one FAILS on main (the sidecar is renamed back
# over the original path and the reversal reports success); each is refused here.
# ===========================================================================

def test_a_tampered_sidecar_is_refused_rather_than_installed(workspace):
    """Rewritten IN PLACE, so the inode is the very one that was parked and the
    length is unchanged. `(dev, ino)` alone cannot see this, which is why the
    capture records the stamps as well."""
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)
    before = os.stat(sidecar)
    with open(sidecar, "r+b") as fh:
        fh.write(b"POISON!")                   # same inode, same length
    after = os.stat(sidecar)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

    with pytest.raises(ws.FsOpError) as ei:
        mod.unrm(witness)
    assert ei.value.code == "EIDENTITY"

    assert not os.path.exists(target), \
        "the tampered sidecar was installed back over the original path anyway"
    assert open(sidecar).read() == "POISON!", "the refused sidecar was consumed"


def test_a_swapped_sidecar_is_refused_rather_than_installed(workspace):
    """A different inode in the same slot: same name, same bytes even, but not
    the file that was parked."""
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)
    decoy = workspace / "decoy"
    decoy.write_text("payload", encoding="utf-8")   # same BYTES as the parked
    os.replace(str(decoy), sidecar)
    assert open(sidecar).read() == "payload"

    with pytest.raises(ws.FsOpError) as ei:
        mod.unrm(witness)
    assert ei.value.code == "EIDENTITY", \
        "a same-bytes inode swap is exactly what an identity check is for"
    assert not os.path.exists(target)
    assert os.path.exists(sidecar)


def test_a_relinked_sidecar_is_refused_rather_than_installed(workspace):
    """A second hardlink to the parked file, added after the park. Installing it
    would hand the caller a file another name still points at, so a later write
    through the restored path reaches the aliased inode — the same hazard the
    forward `write` refuses with `EMULTILINK`."""
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)
    assert os.stat(sidecar).st_nlink == 1
    alias = workspace / "alias"
    os.link(sidecar, str(alias))
    assert os.stat(sidecar).st_nlink == 2

    with pytest.raises(ws.FsOpError) as ei:
        mod.unrm(witness)
    assert ei.value.code == "EMULTILINK"
    assert not os.path.exists(target)
    assert alias.exists(), "the aliased sidecar was renamed away"


def test_a_sidecar_replaced_by_a_directory_is_refused(workspace):
    """Not the kind of thing that was parked at all. `resolve_sidecar` admits
    the slot; only a check on the file itself refuses this."""
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)
    os.unlink(sidecar)
    os.mkdir(sidecar)

    with pytest.raises(ws.FsOpError) as ei:
        mod.unrm(witness)
    assert ei.value.code == "EOUTSIDE"
    assert not os.path.exists(target)


def test_a_sidecar_replaced_by_a_symlink_is_refused(workspace):
    """The one refusal that needs no capture: `rm` never parks a symlink,
    because `resolve_within` realpaths the leaf before the rename. So a symlink
    in the slot is a substitution whatever the witness carries."""
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)
    (workspace / "elsewhere").write_text("payload", encoding="utf-8")
    os.unlink(sidecar)
    os.symlink(str(workspace / "elsewhere"), sidecar)

    with pytest.raises(ws.FsOpError) as ei:
        mod.unrm(witness)
    assert ei.value.code == "EOUTSIDE"
    assert not os.path.exists(target)


def test_the_installed_result_is_rechecked_after_the_rename(workspace, monkeypatch):
    """The check before the rename cannot be the only one: the rename is by
    NAME, so a writer that wins the window between them would have its file
    installed under a check that passed on someone else's. The installed result
    is re-checked for the same reason `confirm_landed` re-establishes the
    forward write's identity afterwards rather than trusting the pre-syscall
    check. The window is forced here rather than raced."""
    mod = _fs_module()
    witness, target, sidecar = _removed(mod, workspace)
    real_replace = ws.replace_confined
    swapped = {"done": False}

    def _lose_the_window(src_real: str, dst_real: str) -> None:
        real_replace(src_real, dst_real)
        if swapped["done"]:
            return
        swapped["done"] = True
        intruder = workspace / "intruder"
        intruder.write_text("payload", encoding="utf-8")
        os.replace(str(intruder), dst_real)    # a different inode, same bytes

    monkeypatch.setattr(ws, "replace_confined", _lose_the_window)

    with pytest.raises(ws.FsOpError) as ei:
        mod.unrm(witness)
    assert ei.value.code == "EIDENTITY"
    assert "unremoved target" in ei.value.message


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
    """End to end on live cordis: the activation removes a file, a same-UID
    writer rewrites the parked sidecar while the activation is still running,
    and the abort's reversal refuses. The crossing is reported as
    `restore-residue` — the kind item 243 rule 6 already defines for a witnessed
    inverse that cannot complete — and the world is left with the residue the
    record names, rather than with a silently installed foreign file."""
    real_lexists = ws.lexists_confined
    tampered = {"done": False}

    def _tamper_before_the_inverse(real: str) -> bool:
        """A same-UID writer that wins the window between the crossing and the
        abort. Hooked on a helper BOTH trees call, so the test exercises the
        same race with and without the check."""
        answer = real_lexists(real)
        if answer and not tampered["done"] and ws.GARBAGE_DIRNAME in real:
            tampered["done"] = True
            with open(real, "r+b") as fh:
                fh.write(b"POISON!")
        return answer

    monkeypatch.setattr(ws, "lexists_confined", _tamper_before_the_inverse)

    session = _session()
    report = session.load(_ir(_component(
        "Agent", [_effect("rm", "doomed.txt")], abort=True)))
    assert report["components"] == [{"name": "Agent", "state": "FAILED"}]

    frame = _sole_frame(session)
    [residue] = [r for r in frame.compensation_residue
                 if r["kind"] == "restore-residue"]
    assert residue["state"] == "unresolved"
    assert residue["method"] == "unrm"
    assert "EIDENTITY" in repr(residue["error"])

    # and the drifted sidecar was NOT installed: the abort left the forward
    # mutation in place and named it, which is what residue means. On main the
    # rewritten sidecar is renamed back over the original path and the abort
    # reports nothing at all.
    assert not (workspace / "doomed.txt").exists()
    assert tampered["done"], "the tamper never fired, so the test proves nothing"


@needs_cordis
def test_an_untampered_abort_reports_no_residue_at_all(workspace):
    """The control for the one above: the same activation with nobody touching
    the sidecar reverts cleanly and reports nothing. Passes before this change
    as well as after it."""
    session = _session()
    report = session.load(_ir(_component(
        "Agent", [_effect("rm", "doomed.txt")], abort=True)))
    assert report["components"] == [{"name": "Agent", "state": "FAILED"}]

    frame = _sole_frame(session)
    assert frame.compensation_residue == []
    assert (workspace / "doomed.txt").read_text() == "payload"
    assert not any((workspace / ws.GARBAGE_DIRNAME).iterdir())
