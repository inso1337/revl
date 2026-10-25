"""The seeded fault profile (issue #2728, slice 2).

A profile is a seeded list of fault events drawn from the sweep points
(`fault.sweep_units`) and driven through the sweep's own machinery. The draw,
the classification and the report are pure and tested without a runtime; the
real activations are gated behind `needs_cordis`.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402

# Sink has one top-level step, Store four: five sweep points in all
SOURCE = '''
service Log { emission fn write(line: Str) }
service Cache { fn put(k: Str, v: Str) }

component Sink provides log: Log {
  provide log { fn write(line) { } }
}

component Store requires log: Log provides cache: Cache {
  config { url: Str = "postgres://primary/app" }
  let scratch = effect Map.new() undo scratch.drop()
  emit log.write("store coming up")
  let pool = effect Pool.open(config.url, 2) undo pool.close()
  provide cache {
    fn put(k, v) { effect scratch.insert(k, v) undo scratch.remove(k) }
  }
}
'''


def _ir() -> dict:
    return compile_source(SOURCE, "profile.rvl")


def _points(events: list) -> list:
    return [(e["component"], e["step"]) for e in events]


# ------------------------------------------------------------------ the draw


def test_the_same_seed_draws_the_same_sequence():
    first = fault_mod.profile_events(_ir(), 7, count=3)
    second = fault_mod.profile_events(_ir(), 7, count=3)
    assert first == second
    assert len(first) == 3
    assert [e["event"] for e in first] == [1, 2, 3]
    assert all(e["kind"] == "step-fail" for e in first)
    # every event is a sweep unit, carrying the sweep's full assertion set
    assert all(e["assert"] == ["failed", "no-residue", "inverses-lifo",
                               "siblings-unaffected"] for e in first)


def test_the_same_seed_draws_the_same_sequence_in_two_processes():
    """Two runs, two interpreters with different hash seeds: the draw must not
    depend on anything but the seed and the IR."""
    script = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from revl import compile_source\n"
        "from revl.fault import profile_events\n"
        "ir = compile_source(sys.argv[2], 'profile.rvl')\n"
        "print(json.dumps([[e['component'], e['step']] "
        "for e in profile_events(ir, 7, count=4)]))\n")
    runs = []
    for hash_seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        done = subprocess.run(
            [sys.executable, "-c", script, str(ROOT / "src"), SOURCE],
            capture_output=True, text=True, env=env, check=True)
        runs.append(json.loads(done.stdout))
    assert runs[0] == runs[1]
    assert [tuple(p) for p in runs[0]] == _points(
        fault_mod.profile_events(_ir(), 7, count=4))


def test_another_seed_draws_another_sequence():
    """Non-vacuity for the two tests above: the seed does steer the draw."""
    draws = {tuple(_points(fault_mod.profile_events(_ir(), seed, count=3)))
             for seed in range(6)}
    assert len(draws) > 1


def test_a_small_composition_gets_every_point_once():
    events = fault_mod.profile_events(_ir(), 3, count=50)
    assert sorted(_points(events)) == [("Sink", 1), ("Store", 1), ("Store", 2),
                                       ("Store", 3), ("Store", 4)]


def test_a_profile_needs_at_least_one_event():
    with pytest.raises(ValueError, match="at least one event"):
        fault_mod.profile_events(_ir(), 1, count=0)


# --------------------------------------------------------- the report (pure)


def _event(index, component="Store", step=1):
    return {"event": index, "kind": "step-fail", "component": component,
            "step": step, "where": f"step {step}"}


def _fabricated():
    return [
        (_event(1, step=2), [], [], ["irreversible: an emission stands"]),
        (_event(2, step=3), [("no-residue", "residue in the effect stack")], [], []),
        (_event(3, "Sink", 1), [], ["the fault-profile driver raised RuntimeError: x"], []),
    ]


def test_the_dossier_counts_contained_failed_and_unclassified():
    dossier = fault_mod._profile_dossier(11, 5, _fabricated())
    assert dossier["seed"] == 11
    assert dossier["status"] == "failed"
    # the three states are disjoint and add up to the injected count
    assert dossier["counts"] == {"points": 5, "injected": 3, "contained": 1,
                                 "failed": 1, "unclassified": 1,
                                 "unreachable": 0}
    statuses = [e["status"] for e in dossier["events"]]
    assert statuses == ["contained", "fail", "unclassified"]
    assert dossier["events"][1]["causes"] == ["no-residue"]
    assert dossier["events"][1]["problems"] == [
        "[no-residue] residue in the effect stack"]
    assert dossier["events"][2]["problems"][0].startswith("[unclassified] ")


def test_a_clean_profile_reports_zero_unclassified():
    dossier = fault_mod._profile_dossier(4, 5, [(_event(1), [], [], [])])
    assert dossier["status"] == "passed"
    assert dossier["counts"]["unclassified"] == 0


def test_the_report_prints_the_seed_the_faults_and_the_unclassified_count():
    results = _fabricated()
    dossier = fault_mod._profile_dossier(11, 5, results)
    lines: list = []
    fault_mod._format_profile(dossier, results, lines.append)
    out = "\n".join(lines)
    assert lines[0].startswith("fault profile, seed 11")
    assert "3 fault(s) drawn from 5 sweep point(s)" in lines[0]
    assert "PASS event 1: step-fail Store step 2" in out
    assert "FAIL event 2: step-fail Store step 3" in out
    assert "UNCLASSIFIED event 3: step-fail Sink step 1" in out
    assert "note: irreversible: an emission stands" in out
    assert lines[-1] == ("seed 11: injected 3 fault(s), 1 contained, "
                         "1 failed, 1 unclassified")


def test_a_profile_that_injected_nothing_is_empty_not_passed():
    unreachable = [{"component": "C", "where": "step 1 > then > step 1 (emit)",
                    "reason": "nested inside an `if`"}]
    dossier = fault_mod._profile_dossier(2, 0, [], unreachable)
    assert dossier["status"] == "empty"
    assert dossier["counts"]["injected"] == 0
    assert dossier["counts"]["unreachable"] == 1
    lines: list = []
    fault_mod._format_profile(dossier, [], lines.append)
    out = "\n".join(lines)
    assert "UNREACHABLE C step 1 > then > step 1 (emit)" in out
    assert "nothing injected" in out


def test_classified_problems_name_the_assertion_behind_each_problem():
    class _State:
        FAILED = "FAILED"
        ACTIVE = "ACTIVE"

    outcome = fault_mod._Outcome()
    outcome.state = _State.ACTIVE          # did not land FAILED
    outcome.siblings = {"Sink": _State.ACTIVE}
    outcome.baseline = outcome.unwound = outcome.settled = {
        "registry": 0, "provisions": [], "listeners": 0, "effects": 0}
    unit = {"step": 2, "assert": ["failed", "no-residue", "inverses-lifo",
                                  "siblings-unaffected"]}
    problems = fault_mod._classified_problems(unit, outcome, _State)
    assert [cause for cause, _ in problems] == ["failed"]
    assert "did not land FAILED" in problems[0][1]


def test_classified_problems_are_exactly_the_judged_problems():
    """Judging one assertion at a time must neither drop nor repeat a problem
    the full judgement reports, on an outcome that breaks several of them."""
    class _State:
        FAILED = "FAILED"
        ACTIVE = "ACTIVE"
        PENDING = "PENDING"

    outcome = fault_mod._Outcome()
    outcome.state = _State.ACTIVE                    # breaks `failed`
    outcome.siblings = {"Sink": _State.PENDING}      # breaks `siblings-unaffected`
    outcome.baseline = outcome.unwound = {
        "registry": 0, "provisions": [], "listeners": 0, "effects": 0}
    outcome.settled = dict(outcome.baseline, effects=2)   # breaks `no-residue`
    outcome.accumulated = 2
    outcome.labels = ["a", "b"]
    outcome.lifo_violation = (1, 1, 2)               # breaks `inverses-lifo`
    unit = {"step": 2, "assert": ["failed", "no-residue", "inverses-lifo",
                                  "siblings-unaffected"]}
    tagged = fault_mod._classified_problems(unit, outcome, _State)
    assert [text for _, text in tagged] == fault_mod._judge(unit, outcome, _State)
    assert [cause for cause, _ in tagged] == [
        "failed", "no-residue", "inverses-lifo", "siblings-unaffected"]


# ----------------------------------------------------------------- the CLI


def test_the_cli_takes_a_seed_and_an_event_count():
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["test", "x.rvl", "--fault-profile", "42", "--fault-profile-events", "3"])
    assert args.fault_profile == 42
    assert args.fault_profile_events == 3


def test_an_event_count_without_a_seed_is_a_usage_error(capsys):
    from revl.test import test_command

    assert test_command(_ir(), "py", fault_profile_events=3) == 2
    assert "--fault-profile SEED" in capsys.readouterr().err


def test_a_zero_event_count_is_refused_even_without_components(capsys):
    from revl.test import test_command

    ir = dict(_ir(), components=[])
    assert test_command(ir, "py", fault_profile=1, fault_profile_events=0) == 2
    assert "at least 1" in capsys.readouterr().err


@pytest.mark.parametrize("mode", [{"sweep": True}, {"mock_requires": True},
                                  {"schedule_seed": 3}, {"schedule_seeds": 3}])
def test_the_profile_does_not_combine_with_another_run_mode(capsys, mode):
    from revl.test import test_command

    assert test_command(_ir(), "py", fault_profile=1, **mode) == 2
    assert "its own run mode" in capsys.readouterr().err


def test_the_profile_skips_with_a_reason_without_the_runtime(capsys, monkeypatch):
    import revl.test as test_mod

    monkeypatch.setattr(test_mod, "_cordis_available", lambda: False)
    assert test_mod.test_command(_ir(), "py", fault_profile=5) == 0
    out = capsys.readouterr().out
    assert "[fault-profile] skipped" in out and "PASS" not in out


# ------------------------------------------------------------ real activations

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the fault profile activates for real; it needs the cordis-py "
           "runtime (sh backends/python/setup.sh)")


@needs_cordis
def test_a_real_profile_run_prints_the_seed_faults_and_unclassified(capsys):
    from revl.test import test_command

    code = test_command(_ir(), "py", fault_profile=7, fault_profile_events=3)
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.startswith("fault profile, seed 7, py reference tier: "
                          "3 fault(s) drawn from 5 sweep point(s)")
    expected = fault_mod.profile_events(_ir(), 7, count=3)
    for event in expected:
        assert (f"PASS event {event['event']}: step-fail {event['component']} "
                f"{event['where']}") in out
    assert "seed 7: injected 3 fault(s), 3 contained, 0 failed, 0 unclassified" in out


@needs_cordis
def test_two_real_runs_with_one_seed_inject_the_same_faults():
    _, first = fault_mod.run_fault_profile(_ir(), 9, count=4, out=lambda _l: None)
    _, second = fault_mod.run_fault_profile(_ir(), 9, count=4, out=lambda _l: None)
    assert first["events"] == second["events"]
    assert first["counts"]["injected"] == 4


@needs_cordis
def test_a_driver_crash_is_counted_unclassified_and_fails_the_run(
        capsys, monkeypatch):
    from revl.test import test_command

    real_drive = fault_mod._drive
    target = fault_mod.profile_events(_ir(), 7, count=3)[1]

    async def crashing_drive(ir, unit, *args, **kwargs):
        if (unit["component"], unit["step"]) == (target["component"], target["step"]):
            raise RuntimeError("probe lost the host")
        return await real_drive(ir, unit, *args, **kwargs)

    monkeypatch.setattr(fault_mod, "_drive", crashing_drive)
    code = test_command(_ir(), "py", fault_profile=7, fault_profile_events=3)
    captured = capsys.readouterr()
    assert code == 1
    assert "UNCLASSIFIED event 2" in captured.out
    assert ("[unclassified] the fault-profile driver raised RuntimeError: "
            "probe lost the host") in captured.out
    assert "2 contained, 0 failed, 1 unclassified" in captured.out
    assert "1 with no classified cause" in captured.err


@needs_cordis
def test_a_profile_with_nothing_to_inject_does_not_pass(capsys):
    from revl.test import test_command

    ir = _ir()
    for component in ir["components"]:
        component["body"] = []
    code = test_command(ir, "py", fault_profile=1)
    captured = capsys.readouterr()
    assert code == 1
    assert "0 fault(s) drawn from 0 sweep point(s)" in captured.out
    assert "nothing was injected" in captured.err
