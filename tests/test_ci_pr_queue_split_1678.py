"""Issue #1678: pull requests run the light jobs, the merge queue runs the rest.

`.github/workflows/ci.yml` skips seven heavy jobs on `pull_request` and runs
them on every other event, the merge queue (`merge_group`) first among them,
where the candidate is the merged result that lands. The seven contexts branch
protection requires (`lint` and the six `backend-*` jobs) must report on BOTH
`pull_request` and `merge_group`, or a queued PR waits forever for a check that
never runs.

Since issue #1817 two more jobs are conditional on a pull request: the six
`backend-*` jobs skip when `changes` reports `backends == 'false'` (no changed
path is one a backend job can observe), which the drain accepts as SKIPPED, and
`root-suite-affected` runs on pull requests only, because `frontend` runs the
whole root suite on every other event.

This file evaluates each job's `if:` and `needs:` per event and pins that split.
A job's `if:` is evaluated for real, not pattern-matched: the conditions in
ci.yml use only `github.event_name`, the two `changes` outputs,
`needs.lint.result`, `!cancelled()`, string (in)equality, `!`, `&&` and `||`,
and the evaluator refuses anything else, so a new condition shape fails here by
name instead of being read wrongly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

HEAVY = frozenset({
    "frontend", "frontend-cordis", "conformance", "formal", "temporal-exit",
    "sandbox-container", "sandbox-microvm",
})
BACKENDS = frozenset({
    "backend-python", "backend-typescript", "backend-wasm",
    "backend-rust", "backend-java", "backend-go",
})
REQUIRED = frozenset({"lint"}) | BACKENDS
# A job that runs on pull requests only, and the job that covers it on every
# other event (issue #1817).
PR_ONLY = {"root-suite-affected": "frontend"}
NON_PR_EVENTS = ("merge_group", "push", "schedule", "workflow_dispatch",
                 "workflow_call")

_TOKEN = re.compile(
    r"\s*(?:(?P<call>cancelled\(\))|(?P<op>\|\||&&|==|!=|!|\(|\))|'(?P<str>[^']*)'"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*))")


def _jobs(text: str | None = None) -> dict:
    return yaml.safe_load(text if text is not None else
                          CI.read_text(encoding="utf-8"))["jobs"]


def _needs(spec: dict) -> list:
    needs = spec.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _evaluate(cond, event: str, frontend: str, backends: str = "true") -> bool:
    """Evaluate a job-level `if:` for one event. Only the vocabulary ci.yml
    uses is understood; anything else raises. The run is never cancelled and
    a job's needs all succeeded (`_runs` only asks once they ran)."""
    if cond is None:
        return True
    text = str(cond).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    values = {"github.event_name": event,
              "needs.changes.outputs.frontend": frontend,
              "needs.changes.outputs.backends": backends,
              "needs.lint.result": "success"}
    out: list[str] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"cannot read the condition {cond!r} at {text[pos:]!r}")
        pos = m.end()
        if m.group("call"):
            out.append("False")
        elif m.group("op"):
            out.append({"||": " or ", "&&": " and ", "!": " not "}.get(
                m.group("op"), m.group("op")))
        elif m.group("str") is not None:
            out.append(repr(m.group("str")))
        else:
            name = m.group("name")
            if name not in values:
                raise ValueError(f"unknown name {name!r} in the condition {cond!r}")
            out.append(repr(values[name]))
    return bool(eval("".join(out), {"__builtins__": {}}, {}))  # noqa: S307


def _runs(jobs: dict, event: str, frontend: str = "true",
          backends: str = "true") -> set:
    """The jobs that run for *event*: `if:` true and every `needs:` ran."""
    ran: set = set()
    changed = True
    while changed:
        changed = False
        for name, spec in jobs.items():
            if name in ran:
                continue
            if not set(_needs(spec)) <= ran:
                continue
            if _evaluate(spec.get("if"), event, frontend, backends):
                ran.add(name)
                changed = True
    return ran


def _on_some_pull_request(jobs: dict) -> set:
    """A pull request can report either value of either `changes` output."""
    return set().union(*(_runs(jobs, "pull_request", f, b)
                         for f in ("true", "false") for b in ("true", "false")))


def test_the_evaluator_reads_every_condition_in_ci_yml():
    jobs = _jobs()
    for name, spec in jobs.items():
        for event in ("pull_request",) + NON_PR_EVENTS:
            _evaluate(spec.get("if"), event, "true", "false")  # raises on a new shape
    with pytest.raises(ValueError):
        _evaluate("${{ github.ref == 'refs/heads/main' }}", "push", "true")


def test_heavy_jobs_never_run_on_a_pull_request():
    jobs = _jobs()
    assert HEAVY <= set(jobs), sorted(HEAVY - set(jobs))
    leaked = sorted(HEAVY & _on_some_pull_request(jobs))
    assert not leaked, (
        f"{leaked} run on a pull request. Issue #1678 moved them to the merge "
        "queue: give each `if: ${{ github.event_name != 'pull_request' }}`.")


@pytest.mark.parametrize("event", NON_PR_EVENTS)
def test_heavy_jobs_run_on_every_other_event(event):
    missing = sorted(HEAVY - _runs(_jobs(), event))
    assert not missing, f"{missing} do not run on {event}"


@pytest.mark.parametrize("event", ("pull_request", "merge_group"))
@pytest.mark.parametrize("frontend", ("true", "false"))
def test_the_required_checks_report_on_pull_request_and_merge_group(event, frontend):
    """Either `frontend` output: a required job must not hang on that filter.
    (`backends == 'false'` is the one deliberate skip, pinned below.)"""
    missing = sorted(REQUIRED - _runs(_jobs(), event, frontend, "true"))
    assert not missing, (
        f"required check(s) {missing} do not run on {event}; branch protection "
        "would wait forever for them")


@pytest.mark.parametrize("frontend", ("true", "false"))
def test_the_backend_jobs_skip_only_on_a_pull_request_the_selection_clears(frontend):
    """Issue #1817. On a pull request whose changes no backend job can observe
    (`changes` reports `backends == 'false'`) the six backend jobs skip and
    `lint` still runs. On every other event, and on any other answer, all six
    run: an empty output (the selector step failed) is not 'false'."""
    jobs = _jobs()
    cleared = _runs(jobs, "pull_request", frontend, "false")
    assert not BACKENDS & cleared, sorted(BACKENDS & cleared)
    assert "lint" in cleared
    assert BACKENDS <= _runs(jobs, "pull_request", frontend, "")
    for event in NON_PR_EVENTS:
        assert BACKENDS <= _runs(jobs, event, frontend, "false"), event


def test_the_pull_request_skip_set_is_exactly_the_heavy_jobs():
    """Pin the split itself. Every job outside HEAVY runs on a pull request
    that touches code (`changes` reports 'true'), so moving a job to the queue
    is a deliberate edit here, not a side effect."""
    jobs = _jobs()
    skipped = set(jobs) - _runs(jobs, "pull_request", "true")
    assert skipped == HEAVY, (
        f"skipped on a code PR: {sorted(skipped)}; expected {sorted(HEAVY)}")


def test_every_job_runs_on_some_non_pull_request_event():
    """An env-gated test runs only where its job runs (see
    tests/test_env_gated_skips_run_somewhere.py), so no job may be PR-only
    unless another job covers it on every other event (`PR_ONLY`)."""
    jobs = _jobs()
    ran = set().union(*(_runs(jobs, e) for e in NON_PR_EVENTS))
    assert ran == set(jobs) - set(PR_ONLY), sorted(set(jobs) - set(PR_ONLY) - ran)
    for job, cover in PR_ONLY.items():
        for event in NON_PR_EVENTS:
            assert job not in _runs(jobs, event), (job, event)
            assert cover in _runs(jobs, event), (
                f"{job} runs on pull requests only, and {cover}, which covers "
                f"it elsewhere, does not run on {event}")
        assert job in _runs(jobs, "pull_request", "false", "false"), (
            f"{job} must run on every pull request")


def test_the_pin_fails_on_the_shape_it_replaced():
    """Anti-vacuity: put back the pre-#1678 gate on `frontend` and a code PR
    runs it again, so the PR-side check above would fail."""
    jobs = _jobs()
    jobs["frontend"]["needs"] = ["changes"]
    jobs["frontend"]["if"] = ("${{ github.event_name != 'pull_request' || "
                              "needs.changes.outputs.frontend == 'true' }}")
    assert "frontend" in _on_some_pull_request(jobs)
    assert "frontend" not in _runs(jobs, "pull_request", "false")
