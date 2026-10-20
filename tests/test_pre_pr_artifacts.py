"""The decision half of the pre-PR generated-artifact checks (issue #2645).

`tools/pre_pr.sh` is the fast loop that was missing the two checks dominating
failed pull-request runs: the committed census records and the emitted
goldens. The interesting part is not running them, it is deciding whether the
census re-run can be skipped, so these tests drive
`tools/pre_pr_artifacts.py` through a fake runner: no census, no emitter, no
repo state, and no change to the checked-in records.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "pre_pr_artifacts.py"


def _helper():
    if not HELPER.exists():
        pytest.fail(
            "tools/pre_pr_artifacts.py is missing: the pre-PR loop still runs "
            "neither the census records check nor the emitted-goldens check"
        )
    return load_by_path("revl_pre_pr_artifacts", HELPER)


class Runner:
    """A `subprocess.run` stand-in that answers from the command it is given.

    Nothing is executed, so a test can show a moved input, a skipped re-run or
    a stale record without a census or an emitter in the loop."""

    def __init__(self, *, diff=(0, "", ""), moved=(1, "", ""),
                 check=(0, "", ""), goldens=(0, "", "")):
        self.diff = diff
        self.moved = moved
        self.check = check
        self.goldens = goldens
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if "--moved-inputs" in argv:
            rc, out, err = self.moved
        elif "tools/census_artifact.py" in argv:
            rc, out, err = self.check
        elif "tools/regen_goldens.py" in argv:
            rc, out, err = self.goldens
        elif argv[:2] == ["git", "diff"]:
            rc, out, err = self.diff
        else:
            raise AssertionError(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, rc, out, err)


def _argvs(run):
    return [argv for argv, _ in run.calls]


def _ran(run, tool, flag):
    return any(tool in argv and flag in argv for argv in _argvs(run))


@pytest.mark.parametrize("status, check", [
    (0, True),    # at least one input moved
    (1, False),   # the only "nothing moved" answer
    (2, True),    # could not decide: check, never skip
    (3, True),    # an answer this filter does not define
    (255, True),  # a crash is not a "no"
])
def test_moved_inputs_only_switches_the_check_off_on_exit_1(status, check):
    assert _helper().census_warrants_check(status) is check


def test_empty_diff_skips_the_filter_and_the_rerun(capsys):
    run = Runner(diff=(0, "", ""))
    assert _helper().census_gate(run=run) == 0
    assert _argvs(run) == [["git", "diff", "--name-only", "origin/main"]]
    assert "nothing moved" in capsys.readouterr().out


def test_no_moved_input_skips_the_census_rerun(capsys):
    run = Runner(diff=(0, "tools/pre_pr.sh\n", ""), moved=(1, "", ""))
    assert _helper().census_gate(run=run) == 0
    assert not _ran(run, "tools/census_artifact.py", "--check")
    assert "not re-run" in capsys.readouterr().out
    moved_call = next(kwargs for argv, kwargs in run.calls
                      if "--moved-inputs" in argv)
    assert moved_call["input"] == "tools/pre_pr.sh\n"


def test_moved_input_runs_the_records_check_and_names_the_fix(capsys):
    run = Runner(diff=(0, "selfhost/lower.rvl\n", ""),
                 moved=(0, "selfhost/lower.rvl\n", ""),
                 check=(1, "", ""))
    assert _helper().census_gate(run=run) == 1
    out = capsys.readouterr().out
    assert "moved input: selfhost/lower.rvl" in out
    assert "python3 tools/census_artifact.py --write" in out
    assert _ran(run, "tools/census_artifact.py", "--check")


def test_current_records_pass_without_naming_a_fix(capsys):
    run = Runner(diff=(0, "selfhost/lower.rvl\n", ""),
                 moved=(0, "selfhost/lower.rvl\n", ""),
                 check=(0, "census artifact: the records are current.\n", ""))
    assert _helper().census_gate(run=run) == 0
    assert "python3 tools/census_artifact.py --write" not in capsys.readouterr().out


def test_filter_that_cannot_decide_checks_anyway(capsys):
    run = Runner(diff=(0, "selfhost/lower.rvl\n", ""), moved=(2, "", ""))
    assert _helper().census_gate(run=run) == 0
    assert "could not decide" in capsys.readouterr().out
    assert _ran(run, "tools/census_artifact.py", "--check")


def test_unreadable_diff_checks_anyway(capsys):
    run = Runner(diff=(128, "", "fatal: bad revision"))
    assert _helper().census_gate(run=run) == 0
    assert "checking anyway" in capsys.readouterr().out
    assert _ran(run, "tools/census_artifact.py", "--check")


def test_goldens_command_is_the_documented_check_mode():
    assert _helper().goldens_command("python3") == [
        "python3", "tools/regen_goldens.py", "--check", "--strict",
        "python", "typescript", "rust", "java", "wasm",
    ]


def test_goldens_pass_runs_the_check_mode(capsys):
    run = Runner(goldens=(0, "every golden checked matches\n", ""))
    assert _helper().goldens_gate(run=run) == 0
    assert _argvs(run) == [[
        _helper().sys.executable, "tools/regen_goldens.py", "--check", "--strict",
        "python", "typescript", "rust", "java", "wasm",
    ]]
    assert "python3 tools/regen_goldens.py" not in capsys.readouterr().out


def test_golden_drift_names_the_regen_command(capsys):
    run = Runner(goldens=(1, "DRIFT rust\n", ""))
    assert _helper().goldens_gate(run=run) == 1
    out = capsys.readouterr().out
    assert ("python3 tools/regen_goldens.py python typescript rust java wasm"
            in out)


def test_goldens_broken_is_not_reported_as_drift(capsys):
    run = Runner(goldens=(2, "", ""))
    assert _helper().goldens_gate(run=run) == 1
    out = capsys.readouterr().out
    assert "not a drift report" in out
    assert "python3 tools/regen_goldens.py" not in out
