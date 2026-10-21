"""The window between an effect taking place and its record (issue #2728).

`backends/python/replay.py` appends an ordinary effect's record in
`Timeline._wal_append`, which runs after the effect body has run and yielded its
inverse. A process killed in that window leaves the world holding the effect
while the WAL holds nothing about it, so `revl recover` has no record to name,
invert or fence: the effect is UNKNOWN to recovery, not reported as undone.

`REVL_CRASH_BEFORE_RECORD` is the deterministic fault point at that instant. The
child process below performs the effect (a real file it leaves behind), records
it through the same `Timeline` the runtime uses, and is killed at the append.
The control is the identical child without the fault point: its record lands,
and recovery names it. The pair is what proves the fault point removes the
record and not the setup.

The verdict asserted is the one `docs/crash-recovery.md` states for this
window: recovery reports `rolled-back` over the records it has (here none) and
the landed effect stays out in the world, unmentioned. The clean proof's world
sentence overclaims in this window; the doc names it and the test does not
assert it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import replay  # noqa: E402
from revl.recovery import recover, render  # noqa: E402

#: The child run. It creates the effect first, then records it exactly as the
#: runtime does; with the fault point armed the recorder exits 137 before the
#: record reaches the log.
_CHILD = r"""
import os, sys
repo = os.environ["REPO"]
sys.path[:0] = [os.path.join(repo, "src"),
                os.path.join(repo, "backends", "python")]
import replay

wal = replay.WriteAheadLog(os.environ["WAL_PATH"], ir={}, generation=1).open()
timeline = replay.Timeline("Svc")
timeline.attach_wal(wal, {})
if os.environ["MODE"] == "emission":
    # an emission's record is written BEFORE the host body runs, so an append
    # crash here is not the effect-to-record window
    timeline.record_emission("Host", "read", (), "Host", ("<emission>", 1))
else:
    effect = os.environ["EFFECT_PATH"]
    with open(effect, "w", encoding="utf-8") as fh:
        fh.write("landed")                   # the effect takes place
    timeline.record_yield(lambda: os.unlink(effect), "activation")
wal.close()
"""


def _run_child(tmp_path: Path, *, crash_at: str | None,
               mode: str = "effect") -> tuple[int, str, str]:
    """Run the child above; return (exit code, effect path, WAL path)."""
    effect = str(tmp_path / "effect.txt")
    wal_path = str(tmp_path / "run.wal")
    env = dict(os.environ, REPO=str(ROOT), EFFECT_PATH=effect,
               WAL_PATH=wal_path, MODE=mode)
    env.pop("REVL_CRASH_BEFORE_RECORD", None)
    if crash_at is not None:
        env["REVL_CRASH_BEFORE_RECORD"] = crash_at
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD], env=env, capture_output=True,
        text=True, cwd=str(ROOT))
    return completed.returncode, effect, wal_path


def _effect_records(wal_path: str) -> list:
    return [r for r in replay.WriteAheadLog.read(wal_path)["records"]
            if r.get("record") == "effect" and r.get("kind") != "emission"]


def _emission_records(wal_path: str) -> list:
    return [r for r in replay.WriteAheadLog.read(wal_path)["records"]
            if r.get("kind") == "emission"]



@pytest.fixture
def crashed(tmp_path):
    """The effect ran and the process died before its record was appended."""
    code, effect, wal_path = _run_child(tmp_path, crash_at="activation")
    assert code == 137, (
        f"the child was expected to die at the effect-to-record fault point, "
        f"got exit {code}")
    return effect, wal_path


def test_a_killed_run_leaves_the_effect_out_of_the_wal(crashed):
    """The fault point is between the effect and the record: the file the child
    wrote is still in the world, and the log has no record of it. The control in
    the next test writes the same record without the fault, so this is the
    record the crash removed, not a setup difference."""
    effect, wal_path = crashed
    assert os.path.exists(effect), "the effect did not take place"
    assert _effect_records(wal_path) == [], (
        "the effect record landed even though the process died at the "
        "effect-to-record fault point")


def test_recover_cannot_name_an_effect_whose_record_never_landed(crashed):
    """The stated verdict for the window: recovery reads what the WAL has, so it
    reports `rolled-back` with nothing to run and does not mention the effect
    that landed. The effect is unknown to recovery; the report does not claim it
    was undone.

    The clean residue proof is bounded by the log, but its closing sentence is
    not: `_residue_proof` says "The world holds only what was deliberately
    committed" (`src/revl/recovery.py:2637-2640`), and this window makes that
    sentence false, because the landed effect is still out in the world and
    unnamed. The test asserts the fields recovery really reports and does not
    assert the proof string, so it cannot be read as blessing the overclaim."""
    effect, wal_path = crashed
    report = recover(wal_path)

    assert report["verdict"] == "rolled-back"
    assert report["committedEffects"] == 0
    assert report["ran"] == []
    assert report["moot"] == []
    assert report["unreconstructible"] == []
    # clean because it has nothing outstanding to report: exactly the limitation
    # this window names, and the proof's world sentence overclaims on top of it
    assert report["residue"]["clean"] is True
    assert report["residue"]["outstanding"] == []
    # the landed effect is still out in the world and the report never names it
    assert os.path.exists(effect)
    assert "activation/effect" not in render(report)


def test_the_same_run_without_the_fault_point_records_and_reports_it(tmp_path):
    """The control: without the fault point the same child records the effect,
    and recovery names it. This is what keeps the two tests above from passing
    because the child failed to record anything at all."""
    code, effect, wal_path = _run_child(tmp_path, crash_at=None)
    assert code == 0

    [record] = _effect_records(wal_path)
    assert "activation" in record["label"]
    assert os.path.exists(effect), "the effect did not take place"

    report = recover(wal_path)
    assert report["verdict"] == "rolled-back"
    assert report["committedEffects"] == 1
    # an in-process closure inverse is moot after a crash, not residue
    assert [entry["label"] for entry in report["moot"]] == ["activation/effect"]
    assert "activation/effect" in render(report)


def test_an_emission_is_excluded_even_when_armed_by_its_own_label(tmp_path):
    """The kind check, not a label miss, keeps emissions out of this window.

    The fault point is armed with the emission's own label, `Host.read`, which
    would otherwise match `step.label`; the child records only that emission and
    exits cleanly, so the emission's append is never a crash site. The record
    lands: an emission's record is written before the host body runs, so it is
    not the effect-to-record window."""
    code, _, wal_path = _run_child(tmp_path, crash_at="Host.read",
                                   mode="emission")
    assert code == 0, (
        f"the emission's append was treated as the effect window, got exit "
        f"{code}")

    [record] = _emission_records(wal_path)
    assert record["label"] == "Host.read"
    assert record["kind"] == "emission"
    # and it is the only record the child wrote
    assert len(replay.WriteAheadLog.read(wal_path)["records"]) == 1


def test_any_arms_the_fault_point_on_the_effect_record(tmp_path):
    """`any` matches the effect without naming it: the child dies before the
    append, the effect it performed stays in the world and the log has no
    record of it."""
    code, effect, wal_path = _run_child(tmp_path, crash_at="any")
    assert code == 137, (
        f"`any` did not arm the fault point, got exit {code}")
    assert os.path.exists(effect), "the effect did not take place"
    assert _effect_records(wal_path) == [], (
        "the effect record landed even though `any` armed the fault point")
    assert replay.WriteAheadLog.read(wal_path)["records"] == []


def test_a_label_no_step_carries_leaves_the_record_alone(tmp_path):
    """A label that matches neither the step's label nor its effect name does
    not arm the fault point: the child completes and its record lands, so the
    match is not "crash on anything armed"."""
    code, effect, wal_path = _run_child(tmp_path, crash_at="no-such-effect")
    assert code == 0, (
        f"an unmatched label armed the fault point, got exit {code}")
    assert os.path.exists(effect), "the effect did not take place"

    [record] = _effect_records(wal_path)
    assert record["label"] == "activation/effect"
    assert record["kind"] == "effect"

    report = recover(wal_path)
    assert report["verdict"] == "rolled-back"
    assert report["committedEffects"] == 1
    assert "activation/effect" in render(report)
