"""A crash in a run that REUSES a WAL is recovered by its own opening (#3147).

A WAL file may be reused for a later run (`--wal PATH` appends to whatever is
already there: #641/#642). A later run records a `generation` record before its
first effect, and the prior run's `activation-complete` still sits earlier in
the file. `read_wal` used to set ``complete`` on ANY marker anywhere, and
`recover` took the whole-file decision from it, so a later run that crashed in
the middle of its activation was read as a finished activation: ROLLED-FORWARD,
exit 0, and the later run's uncommitted writes stayed in place. A control run on
a fresh WAL with the same crash rolls back and re-issues the inverse.

The fix scopes the terminal marker (and `commit-approved`) to the interval the
LATEST opening owns, and recovers each opening by its own marker. These tests
pin the two-run case the way the issue reports it:

* run 1 completes (activation + orderly teardown), run 2 reuses the same file
  and crashes mid-activation: recover ROLLS BACK run 2 and leaves run 1's
  committed effects alone, rather than declaring the half-done activation
  finished;
* the same crash driven through `revl recover --wal` (the CLI an operator or a
  supervisor script trusts) takes the roll-back branch;
* a prior opening's `commit-approved` does not roll the later crash forward,
  and that approved opening's owed discharge is still rolled forward;
* an opening an earlier recover already rolled back is not rolled back again
  once a later opening committed on top of it;
* a compiled tier's repeated `header` is an opening boundary too;
* both openings completing still reads CLEAN, so the change is per-opening
  scoping and not a blanket "a reused WAL is dirty".
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
from revl.recovery import DictWorld, recover, render  # noqa: E402
from revl.run_wasm import (  # noqa: E402
    wal_activation_complete, wal_descriptor, wal_discharge, wal_header)

#: A non-empty IR is what makes a REOPENED log write its `generation` record
#: (replay.WriteAheadLog.open): the boundary this issue is about. A bare log
#: (`ir={}`) cannot tell the two openings apart, which is why every #642 test
#: written before the marker was recorded stays on the single-interval path.
IR = {"composition": "wal-reuse-activation-3147"}


def _unlink(name: str) -> dict:
    return {"receiver": "fs", "method": "unlink", "args": [name]}


def _run_one_completes(path: str) -> None:
    """Run 1: a witnessed, reconstructible write, then a complete activation and
    an orderly shutdown."""
    wal = replay.WriteAheadLog(path, ir=IR, generation=1).open()
    wal.record_boundary("Store", "write data.txt", resource="file:data.txt",
                        inverse_op=_unlink("data.txt"))
    wal.commit_activation(["Store"])
    wal.commit_run()
    wal.close()


def _run_two_crashes_mid_activation(path: str) -> None:
    """Run 2 REUSES the file: it appends its `generation` record and a witnessed
    write, then `kill -9` before stamping its own `activation-complete`."""
    wal = replay.WriteAheadLog(path, ir=IR, generation=2).open()
    wal.record_boundary("Store", "write data2.txt", resource="file:data2.txt",
                        inverse_op=_unlink("data2.txt"))
    wal.close()   # <-- crash: no commit_activation(), no commit_run()


def test_reused_wal_later_crash_mid_activation_rolls_back(tmp_path):
    """Issue #3147. Run 1 completed, Run 2 (same file) crashed mid-activation.
    The whole-file marker must not read as Run 2's: recover ROLLS BACK Run 2's
    witnessed write and never replays Run 1's committed inverse. Before the fix
    the verdict was ROLLED-FORWARD with Run 2's crossing left in place."""
    path = str(tmp_path / "reused.wal")
    _run_one_completes(path)
    _run_two_crashes_mid_activation(path)

    loaded = wal_core.read_wal(path)
    records = loaded["records"]
    # the file really is the two-run shape: one marker (run 1's), one boundary
    # record (run 2's own opening), and the seq space continues past the header.
    assert sum(r.get("record") == "activation-complete" for r in records) == 1
    generations = [r for r in records if r.get("record") == "generation"]
    assert len(generations) == 1 and generations[0]["fromSeq"] > 0
    # the terminal flag names the LATEST opening's outcome, which never finished.
    assert loaded["complete"] is False

    report = recover(path, world=DictWorld())
    assert report["verdict"] == "rolled-back"
    assert report["residue"]["clean"] is True
    # exactly run 2's uncommitted write is inverted; run 1's completed write is
    # untouched by the roll-back.
    assert [(e["op"]["method"], e["op"]["args"]) for e in report["ran"]] == [
        ("unlink", ["data2.txt"])]


def test_cli_recover_reused_wal_rolls_back_the_later_opening(tmp_path, capsys):
    """Issue #3147 through `revl recover --wal` (`main()`), the path a supervisor
    script trusts. The half-done later activation must take the roll-back branch
    and re-issue its inverse; before the fix the CLI reported ROLLED-FORWARD."""
    path = str(tmp_path / "reused-cli.wal")
    _run_one_completes(path)
    _run_two_crashes_mid_activation(path)

    # --model-only accepts the model's re-issued inverse as the verdict; the
    # point here is the branch taken, not a real-world replay.
    rc = main(["recover", "--wal", path, "--json", "--model-only"])
    out = capsys.readouterr().out

    report = json.loads(out)
    assert report["verdict"] == "rolled-back"
    assert report["residue"]["clean"] is True
    assert [e["op"]["method"] for e in report["ran"]] == ["unlink"]
    assert rc == 0


def test_prior_opening_commit_approved_does_not_commit_the_later_crash(tmp_path):
    """Issue #3147, the `commit-approved` half. Run 1 crashed inside the
    approved-to-discharged window, so its durable `commit-approved` is a
    committed session and its interval must never be rolled back. Run 2 reused
    the file and crashed mid-activation. The prior opening's approval must not
    roll Run 2's uncommitted write forward as discharged; recover rolls Run 2
    back and leaves the committed interval's transaction alone. Run 1 is still
    a committed session (item 245, Decision 3), so its missing discharge is
    rolled forward, as a single-opening recover of Run 1 would have done."""
    path = str(tmp_path / "approved.wal")
    wal = replay.WriteAheadLog(path, ir=IR, generation=1).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#1"],
        origin={"key": "db", "method": "insert", "args": ["row#1"]},
        witness={"row": "row#1"})
    wal.record_commit_approved("sha256:manifest")
    wal.close()   # <-- crash in the approved-to-discharged window

    _run_two_crashes_mid_activation(path)

    report = recover(path, world=DictWorld())
    assert report["verdict"] == "rolled-back"
    assert [e["op"]["method"] for e in report["ran"]] == ["unlink"]
    # the committed opening's transaction is not inverted ...
    assert report.get("transactionalRolledBack", []) == []
    # ... and the later crash is not rolled FORWARD as a committed session.
    assert report.get("rolledForwardDischarge") is None
    # Run 1's owed discharge IS rolled forward: its approval is a commit.
    descriptor = next(r for r in wal_core.read_wal(path)["records"]
                      if r.get("record") == "discharge-descriptor")
    after = wal_core.read_wal(path)["records"]
    assert [r["discharged"] for r in after
            if r.get("record") == "discharge"] == [[descriptor["seq"]]]
    [window] = report["priorWindows"]
    assert window["opening"] == 1
    assert window["rolledForwardDischarge"] == [descriptor["seq"]]
    assert "committed earlier opening 1" in render(report)

    # a second recover reads the same verdict and appends nothing more.
    again = recover(path, world=DictWorld())
    assert again["verdict"] == "rolled-back"
    assert again["priorWindows"][0]["rolledForwardDischarge"] == []
    assert sum(r.get("record") == "discharge"
               for r in wal_core.read_wal(path)["records"]) == 1


def test_an_opening_already_recovered_is_not_rolled_back_again(tmp_path):
    """Three openings on one file. Run 1 crashes mid-activation and recover
    rolls it back; Run 2 writes data.txt again and completes; Run 3 crashes
    mid-activation. Only Run 3 is this crash's work: replaying Run 1's inverse
    again would delete the data.txt Run 2 committed."""
    path = str(tmp_path / "three.wal")
    wal = replay.WriteAheadLog(path, ir=IR, generation=1).open()
    wal.record_boundary("Store", "write data.txt", resource="file:data.txt",
                        inverse_op=_unlink("data.txt"))
    wal.close()   # <-- run 1 crashes
    first = recover(path, world=DictWorld())
    assert [(e["op"]["method"], e["op"]["args"]) for e in first["ran"]] == [
        ("unlink", ["data.txt"])]

    wal = replay.WriteAheadLog(path, ir=IR, generation=2).open()
    wal.record_boundary("Store", "write data.txt", resource="file:data.txt",
                        inverse_op=_unlink("data.txt"))
    wal.commit_activation(["Store"])
    wal.commit_run()
    wal.close()   # run 2 completes

    wal = replay.WriteAheadLog(path, ir=IR, generation=3).open()
    wal.record_boundary("Store", "write data3.txt", resource="file:data3.txt",
                        inverse_op=_unlink("data3.txt"))
    wal.close()   # <-- run 3 crashes

    assert len(wal_core.opening_intervals(
        wal_core.read_wal(path)["records"])) == 3
    report = recover(path, world=DictWorld())
    assert report["verdict"] == "rolled-back"
    assert [(e["op"]["method"], e["op"]["args"]) for e in report["ran"]] == [
        ("unlink", ["data3.txt"])]


def _compiled_tier_opening(path: str, seq: int, row: str, *,
                           completes: bool) -> None:
    """One opening the way the go, java and rust sinks write it when REVL_WAL
    already exists: append, stamp a fresh `header`, restart seq at 0."""
    lines = [wal_header(), wal_descriptor(seq, "db", "delete", row)]
    if completes:
        lines += [wal_discharge([seq]), wal_activation_complete()]
    with open(path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def test_a_repeated_header_is_an_opening_boundary(tmp_path):
    """Issue #3147 on the compiled tiers. Run 1 completes, Run 2 appends to
    the same REVL_WAL with its own `header` (no `generation` record, seq from 0)
    and crashes. Run 1's marker must not roll Run 2 forward, and Run 1's
    `discharge [0]` must not read as discharging Run 2's seq 0."""
    path = str(tmp_path / "compiled.wal")
    _compiled_tier_opening(path, 0, "row#1", completes=True)
    _compiled_tier_opening(path, 0, "row#2", completes=False)

    loaded = wal_core.read_wal(path)
    assert loaded["complete"] is False
    assert len(wal_core.opening_intervals(loaded["records"])) == 2
    assert loaded == replay.WriteAheadLog.read(path)

    report = recover(path, world=DictWorld())
    assert report["verdict"] == "rolled-back"
    assert [e["referent"] for e in report["transactionalRolledBack"]] == [
        "db:row#2"]
    assert [e["seq"] for e in report["transactionalRolledBack"]] == [0]
    assert not report.get("dischargedSkipped")


def test_a_single_header_wal_still_reads_as_one_opening(tmp_path):
    """The counterpart: one compiled-tier opening that completed is one
    interval and rolls forward, exactly as before."""
    path = str(tmp_path / "single.wal")
    _compiled_tier_opening(path, 0, "row#1", completes=True)
    loaded = wal_core.read_wal(path)
    assert loaded["complete"] is True
    assert all(r.get("record") != "header" for r in loaded["records"])
    assert recover(path, world=DictWorld())["verdict"] == "rolled-forward"


def test_reused_wal_where_both_runs_complete_still_reads_clean(tmp_path):
    """The counterpart, so the change is per-opening scoping and not a blanket
    "a reused WAL always rolls back": when both openings stamp their own
    activation marker and shutdown, recover still reads CLEAN."""
    path = str(tmp_path / "reused-clean.wal")
    _run_one_completes(path)
    wal = replay.WriteAheadLog(path, ir=IR, generation=2).open()
    wal.record_boundary("Store", "write data2.txt", resource="file:data2.txt",
                        inverse_op=_unlink("data2.txt"))
    wal.commit_activation(["Store"])
    wal.commit_run()
    wal.close()

    assert wal_core.read_wal(path)["complete"] is True
    report = recover(path, world=DictWorld())
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True
    # a roll-forward runs no inverse: the committed openings stay committed.
    assert "ran" not in report
