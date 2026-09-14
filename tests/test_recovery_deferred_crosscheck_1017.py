"""The `activation-complete` short-circuit cross-checks deferred emissions
against the `flushed` set before certifying residue clean (issue #1017).

`recovery.recover` reads a terminal `activation-complete` marker as "activation
finished, nothing is in flight" and returns through `_roll_forward`. That marker
settles the ACTIVATION; it never settled the class-(b) deferral queue. The
approved-to-discharged window path (`_roll_forward_window`) already cross-checks
every `deferred-emission` descriptor against the `flushed` records and reports an
unmatched one as OWED — "the honest state" — but a WAL whose cut includes the
activation marker returned before that check could run, so a queue that was
approved and never flushed read as `residue.clean: true`.

The measured sequence is the one from the item-245 deferral-queue finding
(#1004): `[effect, effect, deferred-emission, effect, effect, commit-approved,
activation-complete]`. #1004 fixed the CAUSE of that particular lost queue (the
queue is now carried across a generation change); it did not stop the reader from
certifying a WAL cut after a lost queue as balanced. That is what this pins.

Failure direction: FAIL CLOSED. An approved deferred emission with no `flushed`
record is RESIDUE, not silence. The control below pins the opposite error too —
an honest, fully-flushed activation must keep reading CLEAN.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


import replay  # noqa: E402
from revl import wal as wal_core  # noqa: E402
from revl.recovery import recover, render  # noqa: E402


def _effects(wal, *labels) -> None:
    """Append one activation-phase `effect` record per label."""
    timeline = replay.Timeline("Svc")
    for index, label in enumerate(labels):
        timeline.record_emission("bus", "send", (label,), "Bus", ("<f>", index))
    wal.append_timeline(timeline)


def _write_lost_queue_wal(path: str, *, flush: bool) -> int:
    """The #1004 record sequence, with the `flushed` record present or absent.

    `flush=False` is the measured cut verbatim:
        [effect, effect, deferred-emission, effect, effect,
         commit-approved, activation-complete]
    `flush=True` is the same run with the queue honestly flushed — the
    non-vacuity control. Returns the deferred emission's seq.
    """
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    _effects(wal, "e0", "e1")
    deferred = wal.record_deferred_emission(receiver="sink", method="enqueue",
                                            args=["q0"])
    _effects(wal, "e2", "e3")
    wal.record_commit_approved("h0")
    if flush:
        wal.record_flushed(deferred["seq"])
    wal.commit_activation(["Svc"])
    wal.commit_run()
    wal.close()
    return deferred["seq"]


def _kinds(path: str) -> list:
    return [r.get("record") for r in wal_core.read_wal(path)["records"]]


def _deferrals(report: dict) -> dict:
    """The report's deferral cross-check, tolerating its ABSENCE.

    The two non-vacuity controls below must pass on the pre-fix tree as well as
    this one — that is the whole point of a control — and the pre-fix report has
    no `deferrals` key at all. Reading it through this default lets a control
    state "nothing is owed" in one form that is true on both trees, while the
    tests that pin the new behaviour read the key directly."""
    return report.get("deferrals") or {"flushed": [], "owed": [], "dropped": []}


def test_activation_complete_never_certifies_a_lost_queue_clean(tmp_path):
    """Issue #1017. A `deferred-emission` that was APPROVED and carries no
    `flushed` record must not read as CLEAN just because the cut reached
    `activation-complete`. Fails on main: `residue.clean` was True."""
    path = str(tmp_path / "lost-queue.wal")
    seq = _write_lost_queue_wal(path, flush=False)

    # the exact measured sequence, verbatim, with no `flushed` anywhere.
    assert _kinds(path) == ["effect", "effect", "deferred-emission",
                            "effect", "effect", "commit-approved",
                            "activation-complete", "run-complete"]
    assert "flushed" not in _kinds(path)

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    # the crux: the short-circuit no longer certifies an unbalanced WAL clean.
    assert report["residue"]["clean"] is False
    owed = report["deferrals"]["owed"]
    assert [entry["seq"] for entry in owed] == [seq]
    assert owed[0]["referent"] == "sink.enqueue"
    assert owed[0]["outcome"] == "not-attempted"
    # and it is enumerated in the merged residue schema, same kind the
    # approved-to-discharged window path reports.
    outstanding = report["residue"]["outstanding"]
    assert [r["kind"] for r in outstanding] == ["flush-residue"]
    assert outstanding[0]["attemptedFlag"] is False
    assert "no `flushed` record" in report["residue"]["proof"]
    # recovery fires nothing on this path: an owed emission is reported, never
    # re-issued, so the verdict is a report and not a second crossing.
    assert outstanding[0]["outcome"] == "not-attempted"
    # the operator sees it in `revl recover`'s rendering, and the roll-forward
    # body is still rendered (not the window arm).
    text = render(report)
    assert "OWED" in text and "sink.enqueue" in text
    assert "committed effects" in text
    assert "[RESIDUE]" in text


def test_a_balanced_activation_still_reads_clean(tmp_path):
    """Non-vacuity control. The SAME run with the deferral honestly flushed must
    keep reporting CLEAN — the cross-check must not invent residue. Passes on
    main AND on the fix; only the test above separates them."""
    path = str(tmp_path / "balanced.wal")
    seq = _write_lost_queue_wal(path, flush=True)

    assert _kinds(path) == ["effect", "effect", "deferred-emission",
                            "effect", "effect", "commit-approved", "flushed",
                            "activation-complete", "run-complete"]

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True
    assert report["residue"]["outstanding"] == []
    assert _deferrals(report)["owed"] == []
    assert "RESIDUE" not in report["residue"]["proof"]
    assert "[CLEAN]" in render(report)
    assert seq is not None


def test_a_balanced_activation_names_its_confirmed_flush(tmp_path):
    """The bookkeeping behind the control: a matched deferral is reported as
    CONFIRMED FLUSHED, not merely omitted, so the clean verdict is a statement
    about the queue rather than silence about it."""
    path = str(tmp_path / "balanced-named.wal")
    seq = _write_lost_queue_wal(path, flush=True)

    report = recover(path)
    assert [entry["seq"] for entry in report["deferrals"]["flushed"]] == [seq]
    assert report["deferrals"]["flushed"][0]["referent"] == "sink.enqueue"
    assert report["deferrals"]["owed"] == []


def test_a_deferral_free_activation_is_unchanged(tmp_path):
    """Second non-vacuity control: a WAL with no deferral queue at all still
    reads CLEAN, with an empty cross-check. Passes on both trees."""
    path = str(tmp_path / "no-queue.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    _effects(wal, "e0")
    wal.commit_activation(["Svc"])
    wal.commit_run()
    wal.close()

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True
    assert report["residue"]["outstanding"] == []
    assert _deferrals(report)["owed"] == []
    assert _deferrals(report)["flushed"] == []


def test_an_unapproved_deferral_under_a_complete_wal_stays_dropped_clean(tmp_path):
    """A deferral with NO `commit-approved` after it was DROPPED, never fired
    (item 245, Decision 3: dropping is free, nothing crossed). The cross-check
    must keep reading that as clean, exactly as the roll-back path does — the
    fail-closed direction is about APPROVED queues, not about every descriptor.
    """
    path = str(tmp_path / "dropped.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    _effects(wal, "e0")
    wal.record_deferred_emission(receiver="sink", method="enqueue", args=["q0"])
    wal.record_aborted([])
    wal.commit_activation(["Svc"])
    wal.commit_run()
    wal.close()

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True
    assert report["residue"]["outstanding"] == []
    assert _deferrals(report)["owed"] == []


def test_a_failed_flush_under_a_complete_wal_is_residue(tmp_path):
    """A `flush-residue` record says the host body RAISED at flush
    (continue-and-record). The commit still completed and stamped
    `activation-complete`, so before the cross-check this too read as CLEAN.
    It is an unlanded crossing: residue, with the recorded error carried."""
    path = str(tmp_path / "failed-flush.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    deferred = wal.record_deferred_emission(receiver="sink", method="enqueue",
                                            args=["q0"])
    wal.record_commit_approved("h0")
    wal.record_flush_residue(deferred["seq"],
                            {"type": "OSError", "message": "sink refused"})
    wal.commit_activation(["Svc"])
    wal.commit_run()
    wal.close()

    report = recover(path)
    assert report["residue"]["clean"] is False
    owed = report["deferrals"]["owed"]
    assert [entry["outcome"] for entry in owed] == ["failed"]
    outstanding = report["residue"]["outstanding"]
    assert outstanding[0]["error"]["message"] == "sink refused"
    assert outstanding[0]["attemptedFlag"] is True


def test_steady_state_residue_and_a_lost_queue_are_reported_together(tmp_path):
    """Both causes at once: the issue-#536 steady-state crossing AND an owed
    deferral. Neither may mask the other — the proof names both."""
    path = str(tmp_path / "both.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_deferred_emission(receiver="sink", method="enqueue", args=["q0"])
    wal.record_commit_approved("h0")
    wal.commit_activation(["Svc"])
    _effects(wal, "steady-event")   # after the marker, and no `run-complete`
    wal.close()

    report = recover(path)
    assert report["residue"]["clean"] is False
    assert len(report["steadyState"]["outstanding"]) == 1
    assert len(report["deferrals"]["owed"]) == 1
    proof = report["residue"]["proof"]
    assert "steady state" in proof
    assert "no `flushed` record" in proof
    assert len(report["residue"]["outstanding"]) == 2
