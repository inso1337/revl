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


import replay  # noqa: E402
from revl import wal as wal_core  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.recovery import recover, recover_forward_admissions  # noqa: E402


class _RestoredSessionStub:
    """A minimal stand-in for the restored Session forward recovery needs to run
    the content CAS against (design 460 §3): it returns a surface so the CAS on an
    `admit-decided` with no recorded `expected` digests passes vacuously and the
    decision classifies ADVANCED. Issue #476's review made a restored session
    MANDATORY before a forward finalize — the no-session path no longer finalizes
    — so these WAL-seal tests supply one to reach the terminal-append path they
    exercise, without dragging in the compiler."""

    def _forward_surface_for_turn(self, _turn):
        return {"baseManifestHash": None, "classMapDigest": None}


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
    # a restored session makes the CAS pass (the decided record carries no
    # `expected` digests, so nothing drifts) — the decision is ADVANCED and
    # forward recovery appends `admit-finalized` (issue #476 review: the finalize
    # requires a restored session, so a stub supplies one).
    reports = recover_forward_admissions(wal, session=_RestoredSessionStub(),
                                         forward=True, wal_path=path)
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
                                       session=_RestoredSessionStub(),
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


def _run_two_crashes_before_activation(path: str) -> None:
    """Run 2 REUSES the same `path` and crashes DURING activation: it appends an
    activation-body effect, then `kill -9` — BEFORE it stamps its own
    `activation-complete` (and so before any `run-complete`). The last
    `activation-complete` in the file is therefore run 1's, and run 1's clean
    `run-complete` sits in the tail AHEAD of run 2's outstanding effect."""
    wal = replay.WriteAheadLog(path, ir={}, generation=2).open()
    tl = replay.Timeline("Svc")
    tl.record_emission("bus", "send", ("run2-event",), "Bus", ("<f>", 2))
    wal.append_timeline(tl)
    wal.close()   # <-- crash: no commit_activation() and no commit_run()


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


def test_preactivation_second_run_crash_is_not_masked_by_an_earlier_run_complete(
        tmp_path):
    """Issue #642, the PREactivation residual. Run 1 completed cleanly (its own
    `activation-complete` then `run-complete`); Run 2 (same WAL file) appended an
    activation-body effect and crashed BEFORE stamping its own
    `activation-complete`. The last `activation-complete` in the file is thus run
    1's, and run 1's `run-complete` sits in the roll-forward tail AHEAD of run
    2's outstanding effect. That historical marker must NOT settle run 2's
    crossing — recover must surface run 2's residue, never a false CLEAN. Before
    the fix `any(run-complete)` over the whole tail zeroed the residue here."""
    path = str(tmp_path / "reused-preact.wal")
    _run_one_clean(path)
    _run_two_crashes_before_activation(path)

    # exactly ONE activation-complete (run 1's; run 2 crashed before its own)
    # and ONE run-complete (run 1's), with run 2's effect appended AFTER both.
    records = wal_core.read_wal(path)["records"]
    assert sum(r.get("record") == "activation-complete" for r in records) == 1
    assert sum(r.get("record") == "run-complete" for r in records) == 1
    kinds = [r.get("record") for r in records]
    assert kinds.index("run-complete") < len(kinds) - 1  # an effect follows it
    assert records[-1].get("record") == "effect"

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    # the crux: NOT falsely CLEAN — run 2's preactivation crossing is still out.
    assert report["residue"]["clean"] is False
    outstanding = report["steadyState"]["outstanding"]
    assert len(outstanding) == 1
    assert outstanding[0]["kind"] == "steady-state-residue"
    # and it is RUN 2's crossing surfaced, not run 1's completed one.
    referent = outstanding[0].get("referent") or ""
    assert "run2-event" in referent
    assert "run1-event" not in referent


def test_cli_recover_preactivation_second_run_crash_exits_nonzero(
        tmp_path, capsys):
    """Issue #642, the preactivation residual driven end to end through
    `revl recover --wal` (`main()`). Run 1 completed cleanly; Run 2 crashed
    DURING activation (an effect appended, no `activation-complete`, no
    `run-complete`). The CLI must exit NON-ZERO and name run 2's crossing as
    residue, never a false clean/exit-0 hidden behind run 1's `run-complete`."""
    path = str(tmp_path / "reused-preact-cli.wal")
    _run_one_clean(path)
    _run_two_crashes_before_activation(path)

    rc = main(["recover", "--wal", path, "--json"])
    out = capsys.readouterr().out

    assert rc == 1
    report = json.loads(out)
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is False
    outstanding = report["steadyState"]["outstanding"]
    assert len(outstanding) == 1
    assert outstanding[0]["kind"] == "steady-state-residue"
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


# --------------------------------------------------------------------------- #
# #642 — end to end: the reused-WAL residue must drive the CLI VERDICT and exit
# status, not just the in-process `recover()` dict. `revl recover --wal FILE`
# exits 0 only when the residue is clean (`src/revl/cli/change.py::_run_recover`),
# so the exact issue-#642 failure — a historical `run-complete` masking a later
# run's steady crash — is the difference between a false `EXIT 0 / CLEAN` and the
# honest `EXIT 1 / RESIDUE` an operator relies on after a crash. #642's own
# "Regression coverage" asks precisely for this CLI-result assertion.
# --------------------------------------------------------------------------- #

def test_cli_recover_over_reused_wal_surfaces_later_run_residue_nonzero(
        tmp_path, capsys):
    """Issue #642, driven through `revl recover --wal` (`main()`), not only the
    library. Run 1 completed cleanly and Run 2 (same WAL) crashed in steady
    state: the CLI must exit NON-ZERO and name Run 2's crossing as residue,
    never a false clean/exit-0 hidden behind Run 1's `run-complete`."""
    path = str(tmp_path / "reused-cli.wal")
    _run_one_clean(path)
    _run_two_crashes(path)

    rc = main(["recover", "--wal", path, "--json"])
    out = capsys.readouterr().out

    # fail-closed: honest residue means a non-zero exit for the operator.
    assert rc == 1
    report = json.loads(out)
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is False
    outstanding = report["steadyState"]["outstanding"]
    assert len(outstanding) == 1
    assert outstanding[0]["kind"] == "steady-state-residue"
    # it is RUN 2's crossing that drives the verdict, not run 1's completed one.
    referent = outstanding[0].get("referent") or ""
    assert "run2-event" in referent
    assert "run1-event" not in referent


def test_cli_recover_over_reused_wal_all_clean_exits_zero(tmp_path, capsys):
    """The scoping counterpart at the CLI boundary: when BOTH runs shut down
    cleanly, the reused WAL is genuinely settled and `revl recover` exits 0 with
    a CLEAN verdict. Proves the fix is per-run scoping, not a blanket 'a reused
    WAL always fails' — the completed intervals keep their correct clean exit."""
    path = str(tmp_path / "reused-cli-clean.wal")
    _run_one_clean(path)
    # run 2 also shuts down cleanly (its own `run-complete`).
    wal = replay.WriteAheadLog(path, ir={}, generation=2).open()
    wal.commit_activation(["Svc"])
    tl = replay.Timeline("Svc")
    tl.record_emission("bus", "send", ("run2-event",), "Bus", ("<f>", 2))
    wal.append_timeline(tl)
    wal.commit_run()
    wal.close()

    rc = main(["recover", "--wal", path, "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    report = json.loads(out)
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True
    assert report["steadyState"]["outstanding"] == []
