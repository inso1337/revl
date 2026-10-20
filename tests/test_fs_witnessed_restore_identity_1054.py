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

Issue #2647 added one more comparison, on both install stages: the sha256 of the
sidecar's bytes, recorded at snapshot and re-read through the same fd the stat
came from, before the rename and again on the installed result. The seven stat
fields alone do not see a same-size in-place rewrite that lands inside one inode
timestamp tick; the digest does, because it does not depend on clock resolution.
The digest comparison is skipped when the capture carries none, so a witness
recorded before the key existed (an older WAL replayed by `revl recover`) is
compared stat-only and still restores.

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
  Record schema, and it is never installed over the target;
* an untampered guarded restore stays exactly as quiet as it was, and a witness
  that carries no capture, or a capture recorded before the digest existed,
  still restores rather than stranding a recoverable WAL (the non-vacuity
  controls, which pass before this change as well as after).

The same-tick test at the head of the substitution section forces the capture's
stat fields to the post-tamper tuple before the inverse runs, so it does not
depend on the filesystem's tick at all: on a clock that did tick, the stat
comparison would refuse anyway; with the tuple pinned, only the digest can.
"""

from __future__ import annotations

import hashlib
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
    assert target.read_text(encoding="utf-8") == after
    assert os.path.dirname(sidecar) == str(root / ws.PREIMAGE_DIRNAME)
    assert open(sidecar, encoding="utf-8").read() == before
    return witness, str(target), sidecar


def _as_if_same_tick(witness, path):
    """Pin the witness's recorded stat fields to `path`'s CURRENT tuple.

    This is the same-tick case of issue #2647, forced rather than raced: after a
    same-size in-place rewrite inside one inode timestamp tick, every captured
    stat field equals the tampered file's own tuple. Overwriting the recorded
    fields with that tuple therefore removes the stat comparison from the test
    without removing the tamper, so anything that still refuses can only be the
    content digest. On a filesystem whose clock did tick this leaves a test that
    passes for one reason instead of a test that flips with the tick."""
    for field, value in ws._capture_of(os.stat(path)).items():
        witness["capture"][field] = value
    return witness


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
    # issue #2647: the content digest is bound alongside the stat fields, and it
    # is the digest of the bytes that were on disk, taken at snapshot time.
    digest = capture[ws.SIDECAR_DIGEST_FIELD]
    assert isinstance(digest, str) and len(digest) == 64
    assert digest == hashlib.sha256(Path(sidecar).read_bytes()).hexdigest()


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

    # nothing was bound by either refusal
    assert frame._transactional == []


def test_the_guarded_surface_refuses_a_target_the_confinement_check_cannot_open(
        root, frame):
    """The third kind of target the forward guard refuses: a mode that denies
    the confinement check its open. Split from the two above so a process that
    reads through a missing read bit (root, CAP_DAC_OVERRIDE) skips only this
    case, as tests/test_estop_443.py does, and not the whole probe."""
    unreadable = root / "unreadable.txt"
    unreadable.write_text("v1", encoding="utf-8")
    os.chmod(str(unreadable), 0o200)
    try:
        if os.access(str(unreadable), os.R_OK):
            pytest.skip("cannot revoke read permission (running as root)")
        with pytest.raises(fs.FsOpError) as ei:
            fs.write_witnessed("unreadable.txt", "x")
        assert ei.value.code == "EOUTSIDE"
    finally:
        os.chmod(str(unreadable), 0o600)

    # nothing was bound by the refusal
    assert frame._transactional == []


# ===========================================================================
# The portable copy path. Every host without the APFS clone takes it (all Linux
# and Windows, non-APFS macOS), so the digest has to be captured there too.
# ===========================================================================

def test_the_portable_copy_path_records_the_sidecar_digest(root, frame,
                                                          monkeypatch):
    """The APFS clone path kept this evidence green on the one host that has it.
    Everywhere else, all Linux and Windows plus non-APFS macOS, the snapshot
    takes the portable pread/write copy. That copy opened the sidecar
    `O_WRONLY`, so capturing the digest read a write-only fd and failed
    `EBADF`: the write refused `ESNAPSHOT` and no portable-path witness ever
    reached a restore. Forcing the fallback drives the copy path on every host,
    so this fails before the sidecar fd is readable and passes after."""
    monkeypatch.setattr(ws, "_clone_from_fd", lambda *a, **k: False)

    witness, target, sidecar = _written(root, frame)   # ordinary overwrite

    digest = witness["capture"][ws.SIDECAR_DIGEST_FIELD]
    assert isinstance(digest, str) and len(digest) == 64
    assert digest == hashlib.sha256(Path(sidecar).read_bytes()).hexdigest()

    # The recorded digest is live evidence, not a dead field: the same witness
    # restores the bytes the portable path snapshotted.
    fs._witnessed_restore(witness)
    assert open(target).read() == "v1"


# ===========================================================================
# The non-vacuity controls. Each passes BEFORE this change as well as after it;
# without them every assertion below could be met by an inverse that always
# refuses.
# ===========================================================================

def test_an_untampered_guarded_restore_still_succeeds_silently(root, frame):
    witness, target, sidecar = _written(root, frame)

    fs._witnessed_restore(witness)             # no raise, no diagnostics

    assert open(target, encoding="utf-8").read() == "v1", "the honest preimage was not restored"
    assert not os.path.exists(sidecar), "the restore left its snapshot behind"
    assert not any((root / ws.PREIMAGE_DIRNAME).iterdir()), \
        "reversal is residue-free: the preimage directory is empty again"

    fs._witnessed_restore(witness)             # still idempotent on replay
    assert open(target, encoding="utf-8").read() == "v1"


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

    assert open(target, encoding="utf-8").read() == "v1"
    assert not os.path.exists(sidecar)


def test_a_legitimate_restore_still_succeeds_with_the_digest_recorded(root, frame):
    """The positive direction of the digest check: a snapshot nobody touched
    restores, so the comparison refuses substitutions rather than honest
    reversals. Pinned as its own case because the digest is the one check that
    reads the sidecar's bytes on every restore."""
    witness, target, sidecar = _written(root, frame)
    digest = witness["capture"][ws.SIDECAR_DIGEST_FIELD]
    assert digest == hashlib.sha256(Path(sidecar).read_bytes()).hexdigest()

    fs._witnessed_restore(witness)             # no raise, no diagnostics

    assert open(target).read() == "v1"
    assert not os.path.exists(sidecar)


def test_a_capture_recorded_before_the_digest_still_restores(root, frame):
    """A capture with the stat fields but no `sha256` predates the digest (an
    older install's WAL, replayed by `revl recover`). It is compared stat-only
    and still restores: adding the key must not turn a recoverable WAL into a
    refusal."""
    witness, target, sidecar = _written(root, frame)
    legacy = dict(witness["capture"])
    legacy.pop(ws.SIDECAR_DIGEST_FIELD, None)
    assert "sha256" not in legacy
    assert set(ws.SIDECAR_CAPTURE_FIELDS) <= set(legacy)

    fs._witnessed_restore({**witness, "capture": legacy})

    assert open(target).read() == "v1"
    assert not os.path.exists(sidecar)


def test_a_forged_recorded_digest_on_an_honest_sidecar_is_refused(root, frame):
    """The record is forged, the file is not: the sidecar still holds the bytes
    the snapshot wrote, and only the recorded sha256 is changed to another
    well-formed digest. A compare path that trusted the record, or that treated
    a well-formed digest as unverifiable, would install the honest bytes and
    report a clean reversal; the refusal here proves the recorded value is
    actually compared with the bytes read through the sidecar fd. The forged
    value is 64 lowercase hex, so it takes the mismatch branch rather than the
    malformed-record one."""
    witness, target, sidecar = _written(root, frame)
    forged = dict(witness["capture"])
    forged[ws.SIDECAR_DIGEST_FIELD] = "0" * 64
    assert forged[ws.SIDECAR_DIGEST_FIELD] != \
        witness["capture"][ws.SIDECAR_DIGEST_FIELD]
    assert hashlib.sha256(Path(sidecar).read_bytes()).hexdigest() == \
        witness["capture"][ws.SIDECAR_DIGEST_FIELD]

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore({**witness, "capture": forged})

    assert ei.value.code == "EIDENTITY"
    assert ws.SIDECAR_DIGEST_FIELD in ei.value.message, \
        "the refusal must name the digest that did not match"
    assert "sha256 differ" in ei.value.message, \
        "a well-formed forged digest must take the mismatch branch, not the malformed-record one"
    assert open(target, encoding="utf-8").read() == "v2", \
        "a forged record must not install the honest sidecar"
    assert open(sidecar, encoding="utf-8").read() == "v1", \
        "the refused sidecar was consumed"


# ===========================================================================
# The recorded digest must BE a digest. A present null, number, list or
# malformed string used to fall through to stat-only, so a corrupt capture
# degraded the one check that does not depend on the clock into no check at all.
# ===========================================================================

@pytest.mark.parametrize("recorded", [
    None,
    7,
    ["a" * 64],
    True,
    "abc",                                    # too short
    "z" * 64,                                 # not hex
    "A" * 64,                                 # uppercase, not lowercase hex
], ids=["null", "number", "list", "bool", "short", "non-hex", "uppercase"])
def test_a_present_invalid_recorded_digest_is_refused(root, frame, recorded):
    """A capture that says it recorded a digest and then carries something that
    is not one is a corrupt record, not a legacy one. Treating it as absent
    would silently drop the only same-tick check, so it is refused as
    `EIDENTITY` rather than compared stat-only."""
    witness, target, sidecar = _written(root, frame)
    witness["capture"][ws.SIDECAR_DIGEST_FIELD] = recorded

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)

    assert ei.value.code == "EIDENTITY"
    assert ws.SIDECAR_DIGEST_FIELD in ei.value.message
    assert "not 64 lowercase hex" in ei.value.message, \
        "the refusal must name the malformed record, not a digest mismatch"
    assert open(target).read() == "v2", \
        "an invalid recorded digest must not degrade to an install"
    assert open(sidecar).read() == "v1", "the refused sidecar was consumed"


def test_an_unreadable_sidecar_is_refused_as_an_identity_failure(root, frame):
    """A sidecar whose bytes this process cannot read cannot be shown to be the
    captured one. On main the `EACCES` escaped `_checked_stat` as a raw
    `OSError`, which the guard totality wrapper tagged `EACCES` rather than the
    `EIDENTITY` residue the inverse contract names."""
    witness, target, sidecar = _written(root, frame)
    os.chmod(sidecar, 0o200)
    try:
        if os.access(sidecar, os.R_OK):
            pytest.skip("cannot revoke read permission (running as root)")
        with pytest.raises(fs.FsOpError) as ei:
            fs._witnessed_restore(witness)
        assert ei.value.code == "EIDENTITY"
        assert "preimage sidecar" in ei.value.message
        assert "could not be read" in ei.value.message
        assert open(target).read() == "v2", \
            "an unreadable sidecar must not be installed"
    finally:
        os.chmod(sidecar, 0o600)
    assert open(sidecar).read() == "v1", "the refused sidecar was consumed"


# ===========================================================================
# The four substitutions. Each one FAILS on main — the sidecar is renamed over
# the target and the reversal reports success — and is refused here.
# ===========================================================================

def test_a_tampered_sidecar_is_refused_rather_than_installed(root, frame):
    """Rewritten IN PLACE, so the inode is the very one that was captured and
    the length is unchanged. `(dev, ino)` alone cannot see this, and on a
    filesystem whose inode clock is coarse neither can `size`/`mtime_ns`/
    `ctime_ns`: the rewrite lands in the same timestamp tick and every captured
    stat field stays equal (issue #2647). The recorded stat tuple is therefore
    pinned to the tampered file's own tuple before the inverse runs, so the tick
    is out of the test and the only comparison left that can refuse is the
    recorded sha256. With the digest check removed the test fails to raise at
    all, because the pinned stat tuple no longer differs."""
    witness, target, sidecar = _written(root, frame)
    before = os.stat(sidecar)
    with open(sidecar, "r+b") as fh:
        fh.write(b"zz")                        # same inode, same length
    after = os.stat(sidecar)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert before.st_size == after.st_size
    assert witness["capture"][ws.SIDECAR_DIGEST_FIELD] == \
        hashlib.sha256(b"v1").hexdigest()
    _as_if_same_tick(witness, sidecar)

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY"
    assert ws.SIDECAR_DIGEST_FIELD in ei.value.message, \
        "the stat tuple was pinned to the tampered file, so only the digest " \
        "can have refused"

    assert open(target, encoding="utf-8").read() == "v2", \
        "the tampered snapshot was installed over the target anyway"
    assert open(sidecar, encoding="utf-8").read() == "zz", "the refused sidecar was consumed"


def test_a_rewrite_between_the_check_and_the_rename_is_refused(root, frame,
                                                               monkeypatch):
    """The second install pass, on the installed result, compares the digest too.

    The window the post-install pass exists for is between the pre-rename read
    and the rename: a writer can rewrite the sidecar in place after its bytes
    were hashed. That is forced by wrapping `replace_confined`, so the pre-rename
    comparison sees the captured bytes and the installed result does not. The
    recorded stat tuple is pinned to the rewritten file's own tuple, so the
    same-tick case holds and the post-install digest is the only comparison that
    can refuse."""
    witness, target, sidecar = _written(root, frame)
    real_replace = ws.replace_confined

    def rewrite_then_replace(src_real, dst_real):
        with open(src_real, "r+b") as fh:
            fh.write(b"zz")                    # same inode, same length
        _as_if_same_tick(witness, src_real)
        real_replace(src_real, dst_real)

    monkeypatch.setattr(ws, "replace_confined", rewrite_then_replace)

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY"
    assert ws.SIDECAR_DIGEST_FIELD in ei.value.message, \
        "the stat tuple was pinned, so only the installed digest can have refused"


def test_a_repermission_between_the_digest_check_and_the_rename_is_refused(
        root, frame, monkeypatch):
    """The digest cannot see a mode change: re-permissioning the sidecar leaves
    every byte, size and inode field equal. The window is forced inside the
    rename, after the pre-rename pass has read and compared the digest, so the
    installed result is the only pass that can see it. `INSTALLED_CAPTURE_FIELDS`
    keeps `mode` precisely so the digest does not replace the permission
    control, and the refusal must name the field that drifted rather than the
    digest. `ctime_ns` also drifts, and is dropped on that pass because the
    rename bumps it; the mode is the actionable field here."""
    witness, target, sidecar = _written(root, frame)
    real_replace = ws.replace_confined

    def chmod_then_replace(src_real, dst_real):
        current = stat.S_IMODE(os.stat(src_real).st_mode)
        os.chmod(src_real, 0o600 if current != 0o600 else 0o640)
        real_replace(src_real, dst_real)

    monkeypatch.setattr(ws, "replace_confined", chmod_then_replace)

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY"
    assert "restored target" in ei.value.message, \
        "only the installed-result pass can see a mode change made inside the rename"
    assert "mode" in ei.value.message, \
        "the digest cannot see a re-permission, so the refusal must name mode"
    assert ws.SIDECAR_DIGEST_FIELD not in ei.value.message, \
        "the bytes were honest; only the mode differed"


def test_a_swapped_sidecar_is_refused_rather_than_installed(root, frame):
    """A different inode in the same slot: same name, same bytes even, but not
    the file that was captured."""
    witness, target, sidecar = _written(root, frame)
    decoy = root / "decoy"
    decoy.write_text("v1", encoding="utf-8")   # the same BYTES as the snapshot
    os.replace(str(decoy), sidecar)
    assert open(sidecar, encoding="utf-8").read() == "v1"

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY", \
        "a same-bytes inode swap is exactly what an identity check is for"
    assert open(target, encoding="utf-8").read() == "v2"
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
    assert open(target, encoding="utf-8").read() == "v2"
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
    assert open(target, encoding="utf-8").read() == "v2"
    assert stat.S_ISDIR(os.stat(sidecar).st_mode)


def test_a_stat_drift_is_refused_before_the_sidecar_bytes_are_read(root, frame,
                                                                  monkeypatch):
    """A replacement can be enormous or sparse, so a sidecar that already
    differs on the fstat must be refused before its bytes are read. The digest
    read used to happen first, which let whoever replaced the sidecar choose the
    work the refusal does. The sparse file here would read for ages; the fstat
    mismatch names it first."""
    witness, target, sidecar = _written(root, frame)
    os.unlink(sidecar)
    with open(sidecar, "wb") as fh:
        fh.truncate(1 << 40)                   # sparse: allocates no data blocks

    def _must_not_read(fd):
        raise AssertionError(
            "read the replacement sidecar before the fstat mismatch refused")

    monkeypatch.setattr(ws, "_sha256_of_fd", _must_not_read)

    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(witness)
    assert ei.value.code == "EIDENTITY"
    assert "ino" in ei.value.message or "size" in ei.value.message
    assert open(target).read() == "v2"


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
    assert open(target, encoding="utf-8").read() == "v2"


def test_an_untampered_abort_reports_no_residue_at_all(root, frame):
    """The control for the one above: the same abort with nobody touching the
    snapshot reverts cleanly and reports nothing. Passes before this change as
    well as after it."""
    witness, target, sidecar = _written(root, frame)
    entry = frame._transactional[-1]

    frame._committed = False
    frame._guard(entry)()

    assert frame.compensation_residue == []
    assert open(target, encoding="utf-8").read() == "v1"
    assert not any((root / ws.PREIMAGE_DIRNAME).iterdir())
