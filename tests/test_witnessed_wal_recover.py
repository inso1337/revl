"""Witnessed-effects teardown: the WAL discharge-descriptor and `revl recover`
(docs/design/teardown-contract.md, "WAL descriptor" + "Commit path").

This is the correctness-critical shared foundation for the six-tier teardown,
proven on the py reference tier. Three things the design review flagged and this
slice fixes, each with a load-bearing test here:

* the WAL discharge-descriptor the teardown loop writes for a `transactional`
  inverse and a `compensation` round-trips through recovery.py's reader — writer
  and reader agree on the named-call field (`call.receiver`);
* a COMMITTED transactional entry (its discharge record is durable) is NOT
  rolled back by `revl recover` — the discharged seq is skipped — while an
  ABORTED (undischarged) one IS rolled back. This is the central safety claim:
  the dangerous window is a Frame that commits then dies before its
  `activation-complete` marker, and the durable discharge record closes it;
* a re-issued best-effort `compensation` lands as RESIDUE (the forward referent
  is still out), never CLEAN, because recover applies it through the dedicated
  `apply_compensation` path that RECORDS rather than name-matching a remove verb
  and clearing the referent.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import replay  # noqa: E402
from revl.recovery import DictWorld, recover, render  # noqa: E402


# --------------------------------------------------------------- descriptor round-trip


def test_discharge_descriptor_round_trips_with_the_recovery_reader(tmp_path):
    """The writer (replay.py) and the reader (recovery.py) agree on the named-call
    field: `call.receiver`/`call.method`/`call.args`, the same shape
    `World.key`/`apply_inverse` already re-issue. `call.key` (the contract's first
    draft) is reconciled to `receiver` so no adapter shim is needed."""
    path = str(tmp_path / "desc.wal")
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        rec = wal.record_discharge_descriptor(
            "transactional", receiver="db", method="delete", args=["row#1"],
            origin={"key": "db", "method": "insert", "args": ["row#1"],
                    "site": "svc.rvl:12"},
            witness={"row": "row#1"}, idempotency=None)

    loaded = replay.WriteAheadLog.read(path)
    [desc] = [r for r in loaded["records"]
              if r.get("record") == "discharge-descriptor"]
    assert desc["entry"] == "transactional"
    assert desc["call"] == {"receiver": "db", "method": "delete", "args": ["row#1"]}
    assert desc["witness"] == {"row": "row#1"}
    # the reader keys the referent off exactly this named call
    assert DictWorld().key(desc["call"]) == "db:row#1"
    assert rec["seq"] == desc["seq"] == 0


def test_only_transactional_or_compensation_entries_are_accepted(tmp_path):
    import pytest
    with replay.WriteAheadLog(str(tmp_path / "x.wal"), ir={}) as wal:
        with pytest.raises(replay.ReplayError):
            wal.record_discharge_descriptor("bracket", receiver="fs",
                                            method="unlink", args=["/x"])


# ------------------------------------------------ committed is skipped, aborted rolls back


def _transactional_wal(path: str, *, discharge: bool) -> None:
    """A witnessed `db.insert(row#1)` (the mutation is a durable row) whose
    declared inverse is `db.delete(row#1)`. Optionally write the durable discharge
    record (the commit path). Never write `activation-complete` — the process
    'crashed' before it."""
    wal = replay.WriteAheadLog(path, ir={}, generation=7).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#1"],
        origin={"key": "db", "method": "insert", "args": ["row#1"],
                "site": "svc.rvl:9"},
        witness={"row": "row#1"})
    if discharge:
        wal.record_discharge([0])   # COMMITTED at drain, durable before success
    wal.close()                     # <-- crash: no commit_activation()


def test_a_committed_transactional_entry_is_not_rolled_back(tmp_path):
    """The Frame COMMITTED (its discharge record is on disk) then the process died
    before `activation-complete`. Recover must SKIP the discharged seq — the
    committed mutation persists, it is not rolled back."""
    path = str(tmp_path / "committed.wal")
    _transactional_wal(path, discharge=True)

    report = recover(path)
    assert report["verdict"] == "rolled-back"
    # the discharged seq was skipped, not replayed
    assert [s["seq"] for s in report["dischargedSkipped"]] == [0]
    assert report["transactionalRolledBack"] == []
    # the committed mutation is STILL in the world — not rolled back
    assert "db:row#1" in report["residue"]["worldRemaining"]
    # nothing is owed or unrecoverable: a retained commit is not residue
    assert report["residue"]["clean"] is True
    assert "committed transactional" in report["residue"]["proof"]


def test_an_aborted_transactional_entry_is_rolled_back(tmp_path):
    """The contrast: no discharge record (the transaction ABORTED / never
    committed). Recover reconstructs and runs the declared inverse — the mutation
    is rolled back, the referent cleared."""
    path = str(tmp_path / "aborted.wal")
    _transactional_wal(path, discharge=False)

    report = recover(path)
    assert report["verdict"] == "rolled-back"
    assert [s["seq"] for s in report["transactionalRolledBack"]] == [0]
    assert report["dischargedSkipped"] == []
    # the inverse ran: the referent is gone from the world
    assert "db:row#1" not in report["residue"]["worldRemaining"]
    assert report["residue"]["clean"] is True


# --------------------------------------------- a re-issued compensation is RESIDUE, not CLEAN


def test_a_reissued_compensation_is_residue_not_clean(tmp_path):
    """A `compensation` whose emission verb (`revoke`) NAME-MATCHES DictWorld's
    remove set. Routed through the generic `apply_inverse` it would POP the
    referent and report CLEAN; recover routes it through `apply_compensation`,
    which RECORDS and never clears, so the forward referent is still out —
    RESIDUE."""
    path = str(tmp_path / "comp.wal")
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        wal.record_discharge_descriptor(
            "compensation", receiver="svc", method="revoke", args=["grant#1"],
            origin={"key": "svc", "method": "grant", "args": ["grant#1"],
                    "site": "svc.rvl:20"})
    # NO discharge record (the compensation is OWED), NO activation-complete.

    report = recover(path)
    assert report["verdict"] == "rolled-back"
    assert [s["seq"] for s in report["compensationsReissued"]] == [0]
    # the forward referent is still out in the world — the offset did not clear it
    assert "svc:grant#1" in report["residue"]["worldRemaining"]
    assert report["residue"]["clean"] is False
    [rec] = [r for r in report["residue"]["outstanding"]
             if r["kind"] == "compensation-residue"]
    assert rec["attempted"] == {"call": "revoke", "args": ["grant#1"], "phase": 2}
    assert rec["outcome"] == "unknown"
    assert rec["referent"] == "svc:grant#1"
    assert rec["crossing"]["method"] == "grant"  # the ORIGINAL forward crossing
    assert "RESIDUE" in render(report)


def test_the_remove_verb_hazard_the_fix_closes(tmp_path):
    """Directly: the generic inverse path clears a `revoke`-named referent (the
    latent bug), the compensation path records it (the fix). Same op, opposite
    world outcome — this is why recover must not route a compensation through
    `apply_inverse`."""
    call = {"receiver": "svc", "method": "revoke", "args": ["grant#1"]}

    wrong = DictWorld()
    wrong.seed(wrong.key(call))
    wrong.apply_inverse(call)                 # name-matches "revoke" in _REMOVE
    assert wrong.remaining() == []            # WRONG: reported as cleared

    right = DictWorld()
    right.seed(right.key(call))
    right.apply_compensation(call)            # forced record-branch
    assert right.remaining() == ["svc:grant#1"]  # RIGHT: still out


def test_a_discharged_compensation_on_a_clean_frame_is_not_owed(tmp_path):
    """A compensation with a durable discharge record was discharged on a clean
    unload — it is never owed (the forward emission was the deliverable). Recover
    skips it, no residue."""
    path = str(tmp_path / "comp-discharged.wal")
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        wal.record_discharge_descriptor(
            "compensation", receiver="svc", method="revoke", args=["grant#1"],
            origin={"key": "svc", "method": "grant", "args": ["grant#1"]})
        wal.record_discharge([0])

    report = recover(path)
    assert report["compensationsReissued"] == []
    assert [s["seq"] for s in report["dischargedSkipped"]] == [0]
    assert report["residue"]["clean"] is True


# -------------------------------------------------------- mixed stack, both phases LIFO


def test_transactional_and_compensation_mix_walks_both_phases(tmp_path):
    """A mixed stack: two transactional entries (one committed, one aborted) and a
    compensation. Phase 1 handles the transactional inverses reverse-seq (skipping
    the committed one), Phase 2 re-issues the owed compensation."""
    path = str(tmp_path / "mix.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=3).open()
    wal.record_discharge_descriptor(                       # seq 0 — committed
        "transactional", receiver="db", method="delete", args=["a"],
        origin={"key": "db", "method": "insert", "args": ["a"]})
    wal.record_discharge_descriptor(                       # seq 1 — compensation, owed
        "compensation", receiver="mail", method="recall", args=["m"],
        origin={"key": "mail", "method": "send", "args": ["m"]})
    wal.record_discharge_descriptor(                       # seq 2 — aborted
        "transactional", receiver="db", method="delete", args=["b"],
        origin={"key": "db", "method": "insert", "args": ["b"]})
    wal.record_discharge([0])                              # only seq 0 committed
    wal.close()

    report = recover(path)
    assert [s["seq"] for s in report["dischargedSkipped"]] == [0]      # committed
    assert [s["seq"] for s in report["transactionalRolledBack"]] == [2]  # aborted
    assert [s["seq"] for s in report["compensationsReissued"]] == [1]  # owed
    remaining = report["residue"]["worldRemaining"]
    assert "db:a" in remaining        # committed, retained
    assert "db:b" not in remaining    # aborted, rolled back
    assert "mail:m" in remaining      # compensation offset, still out
    assert report["residue"]["clean"] is False  # the owed compensation is residue
