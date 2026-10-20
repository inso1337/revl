"""Issue #2642: an affected selection that all skipped must not read as green.

`root-suite-affected` runs the selection `tools/affected_tests.py` computes on
an interpreter with no cordis-py runtime. A selected file whose every test
carries the `find_spec("cordis") is None` guard therefore reports `2 skipped`,
pytest exits 0, and the merge box sees a green check for a run that executed
nothing. This is the issue's own evidence:
`tests/test_swap_export_area_host_code_2628.py` was the whole selection and its
job log read `2 skipped in 1.10s` with conclusion success.

`tools/affected_pytest_receipt.py` reads the pytest summary back and decides
whether the selection counted. The verdicts below are pinned against synthetic
summary output, so the rule is tested without a runtime or a branch-protection
change. The last test pins the workflow wiring: the receipt is read after the
pytest invocation, and only a shard may pass with no tests.

A later edit that drops the receipt step, or makes the job accept an all-skip
summary again, fails here rather than quietly restoring the green skip.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
receipt = load_by_path("revl_affected_pytest_receipt", ROOT / "tools" / "affected_pytest_receipt.py")

SELECTED = "tests/test_swap_export_area_host_code_2628.py"


def _judge(output, *, pytest_exit=0, allow_empty_shard=False):
    return receipt.judge(output, pytest_exit=pytest_exit,
                         allow_empty_shard=allow_empty_shard)


def test_all_skipped_selection_is_red_with_named_counts():
    """The issue's log line: two tests selected, two skipped, zero executed."""
    verdict = _judge("ss                                                                       [100%]\n"
                     "2 skipped in 1.10s\n")
    assert verdict.ok is False
    assert verdict.exit_code == 3
    assert verdict.counts.passed == 0
    assert verdict.counts.skipped == 2
    assert verdict.counts.not_run == 2
    assert verdict.counts.executed == 0
    assert verdict.reason == "every selected test was skipped (2 skipped, 0 executed)"


def test_partial_skip_is_green_because_the_selected_tests_ran():
    verdict = _judge("3 passed, 2 skipped in 4.20s\n")
    assert verdict.ok is True
    assert verdict.exit_code == 0
    assert verdict.counts.passed == 3
    assert verdict.counts.skipped == 2
    assert verdict.counts.not_run == 2
    assert "3 selected test(s) executed, 2 skipped" == verdict.reason


def test_a_real_pass_is_green():
    verdict = _judge("8 passed in 1.02s\n")
    assert verdict.ok is True
    assert verdict.counts.passed == 8
    assert verdict.counts.skipped == 0
    assert verdict.counts.not_run == 0


def test_zero_collected_for_a_requested_selection_is_red():
    verdict = _judge("no tests ran in 0.01s\n", pytest_exit=5)
    assert verdict.ok is False
    assert verdict.exit_code == 5
    assert "collected no tests" in verdict.reason


_EMPTY_SHARD = ("REVL_TEST_SHARD 2/4: 0 file(s), 0 test(s) of 8 collected\n"
                "\n"
                "8 deselected in 0.17s\n")


def test_an_empty_shard_is_the_only_allowed_zero():
    """The four-shard split may hand a shard no file; the other shards run it."""
    verdict = _judge(_EMPTY_SHARD, pytest_exit=5, allow_empty_shard=True)
    assert verdict.ok is True
    assert verdict.counts.collected == 0
    assert verdict.reason == "shard assigned no test file"


def test_an_empty_shard_without_the_shard_record_is_red():
    """A collection that came back empty is not the split's deliberate empty."""
    verdict = _judge("8 deselected in 0.17s\n", pytest_exit=5,
                     allow_empty_shard=True)
    assert verdict.ok is False
    assert verdict.exit_code == 5
    assert "no shard recorded itself empty" in verdict.reason


def test_allow_empty_shard_does_not_mask_an_all_skip_selection():
    verdict = _judge("2 skipped in 1.10s\n", allow_empty_shard=True)
    assert verdict.ok is False
    assert verdict.exit_code == 3
    assert verdict.counts.skipped == 2
    assert verdict.reason == "every selected test was skipped (2 skipped, 0 executed)"


def test_an_interrupted_pytest_is_red():
    verdict = _judge("2 passed in 1.0s\n", pytest_exit=2)
    assert verdict.ok is False
    assert verdict.exit_code == 2
    assert "did not complete" in verdict.reason


def test_an_unreadable_summary_is_red():
    verdict = _judge("something went wrong\n")
    assert verdict.ok is False
    assert verdict.exit_code == 1
    assert "could not read a pytest summary" in verdict.reason


def test_expected_failures_count_as_executed():
    """xfail ran the test, so it is not an all-skip selection."""
    verdict = _judge("2 xfailed in 1.0s\n")
    assert verdict.ok is True
    assert verdict.counts.passed == 0
    assert verdict.counts.executed == 2
    assert verdict.reason == "2 selected test(s) executed"


def test_a_failure_is_never_green():
    verdict = _judge("1 failed, 2 passed in 2.31s\n", pytest_exit=1)
    assert verdict.ok is False
    assert verdict.exit_code == 1
    assert verdict.counts.passed == 2
    assert verdict.counts.failed == 1


def test_warnings_do_not_hide_a_pass():
    verdict = _judge("1 passed, 2 warnings in 1.00s\n")
    assert verdict.ok is True
    assert verdict.counts.passed == 1


def test_the_last_summary_line_wins():
    """A test that prints a summary-shaped line cannot make the run green."""
    verdict = _judge("4 passed in 0.10s\n"  # a test's own output
                     "ss\n"
                     "2 skipped in 1.10s\n")
    assert verdict.ok is False
    assert verdict.counts.skipped == 2
    assert verdict.counts.passed == 0


def test_the_receipt_names_the_selection_and_the_counts():
    verdict = _judge("2 skipped in 1.10s\n")
    line = receipt.receipt_line(verdict, SELECTED)
    assert "selection=[tests/test_swap_export_area_host_code_2628.py]" in line
    assert "passed=0" in line
    assert "skipped=2" in line
    assert "not-run=2" in line
    assert "verdict=FAIL" in line
    assert "every selected test was skipped" in line


def _affected_run_step():
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    job = jobs["root-suite-affected"]
    for step in job["steps"]:
        run = step.get("run") or ""
        if "tools/affected_pytest_receipt.py" in run:
            return step, run
    pytest.fail("root-suite-affected never reads the pytest summary back")


def _command_line(script, needle):
    """The first non-comment line containing `needle`, or -1."""
    if needle not in script:
        return -1
    for number, line in enumerate(script.splitlines()):
        if line.lstrip().startswith("#"):
            continue
        if needle in line:
            return number
    return -1


def test_the_affected_job_reads_the_summary_after_pytest():
    step, script = _affected_run_step()
    assert step.get("if") is None, (
        "the step carrying the receipt gained a condition, so it can now report "
        "skipping and the merge box cannot tell that from a pass"
    )
    assert not step.get("continue-on-error"), (
        "the receipt step can no longer fail the job"
    )
    assert _command_line(script, "pytest $SEL_PYTEST -q") >= 0, (
        "root-suite-affected no longer runs the selector's nodes; the receipt "
        "reads that run and this pin needs to move with a deliberate change"
    )
    assert _command_line(script, "pytest $SEL_PYTEST -q") < _command_line(
        script, "tools/affected_pytest_receipt.py"), (
        "the receipt is invoked before pytest, so it reads nothing"
    )
    assert "--selection \"$SEL_PYTEST\"" in script
    assert "--pytest-exit \"$rc\"" in script
    assert "--allow-empty-shard" in script, (
        "the four-shard split's deliberately empty shard would otherwise fail"
    )
    assert '[ "$SHARD_INDEX" != "1" ]' in script, (
        "shard 1 always receives the heaviest unit, so an empty shard 1 must "
        "not be allowed to pass without executing the selection"
    )
    assert "tee \"$log\"" in script, "the run's output is not captured for the receipt"
