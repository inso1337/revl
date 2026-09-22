"""A gate step must not be able to skip the test step below it (issue #1358).

`.github/workflows/ci.yml`'s `frontend` job ran six independent commands as six
plain steps:

    python3 tools/conformance.py --check-readme
    python3 tools/selfhost_coverage.py --check
    python3 tools/oracle_construct_reach.py --check
    python3 tools/corpus_provenance.py --check
    python3 tools/docgen.py --check
    pytest tests/ -q

A step with no `if:` carries GitHub's default condition, `success()`, so each
one was a precondition for every step after it. `conformance.py --check-readme`
had been failing against a stale conformance matrix, which meant the four gates
after it and the root suite all reported `skipped`. Measured on main's own run
35636928793 (sha 421695acb): step 7 `failure`, steps 8 through 12 `skipped`.
`pytest tests/ -q` was not running in CI, on that main or on any pull request
built from it. #1358 counts what accumulated behind the green tick; one item is
`tools/docgen.py --check` drift growing from 3 findings to 9, because
`frontend` is the only job that runs it.

The fix is a condition on the steps, not a reordering and not a new job. A
reordering moves the silence rather than removing it, and would have moved it
onto these five gates, since the root suite is the step that is currently red.
A separate job costs three runner slots (`frontend` is a 3-version matrix) on a
repository whose runner concurrency is the throughput limit. The condition is
`!cancelled()` rather than `always()` because this workflow sets
`cancel-in-progress: true`: under `always()` a superseded run would push on
through `pytest tests/ -q` after being cancelled, spending exactly the runner
minutes that are scarce. The job still fails when any step fails.

This module is the structural half. It holds three things:

  * the jobs the fix touches (`frontend`, `backend-go`) have no step after
    their install step that can skip a later step;
  * a census over EVERY job: a gate step that can silence a test step below it
    has to be named in `PRECONDITIONS_BY_DESIGN` with a reason. A new instance
    of the #1358 shape lands as a red here, not as a quiet skip in a log;
  * the census predicate is seen to fire, on the exact pre-fix shape, so this
    is not one more gate that has never failed.

The last one is the point of the issue. `tests/test_check_vision_claims.py`
states the same discipline for the vision gate.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github/workflows/ci.yml"

# What counts as a test step: a command that runs a suite. Anything else in a
# job is provisioning (pip, npm ci, a toolchain download, a setup script) or a
# gate.
TEST_STEP = re.compile(r"(?<![\w-])(pytest|go test|cargo test|npx vitest|npm test|mvn test)(?![\w-])")

# What counts as a gate step: one of this repository's own `--check`-style
# tools. These fail on committed-bytes drift, which is independent of whether
# the tests below them pass, so a gate silencing a test is never intended.
# Provisioning steps are deliberately NOT in here: a test step after a failed
# `npm ci` has nothing to measure, and skipping it is correct.
GATE_STEP = re.compile(r"tools/[A-Za-z0-9_]+\.py\s+(--[A-Za-z0-9-]*check|--self-test|--strict)")

# A condition that survives a previous step's failure. `always()` also survives
# cancellation, which this workflow does not want (see the module docstring),
# but both forms answer the question this census asks.
SURVIVES = ("always()", "!cancelled()")

# The one gate/test pair that is a precondition on purpose, keyed by
# (job, the gate's command). Adding an entry is a decision with a cost, which
# is why it is an exact map and not a prefix rule.
PRECONDITIONS_BY_DESIGN = {
    ("conformance", "python3 tools/conformance.py --check-toolchains"): (
        "The `conformance` job is the one place every tier's compiler exists at "
        "once, and `tests/test_conformance_validate.py` SKIPS a tier whose "
        "toolchain is absent rather than failing. A missing toolchain therefore "
        "turns the tests below into green lines that measured nothing, which is "
        "worse than not running them. This gate answers 'is this job able to "
        "measure anything', so it is a real precondition, not a drift gate."
    ),
}

# The jobs #1358 repaired. Every run step after the step with `id: install`
# must carry a surviving condition.
INDEPENDENT_AFTER_INSTALL = ("frontend", "backend-go")


def _workflow(text: str | None = None) -> dict:
    return yaml.safe_load(text if text is not None else CI_YML.read_text(encoding="utf-8"))


def _run_steps(job: dict) -> list[tuple[int, dict]]:
    return [(i, s) for i, s in enumerate(job.get("steps") or []) if s.get("run")]


def _survives(step: dict) -> bool:
    cond = step.get("if") or ""
    return any(form in cond for form in SURVIVES)


def census(workflow: dict) -> dict[tuple[str, str], list[str]]:
    """(job, gate command) -> the test commands that gate can skip.

    A pair is reported when a gate step with no surviving condition sits above
    a test step with no surviving condition in the same job, which is the exact
    shape that kept `pytest tests/ -q` out of CI.
    """
    found: dict[tuple[str, str], list[str]] = {}
    for name, job in (workflow.get("jobs") or {}).items():
        steps = _run_steps(job)
        for pos, (_, step) in enumerate(steps):
            run = step["run"].strip()
            if _survives(step) or not TEST_STEP.search(run):
                continue
            for _, earlier in steps[:pos]:
                text = earlier["run"].strip()
                if _survives(earlier) or TEST_STEP.search(text):
                    continue
                if GATE_STEP.search(text):
                    key = (name, text.splitlines()[0].strip())
                    found.setdefault(key, []).append(run.splitlines()[0].strip())
    return found


# --- the repaired jobs ----------------------------------------------------- #
@pytest.mark.parametrize("job_name", INDEPENDENT_AFTER_INSTALL)
def test_no_step_after_the_install_step_can_skip_a_later_one(job_name):
    """The #1358 fix itself. `pip install -e ".[test]"` is the one genuine
    precondition in these jobs, so it carries `id: install` and everything
    below it runs unless the install failed or the run was cancelled."""
    job = _workflow()["jobs"][job_name]
    steps = _run_steps(job)
    install = [i for i, s in steps if s.get("id") == "install"]
    assert len(install) == 1, (
        f"`{job_name}` needs exactly one step with `id: install` for the later "
        f"steps to name as their precondition; found {install}"
    )
    after = [(i, s) for i, s in steps if i > install[0]]
    assert after, f"`{job_name}` has no steps after its install step"
    bad = [s["run"].strip().splitlines()[0] for _, s in after if not _survives(s)]
    assert not bad, (
        f"`{job_name}`: {len(bad)} step(s) after the install step still carry "
        f"the default `success()` condition, so a failure in any earlier step "
        f"skips them: {bad}. Issue #1358 is what happens next. Add "
        f"`if: ${{{{ !cancelled() && steps.install.outcome == 'success' }}}}`."
    )


def test_the_root_suite_is_one_of_the_steps_that_cannot_be_skipped():
    """Naming the step the issue is about, so a rename or a move that drops it
    out of `frontend` fails here rather than passing by vacuity."""
    steps = _run_steps(_workflow()["jobs"]["frontend"])
    suite = [s for _, s in steps if s["run"].strip() == "pytest tests/ -q"]
    assert len(suite) == 1, (
        "`frontend` no longer runs `pytest tests/ -q` as its own step. That is "
        "the root suite; if it moved, move this assertion with it and say where"
    )
    assert _survives(suite[0]), "`pytest tests/ -q` can be skipped by an earlier step"


def test_the_condition_is_not_cancelled_rather_than_always():
    """`always()` would keep a cancelled run working through the root suite.
    The workflow sets `cancel-in-progress: true`, so that is real runner time,
    on the resource that is this repository's throughput limit."""
    workflow = _workflow()
    assert workflow["concurrency"]["cancel-in-progress"] is True, (
        "this assertion exists because runs are cancelled in progress; if that "
        "changed, re-weigh `always()` against `!cancelled()` rather than "
        "deleting this"
    )
    for job_name in INDEPENDENT_AFTER_INSTALL:
        for _, step in _run_steps(workflow["jobs"][job_name]):
            cond = step.get("if") or ""
            assert "always()" not in cond, (
                f"`{job_name}` uses `always()`: a cancelled run would still "
                f"execute `{step['run'].strip().splitlines()[0]}`. Use "
                f"`!cancelled()`"
            )


# --- the sweep, pinned ----------------------------------------------------- #
def test_no_job_lets_a_drift_gate_skip_a_test_step():
    """Item 4 of the issue: `frontend` was found by accident, so ask every job.

    On the shipped bytes this reports `conformance` and nothing else, and that
    one is a precondition by design (see `PRECONDITIONS_BY_DESIGN`). The two
    jobs the fix repaired, `frontend` and `backend-go`, were the other two."""
    found = census(_workflow())
    undeclared = {k: v for k, v in found.items() if k not in PRECONDITIONS_BY_DESIGN}
    assert not undeclared, (
        "a gate step can skip a test step below it, which is issue #1358's "
        "shape:\n"
        + "\n".join(
            f"  job {job}: `{gate}` skips {len(tests)} test step(s): {tests}"
            for (job, gate), tests in sorted(undeclared.items())
        )
        + "\nEither give the test step a surviving condition, or add the pair "
        "to PRECONDITIONS_BY_DESIGN with the reason it is a real precondition."
    )
    stale = set(PRECONDITIONS_BY_DESIGN) - set(found)
    assert not stale, (
        f"PRECONDITIONS_BY_DESIGN names pairs that no longer exist: {sorted(stale)}. "
        "An exception that outlived its step reads as a considered decision and "
        "is not one"
    )


# --- the census is seen to fail ------------------------------------------- #
_PRE_FIX_FRONTEND = """
name: ci
concurrency:
  group: ci
  cancel-in-progress: true
jobs:
  frontend:
    runs-on: ubuntu-latest
    steps:
      - run: pip install -e ".[test]"
      - run: python3 tools/conformance.py --check-readme
      - run: python3 tools/docgen.py --check
      - run: pytest tests/ -q
"""

_FIXED_FRONTEND = """
name: ci
concurrency:
  group: ci
  cancel-in-progress: true
jobs:
  frontend:
    runs-on: ubuntu-latest
    steps:
      - id: install
        run: pip install -e ".[test]"
      - run: python3 tools/conformance.py --check-readme
        if: ${{ !cancelled() && steps.install.outcome == 'success' }}
      - run: python3 tools/docgen.py --check
        if: ${{ !cancelled() && steps.install.outcome == 'success' }}
      - run: pytest tests/ -q
        if: ${{ !cancelled() && steps.install.outcome == 'success' }}
"""


def test_the_census_reports_the_shape_that_shipped():
    """The control and the treatment, on the bytes that were on main. Without
    this the module is a gate that has never been watched fail, which is the
    thing issue #1358 is about."""
    before = census(_workflow(_PRE_FIX_FRONTEND))
    assert set(before) == {
        ("frontend", "python3 tools/conformance.py --check-readme"),
        ("frontend", "python3 tools/docgen.py --check"),
    }, before
    assert all(v == ["pytest tests/ -q"] for v in before.values()), before

    after = census(_workflow(_FIXED_FRONTEND))
    assert after == {}, after


def test_the_census_is_not_satisfied_by_a_reordering():
    """Moving `pytest tests/ -q` above the gates clears the census for the
    suite and re-creates it for the gates, which is why the fix is a condition.
    Stated as a test so the next reader does not have to rediscover it."""
    reordered = _PRE_FIX_FRONTEND.replace(
        """      - run: python3 tools/conformance.py --check-readme
      - run: python3 tools/docgen.py --check
      - run: pytest tests/ -q""",
        """      - run: pytest tests/ -q
      - run: python3 tools/conformance.py --check-readme
      - run: python3 tools/docgen.py --check""",
    )
    assert "- run: pytest tests/ -q\n      - run: python3" in reordered, (
        "the reorder fixture did not apply; the pre-fix fixture above changed"
    )
    # The census asks only about gates above tests, so a reorder reads clean
    # here while the gates are now the steps that get skipped.
    assert census(_workflow(reordered)) == {}
    steps = _run_steps(_workflow(reordered)["jobs"]["frontend"])
    gates = [s for _, s in steps if GATE_STEP.search(s["run"]) and not _survives(s)]
    assert len(gates) == 2, (
        "after the reorder the two gates are the unconditional steps below a "
        "test step, so the suite's failure now silences them: the silence "
        f"moved rather than went away; found {len(gates)}"
    )
