"""The host-bound witnessed write replays its inverse on the runtime's OWN
abort path (issue #1071).

`revl.fs.write_witnessed` binds its inverse through `_ambient_bind`. That
registration used `Frame.transactional`, the ACTIVATION-BODY path, whose
contract is that the emitted body yields the returned disposer into cordis's
LIFO stack; the frame's `_transactional` list is introspection only, read by
`drain` for the WAL discharge record and by the residue/E-Stop inventories, and
never walked to run anything. A host write has no emitted body to yield from, so
the disposer went nowhere and the entry was unreachable at teardown.

The failure direction this pins is the SILENT one. Pre-fix, driving
`owner.begin_abort()` then `frame.drain()` over a host-bound `write_witnessed`
left the target at its forward mutation `v2`, reported no residue, and named no
refusal. Not a refusal, not a reported residue: a mutation that stays while the
abort reports success. That voids the temporal-composability guarantee the
witnessed surface sells, and it also hides the `restore-residue` reporting
#1037/#1052/#1070 added, which is only reached if the inverse runs at all.

The registration now goes through `Frame.transactional_method` — the runtime's
existing path (item 318, docs/design/243-witnessed-externs.md rule 5) for an
inverse with no body generator to yield into, which parks the entry in
`_deferred_transactional` where `drain` does read it. No new WAL version, no new
verdict vocabulary, no third settlement surface.

Every test here drives the runtime's own abort (`SessionOwner.begin_abort` +
`Frame.drain` + `SessionOwner.finalize_abort`) rather than hand-calling the
entry, which is what the existing #623 / #1054 suites do and why this never
surfaced there.

The non-vacuity controls are marked NON-VACUITY below: each one passes on BOTH
sides of the fix, so a green run is evidence the drive really exercises the
abort seam rather than evidence the seam does nothing.
"""

from __future__ import annotations

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
    """The smallest WAL that gives a registration a durable seq to bind, plus a
    fence log so the declared-idempotent claim can be checked."""

    def __init__(self):
        self.records = []
        self.fences = []

    def record_discharge_descriptor(self, kind, **kw):
        seq = len(self.records)
        self.records.append(dict(kw, kind=kind, seq=seq))
        return {"seq": seq}

    def record_fence(self, seq):
        self.fences.append(seq)

    def record_discharge(self, seqs):
        self.records.append({"kind": "discharge", "seqs": list(seqs)})

    def record_aborted(self, seqs):
        self.records.append({"kind": "aborted", "seqs": list(seqs)})


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ws"
    r.mkdir()
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(r))
    return r


@pytest.fixture
def session():
    """A live witnessed session: a registered `SessionOwner` plus one live
    `Frame` with a WAL attached. Yields `(owner, frame, wal)`."""
    wal = _FakeWal()
    owner = runtime.SessionOwner(wal_getter=lambda: wal)
    owner.session_id = "sess-1071"
    runtime.set_session_owner(owner)
    tl = types.SimpleNamespace(_wal=wal)
    ctx = types.SimpleNamespace(_revl_timeline=tl)
    frame = runtime.Frame(ctx, "Notes")
    try:
        yield owner, frame, wal
    finally:
        runtime.clear_session_owner()


def _abort(owner, frame):
    """Drive the runtime's OWN abort, in the order the driver uses: mark every
    live frame aborting BEFORE any teardown (Decision 5), tear the frame down,
    then settle the session."""
    owner.begin_abort()
    frame.drain()
    return owner.finalize_abort()


def _commit(owner, frame):
    """The same drive without the abort: a clean unload under a session owner
    that then commits."""
    owner._verdict = "commit"
    frame.drain()
    return owner.finalize_commit()


# ===========================================================================
# The gap: the abort drive replayed nothing, silently.
# ===========================================================================

def test_a_replacing_host_write_reverts_on_the_runtime_abort(root, session):
    """FAILS pre-fix: the target stays at `v2`.

    A host-bound witnessed write replaces `v1` with `v2`; the session aborts
    through its own `begin_abort` + `drain`. The preimage must be back."""
    owner, frame, _wal = session
    target = root / "notes.txt"
    target.write_text("v1")

    fs.write_witnessed("notes.txt", "v2")
    assert target.read_text() == "v2"

    _abort(owner, frame)

    assert target.read_text() == "v1"
    assert not any((root / ws.PREIMAGE_DIRNAME).iterdir())


def test_a_created_host_write_is_deleted_by_the_runtime_abort(root, session):
    """FAILS pre-fix: the created file survives the abort.

    The created-target half of the same inverse: nothing preexisted, so the
    abort must remove the file rather than restore anything."""
    owner, frame, _wal = session
    target = root / "fresh.txt"

    fs.write_witnessed("fresh.txt", "made\n")
    assert target.exists()

    _abort(owner, frame)

    assert not target.exists()


def test_the_entry_is_reachable_from_the_list_drain_actually_reads(root, session):
    """FAILS pre-fix: `_deferred_transactional` is empty.

    The structural statement behind the two above. `_transactional` carried the
    entry before and after — it is the introspection/WAL-record list — but only
    `_deferred_transactional` is walked by `drain`, so membership there is what
    makes the inverse reachable at teardown at all."""
    owner, frame, _wal = session
    (root / "notes.txt").write_text("v1")

    fs.write_witnessed("notes.txt", "v2")

    assert len(frame._transactional) == 1
    assert frame._deferred_transactional == frame._transactional


def test_three_host_writes_revert_newest_first_on_the_runtime_abort(root, session):
    """FAILS pre-fix: the target stays at `v4` with nothing replayed.

    The item-369 LIFO rule, driven through the real abort rather than by hand.
    Three overlapping writes to one path: because every fs inverse is
    idempotent-and-total, a FIFO replay would restore `v1` first, then let the
    newer inverses undo into the hole. Newest-first lands back on `v1`."""
    owner, frame, _wal = session
    target = root / "doc.txt"
    target.write_text("v1")

    fs.write_witnessed("doc.txt", "v2")
    fs.write_witnessed("doc.txt", "v3")
    fs.write_witnessed("doc.txt", "v4")
    assert target.read_text() == "v4"

    _abort(owner, frame)

    assert target.read_text() == "v1"


def test_a_drifted_sidecar_reaches_the_report_through_the_runtime_abort(root, session):
    """FAILS pre-fix: no residue record at all, because the refusal path is only
    reached if the inverse runs.

    #1037/#1052/#1070 made `restore` refuse and report `restore-residue` when the
    preimage cannot be shown to be the captured one. That reporting is downstream
    of the inverse running, so an unreachable inverse silently suppressed it: the
    forward mutation stayed AND the abort reported clean, which is strictly worse
    than the residue it was built to name."""
    owner, frame, _wal = session
    target = root / "notes.txt"
    target.write_text("v1")

    fs.write_witnessed("notes.txt", "v2")
    [sidecar] = list((root / ws.PREIMAGE_DIRNAME).iterdir())
    with open(sidecar, "r+b") as fh:            # a competing same-UID writer
        fh.write(b"zz")

    _abort(owner, frame)

    [residue] = [r for r in owner.collect_compensation_residue()
                 if r["kind"] == "restore-residue"]
    assert residue["state"] == "unresolved"
    assert residue["outcome"] == "failed"
    assert "EIDENTITY" in residue["error"]["message"]
    # and the drifted snapshot was NOT installed: the abort left the forward
    # mutation and NAMED it, which is what residue means.
    assert target.read_text() == "v2"


def test_a_withdrawn_frame_escrows_the_host_write_until_the_verdict(root, session):
    """FAILS pre-fix: the mid-session withdrawal escrowed nothing, so the later
    session abort had nothing to replay.

    A frame torn down while the session verdict is still pending escrows its
    undischarged entries rather than settling them. `drain` escrows from
    `_deferred_transactional`, so the same unreachability applied here."""
    owner, frame, _wal = session
    target = root / "notes.txt"
    target.write_text("v1")

    fs.write_witnessed("notes.txt", "v2")

    frame.drain()                               # verdict still None: escrow
    assert target.read_text() == "v2"           # held, neither replayed nor discharged

    owner.begin_abort()
    owner.finalize_abort()

    assert target.read_text() == "v1"


# ===========================================================================
# NON-VACUITY controls: each passes on BOTH sides of the fix.
# ===========================================================================

def test_nonvacuity_a_deferred_inverse_still_replays_on_this_drive(root, session):
    """NON-VACUITY, passes pre-fix and post-fix.

    The same abort drive over an inverse that was ALREADY reachable: one
    registered through `Frame.transactional_method`, the provide-method seam.
    If this ever fails, the tests above are failing because the drive is wrong,
    not because the host-bound registration is."""
    owner, frame, _wal = session
    target = root / "by-hand.txt"
    target.write_text("v1")
    replayed = []

    def undo(witness):
        replayed.append(witness)
        Path(witness["path"]).write_text(witness["was"])

    target.write_text("v2")
    frame.transactional_method(undo, {"path": str(target), "was": "v1"})

    _abort(owner, frame)

    assert replayed == [{"path": str(target), "was": "v1"}]
    assert target.read_text() == "v1"


def test_nonvacuity_a_committing_drive_keeps_the_host_write(root, session):
    """NON-VACUITY, passes pre-fix and post-fix.

    The commit direction of the same seam, stated as the world state a caller
    can see. A witnessed mutation is the deliverable, so a clean unload must
    leave it in place. This is the regression the fix could plausibly have
    introduced — parking the entry where `drain` reads it must not turn a commit
    into a revert — and it held before the change as well, for the different
    reason that nothing ran at all."""
    owner, frame, _wal = session
    target = root / "notes.txt"
    target.write_text("v1")

    fs.write_witnessed("notes.txt", "v2")
    _commit(owner, frame)

    assert target.read_text() == "v2"


def test_the_commit_discharges_the_entry_rather_than_leaving_it_unsettled(root, session):
    """FAILS pre-fix: `discharged` is False, because the entry was never
    disposed either way.

    The bookkeeping half of the control above. An unreachable entry looks
    identical to a committed one from outside — the mutation stays — but it is
    not settled: the inverse and witness are still held, so nothing proves the
    transaction was decided. A commit must DISCHARGE it (inverse skipped,
    witness GC'd)."""
    owner, frame, _wal = session
    (root / "notes.txt").write_text("v1")

    fs.write_witnessed("notes.txt", "v2")
    _commit(owner, frame)

    [entry] = frame._transactional
    assert entry.discharged is True
    assert entry.replayed is False
    assert entry.witness is None


def test_nonvacuity_the_refusal_before_any_session_is_unchanged(root):
    """NON-VACUITY, passes pre-fix and post-fix.

    The bind point is the only thing that moved: with no witnessed session the
    write is still refused `EUNWITNESSED` with nothing written."""
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_witnessed("nope.txt", "x")
    assert ei.value.code == "EUNWITNESSED"
    assert not (root / "nope.txt").exists()


# ===========================================================================
# The durable record is unchanged in the ways that are load-bearing.
# ===========================================================================

def test_the_descriptor_still_declares_the_inverse_idempotent(root, session):
    """The host inverse is declared idempotent (item 309), so the descriptor
    must still say so and the replay must write NO fence. Moving the
    registration to `transactional_method` would have dropped the declaration if
    it were not threaded through, which `revl recover` reads off the record to
    choose free-vs-fenced replay."""
    owner, frame, wal = session
    (root / "notes.txt").write_text("v1")

    fs.write_witnessed("notes.txt", "v2")

    [descriptor] = [r for r in wal.records if r.get("kind") == "transactional"]
    assert descriptor["undo_idempotent"] is True
    assert frame._transactional[0].undo_idempotent is True

    _abort(owner, frame)

    assert wal.fences == []


def test_the_abort_names_the_replayed_seq_in_the_completion_record(root, session):
    """FAILS pre-fix: `replayed` is empty, because nothing replayed.

    `finalize_abort` names every seq whose inverse actually ran. That record is
    how a completed abort is told from a crashed one, so an unreachable inverse
    also made the durable abort record understate what happened."""
    owner, frame, wal = session
    (root / "notes.txt").write_text("v1")

    fs.write_witnessed("notes.txt", "v2")
    seq = frame._transactional[0].seq

    result = _abort(owner, frame)

    assert result["replayed"] == [seq]
    assert {"kind": "aborted", "seqs": [seq]} in wal.records
