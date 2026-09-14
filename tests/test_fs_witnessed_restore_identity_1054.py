"""`write_witnessed`'s host inverse reads the capture it binds (issue #1054).

`revl.fs.write_witnessed` is the guarded host-side witnessed write, and
`revl.fs._witnessed_restore` is the inverse its effect replays on abort. Issue
#1016 gave the canonical `stdlib/fs.rvl` `restore` an identity check on the
preimage sidecar it installs, and #1038 gave `unrm` the same check on its
garbage sidecar. This surface was the third caller of the same pattern and the
only one in the worst state: `witnessed_write_record` bound the `capture` key
onto the witness, and the inverse called `replace_confined` with no identity
check at all, so the evidence was recorded and never read. A reader seeing the
key on the witness would reasonably assume it was being used.

The inverse now installs through `install_captured_sidecar`, the same machinery
and the same three codes (`EOUTSIDE`, `EMULTILINK`, `EIDENTITY`), checked at
syscall time and re-checked on the installed result with `ctime_ns` dropped
because the rename bumps it.

The constants are load-bearing and are pinned by probe here rather than assumed.
#1038 found that copying #1016's "regular file" and "one link" assertions onto
`unrm` would have over-refused honest reversals, because `rm` parks directories,
mode-0200 files and already-hardlinked files. This surface is the other case:
`open_confined_write` refuses every one of those before a preimage exists, and
the sidecar comes from the very same `snapshot_preimage`, so the two constants
hold. `test_the_guarded_surface_admits_only_regular_single_linked_targets`
records that, so a later widening of the forward guard fails here first instead
of silently turning the check into an over-refusal.

The failure direction is the point, and both halves are pinned:

* a preimage that cannot be shown to be the captured one RAISES, which the
  teardown loop records as `restore-residue` through the existing merged-residue
  Record schema — it is never installed over the target;
* an untampered guarded restore stays exactly as quiet as it was, and a witness
  that carries no capture still restores rather than stranding a recoverable
  WAL (the non-vacuity controls, which pass before this change as well as after).
"""

from __future__ import annotations

import os
import stat
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import runtime  # noqa: E402
import revl_fs_workspace as ws  # noqa: E402

from revl import fs  # noqa: E402


class _FakeWal:
    """The smallest WAL that gives `Frame.transactional` a durable seq to bind,
    exactly as tests/test_fs_witnessed_receipt_binding_623.py uses."""

    def __init__(self):
        self.records = []

    def record_discharge_descriptor(self, kind, **kw):
        seq = len(self.records)
        self.records.append(dict(kw, kind=kind, seq=seq))
        return {"seq": seq}

    def record_fence(self, seq):   # pragma: no cover - declared idempotent
        self.records.append({"kind": "fence", "seq": seq})


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ws"
    r.mkdir()
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(r))
    return r


@pytest.fixture
def frame(monkeypatch):
    """A live witnessed session: a registered `SessionOwner` plus one live
    `Frame` with a WAL attached, so `write_witnessed` binds for real."""
    owner = runtime.SessionOwner()
    owner.session_id = "sess-1054"
    runtime.set_session_owner(owner)
    ctx = types.SimpleNamespace(
        _revl_timeline=types.SimpleNamespace(_wal=_FakeWal()))
    f = runtime.Frame(ctx, "Agent")          # auto-registers into owner
    f._owner = owner
    try:
        yield f
    finally:
        runtime.clear_session_owner()


def _written(root, frame, name="artifact.txt", before="v1", after="v2"):
    """Perform the guarded witnessed write; hand back (witness, target, sidecar)."""
    target = root / name
    target.write_text(before, encoding="utf-8")
    fs.write_witnessed(name, after)
    witness = frame._transactional[-1].witness
    sidecar = witness["preimage"]
    assert target.read_text() == after
    assert os.path.dirname(sidecar) == str(root / ws.PREIMAGE_DIRNAME)
    assert open(sidecar).read() == before
    return witness, str(target), sidecar


# ===========================================================================
# What the bound key is, and what the guarded surface actually admits. Both
# pass on either tree; the second is what licenses the two constants.
# ===========================================================================

def test_the_witness_binds_a_capture_the_inverse_can_read(root, frame):
    witness, target, sidecar = _written(root, frame)
    capture = witness["capture"]
    st = os.stat(sidecar)
    assert capture["ino"] == st.st_ino and capture["dev"] == st.st_dev
    assert set(ws.SIDECAR_CAPTURE_FIELDS) <= set(capture)
    assert "ctime_ns" not in ws.INSTALLED_CAPTURE_FIELDS


def test_the_guarded_surface_admits_only_regular_single_linked_targets(root, frame):
    """The probe behind the constants. `install_captured_sidecar` asserts that
    the sidecar is a regular file with one link rather than deriving those from
    the capture the way `unrm` must (#1038). That is only honest because the
    forward guard refuses every target that would produce a different kind of
    snapshot, so there is no honest reversal to over-refuse. If the forward
    guard is ever widened, this fails before the inverse starts over-refusing."""
    (root / "adir").mkdir()
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_witnessed("adir", "x")
    assert ei.value.code == "ENOTFILE"

    linked = root / "linked.txt"
    linked.write_text("v1", encoding="utf-8")
    os.link(str(linked), str(root / "alias.txt"))
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_witnessed("linked.txt", "x")
    assert ei.value.code == "EMULTILINK"

    unreadable = root / "unreadable.txt"
    unreadable.write_text("v1", encoding="utf-8")
    os.chmod(str(unreadable), 0o200)
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_witnessed("unreadable.txt", "x")
    assert ei.value.code == "EOUTSIDE"
    os.chmod(str(unreadable), 0o600)

    # nothing was bound by any of the three refusals
    assert frame._transactional == []


# ===========================================================================
# The non-vacuity controls. Each passes BEFORE this change as well as after it;
# without them every assertion below could be met by an inverse that always
# refuses.
# ===========================================================================

def test_an_untampered_guarded_restore_still_succeeds_silently(root, frame):
    witness, target, sidecar = _written(root, frame)

    fs._witnessed_restore(witness)             # no raise, no diagnostics

    assert open(target).read() == "v1", "the honest preimage was not restored"
    assert not os.path.exists(sidecar), "the restore left its snapshot behind"
    assert not any((root / ws.PREIMAGE_DIRNAME).iterdir()), \
        "reversal is residue-free: the preimage directory is empty again"

    fs._witnessed_restore(witness)             # still idempotent on replay
    assert open(target).read() == "v1"


def test_a_created_target_is_still_deleted_by_its_inverse(root, frame):
    """The `created` arm takes no sidecar at all, so the check must not reach
    it: a witness with an empty `preimage` still deletes the created file."""
    fs.write_witnessed("fresh.txt", "hello")
    witness = frame._transactional[-1].witness
    assert witness["created"] is True and not witness["preimage"]

    fs._witnessed_restore(witness)
    assert not os.path.exists(str(root / "fresh.txt"))
    fs._witnessed_restore(witness)             # idempotent


def test_a_witness_that_carries_no_capture_still_restores(root, frame):
    """A durable record written before the capture key was read here (an older
    install's WAL, replayed by `revl recover`) has nothing to compare against.
    It keeps the two caller-independent checks and restores; refusing every such
    replay would strand a recoverable WAL, which is over-refusal, not
    hardening."""
    witness, target, sidecar = _written(root, frame)
    legacy = {k: v for k, v in witness.items() if k != "capture"}
    assert "capture" not in legacy

    fs._witnessed_restore(legacy)

    assert open(target).read() == "v1"
    assert not os.path.exists(sidecar)


# ===========================================================================
# The four substitutions. Each one FAILS on main — the sidecar is renamed over
# the target and the reversal reports success — and is refused here.
# ===========================================================================

def test_a_tampered_sidecar_is_refused_rather_than_installed(root, frame):
    """Rewritten IN PLACE, so the inode is the very one that was captured and
    the length is unchanged. `(dev, ino)` alone cannot see this, which is why
    the capture records the stamps too."""
    witness, target, sidecar = _written(root, frame)
    before = os.stat(sidecar)
    with open(sidecar, "r+b") as fh:
        fh.write(b"zz")                        # same inode, same length
    after = os.stat(sidecar)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY"

    assert open(target).read() == "v2", \
        "the tampered snapshot was installed over the target anyway"
    assert open(sidecar).read() == "zz", "the refused sidecar was consumed"


def test_a_swapped_sidecar_is_refused_rather_than_installed(root, frame):
    """A different inode in the same slot: same name, same bytes even, but not
    the file that was captured."""
    witness, target, sidecar = _written(root, frame)
    decoy = root / "decoy"
    decoy.write_text("v1", encoding="utf-8")   # the same BYTES as the snapshot
    os.replace(str(decoy), sidecar)
    assert open(sidecar).read() == "v1"

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY", \
        "a same-bytes inode swap is exactly what an identity check is for"
    assert open(target).read() == "v2"
    assert os.path.exists(sidecar)


def test_a_relinked_sidecar_is_refused_rather_than_installed(root, frame):
    """A second hardlink to the snapshot. Installing it would hand the caller a
    file another name still points at, so a later write through the restored
    target reaches the aliased inode — the hazard the forward `write` already
    refuses with `EMULTILINK`."""
    witness, target, sidecar = _written(root, frame)
    alias = root / "alias"
    os.link(sidecar, str(alias))
    assert os.stat(sidecar).st_nlink == 2

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EMULTILINK"
    assert open(target).read() == "v2"
    assert alias.exists(), "the aliased snapshot was renamed away"


def test_a_sidecar_replaced_by_a_directory_is_refused(root, frame):
    """Not a regular file at all. `resolve_sidecar` admits the slot; only a type
    check on the file itself refuses this. On main the rename failed with a raw
    `ENOTDIR` from the syscall rather than naming the substitution."""
    witness, target, sidecar = _written(root, frame)
    os.unlink(sidecar)
    os.mkdir(sidecar)

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EOUTSIDE"
    assert open(target).read() == "v2"
    assert stat.S_ISDIR(os.stat(sidecar).st_mode)


def test_the_installed_result_is_rechecked_after_the_rename(root, frame, monkeypatch):
    """The check before the rename cannot be the only one: the rename is by
    NAME, so a writer that wins the window between them would have its file
    installed under a check that passed on someone else's. The installed result
    is re-checked for the same reason `confirm_landed` re-establishes the
    forward write's identity afterwards rather than trusting its pre-syscall
    check. The window is forced here rather than raced."""
    witness, target, sidecar = _written(root, frame)
    real_replace = ws.replace_confined

    def _lose_the_window(src_real: str, dst_real: str) -> None:
        real_replace(src_real, dst_real)
        intruder = root / "intruder"
        intruder.write_text("v1", encoding="utf-8")
        os.replace(str(intruder), dst_real)    # a different inode, same bytes

    monkeypatch.setattr(ws, "replace_confined", _lose_the_window)

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY"
    assert "restored target" in ei.value.message or "restored" in ei.value.message


# ===========================================================================
# The report: a refusal reaches the audit surface as `restore-residue`, through
# the merged residue Record schema that already exists (item 243 rule 6,
# docs/design/teardown-contract.md), with no new verdict vocabulary.
# ===========================================================================

def test_a_drifted_sidecar_aborts_as_restore_residue(root, frame):
    """End to end on the real teardown seam: the guarded write binds its effect
    on the live frame, a same-UID writer rewrites the snapshot while the
    activation is still running, and the abort's reversal refuses. The raise is
    caught by the frame's Phase-1 continue-and-record chokepoint (`_guard`, the
    one every disposer passes through) and recorded as `restore-residue`; the
    session-level `collect_compensation_residue` merges it. The world is left
    with the residue the record names rather than a silently installed foreign
    file."""
    witness, target, sidecar = _written(root, frame)
    entry = frame._transactional[-1]
    with open(sidecar, "r+b") as fh:           # a competing writer, mid-window
        fh.write(b"zz")

    frame._committed = False                   # the activation aborts
    frame._guard(entry)()                      # never re-raises: it records

    [residue] = [r for r in frame.compensation_residue
                 if r["kind"] == "restore-residue"]
    assert residue["state"] == "unresolved"
    assert residue["outcome"] == "failed"
    assert "EIDENTITY" in residue["error"]["message"]
    assert residue in frame._owner.collect_compensation_residue()

    # and the drifted snapshot was NOT installed: the abort left the forward
    # mutation in place and named it, which is what residue means.
    assert open(target).read() == "v2"


def test_an_untampered_abort_reports_no_residue_at_all(root, frame):
    """The control for the one above: the same abort with nobody touching the
    snapshot reverts cleanly and reports nothing. Passes before this change as
    well as after it."""
    witness, target, sidecar = _written(root, frame)
    entry = frame._transactional[-1]

    frame._committed = False
    frame._guard(entry)()

    assert frame.compensation_residue == []
    assert open(target).read() == "v1"
    assert not any((root / ws.PREIMAGE_DIRNAME).iterdir())
