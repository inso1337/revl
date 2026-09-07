"""WAL recovery over torn tails and reused files (issues #641, #642).

Two independent defects in the same recovery path, both surfacing when a WAL is
NOT the pristine single-run file the happy path assumes:

* #641 — forward ADMISSION recovery appended a terminal `admit-finalized` /
  `admit-abandoned` record straight onto a torn (never-acknowledged, non
  newline-terminated) trailing write, WITHOUT first sealing it. The two merge
  into one unparseable line: the reader tolerates it as a torn LAST line and
  silently drops the finalization (not durably readable), and a second terminal
  append leaves the merged line mid-file, which the item 413 gate refuses
  forever with `WALIntegrityError`. The fix makes `_append_admit_record` seal
  the torn tail first, exactly as every other appender does (#535/#563).

* #642 — a single WAL file can hold more than one run when a later invocation
  reuses the same `--wal` path (the recorder resumes the seq space and appends,
  issue #536). `_roll_forward` selected the FIRST `activation-complete` and
  asked `any(run-complete)` over the WHOLE history, so an EARLIER run's clean
  `run-complete` masked a LATER run's steady-state crash residue and reported a
  false CLEAN. The fix scopes the run to the LAST activation marker and counts
  only a `run-complete` in that run's own interval.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import replay  # noqa: E402
from revl import wal as wal_core  # noqa: E402
from revl.recovery import recover, recover_forward_admissions  # noqa: E402


# --------------------------------------------------------------------------- #
# #641 — seal the torn tail before appending a terminal admission record
# --------------------------------------------------------------------------- #

def _write_advanced_prefix_then_torn(path: str) -> None:
    """A valid `admit-decided`+`admit-applied` prefix (an ADVANCED decision that
    forward recovery finalizes) followed by a TORN final line — a `kill -9`
    caught the WAL mid-write, so the last line has no trailing newline."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"record": "header", "walVersion": 1,
                                 "generation": 1,
                                 "guarantee": wal_core.WAL_GUARANTEE},
                                sort_keys=True) + "\n")
        handle.write(json.dumps({"record": "admit-decided", "seq": 5,
                                 "decisionId": "D1", "turn": {"t": 1}},
                                sort_keys=True) + "\n")
        handle.write(json.dumps({"record": "admit-applied", "seq": 7,
                                 "decisionId": "D1"}, sort_keys=True) + "\n")
        # the crash itself: a partial record, no trailing newline.
        handle.write('{"record": "eff')


def test_forward_admission_finalize_survives_a_torn_tail(tmp_path):
    """Issue #641. Forward recovery finalizes an advanced decision by appending
    `admit-finalized`. With a torn trailing line present it must SEAL first, so
    the terminal record lands on its own clean line and is durably readable —
    not merged into the torn tail and silently dropped."""
    path = str(tmp_path / "admit.wal")
    _write_advanced_prefix_then_torn(path)

    wal = wal_core.read_wal(path)
    assert wal["torn"] is True  # the reader saw the torn trailing write
    # no session: `admit-applied` present with no CAS makes this ADVANCED, so
    # forward recovery appends `admit-finalized`.
    reports = recover_forward_admissions(wal, forward=True, wal_path=path)
    assert len(reports) == 1
    assert reports[0]["classification"] == "advanced"
    assert reports[0]["finalized"] is True

    # the finalization must be durably READABLE after reopening — before the fix
    # it merged onto the torn tail and the reader dropped it as a torn last line.
    reread = wal_core.read_wal(path)
    finalized = [r for r in reread["records"]
                 if r.get("record") == "admit-finalized"]
    assert len(finalized) == 1
    assert finalized[0]["decisionId"] == "D1"
    # the acknowledged prior records are retained; the torn tail is gone.
    assert reread["torn"] is False
    assert [r.get("record") for r in reread["records"]] == [
        "admit-decided", "admit-applied", "admit-finalized"]


def test_second_forward_pass_over_a_torn_tail_stays_readable_and_idempotent(tmp_path):
    """Issue #641, the mid-file-corruption half. A second recovery pass must not
    turn the WAL into permanent `WALIntegrityError` corruption. Before the fix
    the first pass merged `admit-finalized` onto the torn tail (dropped as torn),
    so the second pass re-classified the decision ADVANCED and appended AGAIN —
    now the merged unparseable line had content after it (mid-file corruption),
    which the item 413 gate refuses forever. Sealed, the first finalize is
    durable, so the second pass is a no-op over a clean file."""
    path = str(tmp_path / "admit.wal")
    _write_advanced_prefix_then_torn(path)

    first = recover_forward_admissions(wal_core.read_wal(path),
                                       forward=True, wal_path=path)
    assert first[0]["finalized"] is True

    # the WAL must still READ (no mid-file corruption) ...
    reread = wal_core.read_wal(path)  # would raise WALIntegrityError before fix
    # ... and the decision is already finalized, so the second pass changes
    # nothing and reports nothing (a fully committed decision is a no-op).
    second = recover_forward_admissions(reread, forward=True, wal_path=path)
    assert second == []
    # still exactly one finalize; no duplicate, no corruption.
    final = wal_core.read_wal(path)
    assert sum(r.get("record") == "admit-finalized"
               for r in final["records"]) == 1


# --------------------------------------------------------------------------- #
# #642 — a historical run-complete must not mask a later run's crash residue
# --------------------------------------------------------------------------- #

def _run_one_clean(path: str) -> None:
    """Run 1 reuses `path`: activate, cross a steady-state emission, shut down
    cleanly (stamping `run-complete`)."""
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.commit_activation(["Svc"])
    tl = replay.Timeline("Svc")
    tl.record_emission("bus", "send", ("run1-event",), "Bus", ("<f>", 1))
    wal.append_timeline(tl)
    wal.commit_run()   # orderly teardown: run 1's own `run-complete`
    wal.close()


def _run_two_crashes(path: str) -> None:
    """Run 2 REUSES the same `path`: activate again, cross a steady-state
    emission, then `kill -9` — no `run-complete` for run 2."""
    wal = replay.WriteAheadLog(path, ir={}, generation=2).open()
    wal.commit_activation(["Svc"])
    tl = replay.Timeline("Svc")
    tl.record_emission("bus", "send", ("run2-event",), "Bus", ("<f>", 2))
    wal.append_timeline(tl)
    wal.close()   # <-- crash: no commit_run()


def test_later_run_crash_residue_is_not_masked_by_an_earlier_run_complete(tmp_path):
    """Issue #642. Run 1 completed cleanly and Run 2 (same WAL file) crashed in
    steady state. Run 1's `run-complete` must NOT settle Run 2's outstanding
    crossing — recover must surface Run 2's residue instead of a false CLEAN."""
    path = str(tmp_path / "reused.wal")
    _run_one_clean(path)
    _run_two_crashes(path)

    # both runs are in the one file: two activation markers, one run-complete.
    records = wal_core.read_wal(path)["records"]
    assert sum(r.get("record") == "activation-complete" for r in records) == 2
    assert sum(r.get("record") == "run-complete" for r in records) == 1

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    # the crux: NOT falsely CLEAN — Run 2's steady crossing is still out.
    assert report["residue"]["clean"] is False
    outstanding = report["steadyState"]["outstanding"]
    assert len(outstanding) == 1
    assert outstanding[0]["kind"] == "steady-state-residue"
    # and it is RUN 2's crossing that is surfaced, not run 1's completed one.
    referent = outstanding[0].get("referent") or ""
    assert "run2-event" in referent
    assert "run1-event" not in referent


def test_a_second_run_that_also_shuts_down_cleanly_reads_clean(tmp_path):
    """The counterpart, so the fix is scoping and not a blanket 'reused WAL is
    dirty': when Run 2 ALSO stamps its own `run-complete`, its interval is
    settled and recover reads CLEAN. The completed interval keeps its correct
    clean verdict (#642's regression-coverage requirement)."""
    path = str(tmp_path / "reused-clean.wal")
    _run_one_clean(path)
    # run 2, this time an orderly shutdown of its own.
    wal = replay.WriteAheadLog(path, ir={}, generation=2).open()
    wal.commit_activation(["Svc"])
    tl = replay.Timeline("Svc")
    tl.record_emission("bus", "send", ("run2-event",), "Bus", ("<f>", 2))
    wal.append_timeline(tl)
    wal.commit_run()
    wal.close()

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True
    assert report["steadyState"]["outstanding"] == []
