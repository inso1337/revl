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
whole root suite on every other event. Issue #2672's review adds
`swap-template-cordis`, PR-only and advisory: `frontend-cordis` runs the same
swap and authoring suites with the cordis runtime on every other event. Issue
#2642's option B adds `cordis-affected`, also PR-only and advisory: it runs the
cordis half of a named affected selection under the pinned runtime (the FULL
fallback is bounded out and says so), and `frontend-cordis` covers the whole
guarded suite on every other event.

This file evaluates each job's `if:` and `needs:` per event and pins that split.
A job's `if:` is evaluated for real, not pattern-matched: the conditions in
ci.yml use only `github.event_name`, the four `changes` outputs,
`needs.lint.result`, `!cancelled()`, string (in)equality, `!`, `&&`, `||` and
parentheses, and the evaluator refuses anything else, so a new condition shape
fails here by name instead of being read wrongly.

The `swap-template-cordis` selection rule has its own section at the end
(dogfood adoption item 2): which changed paths select that job, and that the
job runs the changed MCP/swap suite which selected it.
"""

from __future__ import annotations

import os
import re
import subprocess
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
# other event (issue #1817; #2672's review and #2642's option B add two more
# PR-only jobs).
PR_ONLY = {
    "root-suite-affected": "frontend",
    "swap-template-cordis": "frontend-cordis",
    "cordis-affected": "frontend-cordis",
}
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


def _evaluate(cond, event: str, frontend: str, backends: str = "true",
              swap: str = "true", swap_tests: str = "") -> bool:
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
              # #2672's review adds the swap/authoring selector; the job skips
              # only on its exact 'false' answer, so a code PR is 'true'.
              "needs.changes.outputs.swap": swap,
              # Dogfood adoption item 2 adds the changed MCP/swap suites. An
              # empty answer (the selector step failed) fails open.
              "needs.changes.outputs.swap_tests": swap_tests,
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
          backends: str = "true", swap: str = "true",
          swap_tests: str = "") -> set:
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
            if _evaluate(spec.get("if"), event, frontend, backends, swap,
                         swap_tests):
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


# --------------------------------------------------------------------------
# The swap/authoring SELECTION rule (dogfood adoption item 2).
#
# Issue #2672's review made `swap-template-cordis` the PR path for the
# cordis-guarded swap and authoring suites, selected by `SWAP_RE`: a literal
# list of eleven suites plus the source files they observe. A pull request that
# changed an MCP test suite the list had never heard of (a new
# `tests/test_mcp_foo.py`, or an existing `tests/test_mcp_*.py` outside the
# eleven) selected nothing, although it changes the tests that job exists to
# run.
#
# The rule now has two answers, because they cost different things:
#
#   * `swap` (unchanged): the source side. It runs the eleven named suites,
#     so a change to one of those eleven still runs all eleven, exactly as
#     before.
#   * `swap_tests` (new): any changed `tests/test_mcp*.py` or
#     `tests/test_swap*.py`, the bare `tests/test_mcp.py` and
#     `tests/test_swap.py` and a new name included. It selects the job and the
#     job runs those suites under the pinned runtime instead of all eleven,
#     which is the honest cost for a test-only diff: editing an MCP suite the
#     explicit list does not name is no reason to run the eleven, and it is a
#     reason to execute the file where `root-suite-affected` can only skip it.
#
# These pins read the rule out of ci.yml and apply it. None restates the
# regexes, so widening them cannot leave a pin behind.
# --------------------------------------------------------------------------
CHANGES = "changes"
SWAP_JOB = "swap-template-cordis"
SWAP_STEP = "swap and authoring suites under the pinned runtime"


def _step(job: str, step_id: str) -> dict:
    for step in _jobs()[job]["steps"]:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"{job} has no step with id {step_id!r}")


def _swap_env() -> dict:
    env = _step(CHANGES, "swap").get("env") or {}
    assert {"SWAP_RE", "SWAP_TEST_RE"} <= set(env), (
        f"the swap selector lost a regex: {sorted(env)}")
    return env


def _swap_source(paths) -> bool:
    """The `swap` answer: does a changed path match the SOURCE selection?"""
    return any(re.search(_swap_env()["SWAP_RE"], p) for p in paths)


def _swap_tests(paths) -> list:
    """The `swap_tests` answer, as the step builds it: the matching changed
    suites, in the order the diff lists them (the step space-joins them)."""
    return [p for p in paths if re.search(_swap_env()["SWAP_TEST_RE"], p)]


def test_a_changed_mcp_or_swap_test_suite_selects_the_swap_job():
    """The gap itself: a suite the explicit list has never heard of, and the
    bare `tests/test_mcp.py` / `tests/test_swap.py` the pattern once missed."""
    for changed in ("tests/test_mcp_brand_new.py",
                    "tests/test_swap_brand_new.py",
                    "tests/test_mcp.py",        # the canonical MCP suite
                    "tests/test_swap.py",       # the canonical swap suite
                    "tests/test_mcp_proxy.py",  # existing, never on the list
                    "tests/test_mcp_authoring_loop_1704.py"):  # on the list
        assert _swap_tests([changed]) == [changed], changed


def test_every_mcp_and_swap_suite_in_the_tree_selects_the_swap_job():
    """Tree-wide, not a sample: a suite renamed out of the pattern, or a new
    one that uses a character the pattern does not admit, fails here rather
    than silently selecting nothing (which was the original bug). The glob
    includes the bare names, so `tests/test_mcp.py` is covered too."""
    suites = sorted(p.name for p in (ROOT / "tests").glob("test_mcp*.py"))
    suites += sorted(p.name for p in (ROOT / "tests").glob("test_swap*.py"))
    assert len(suites) > 20, f"suspiciously few MCP/swap suites: {suites}"
    assert {"test_mcp.py", "test_swap.py"} <= set(suites), (
        "the bare suites are gone or renamed; re-derive this pin")
    missed = [n for n in suites if not _swap_tests([f"tests/{n}"])]
    assert not missed, (
        f"{missed} are MCP or swap test suites but select nothing; widen "
        f"SWAP_TEST_RE in ci.yml's `swap` step")


def test_an_unrelated_test_or_docs_diff_still_selects_nothing():
    """The cost bound this rule must not break: a test-only or docs diff that
    is not an MCP/swap suite does not spin up the cordis job at all."""
    for changed in ("tests/test_foo.py", "tests/test_helpers.py",
                    "docs/ci.md", ".github/workflows/pages.yml",
                    "src/revl/gate.py"):
        assert not _swap_tests([changed]), changed
        assert not _swap_source([changed]), changed


def test_the_source_side_answer_keeps_the_eleven_suite_shape():
    """`swap` still means "run the eleven": the modules those suites observe,
    the suites on the explicit list, the provisioning script and this
    workflow. A changed MCP/swap suite the explicit list does not name is
    deliberately not a source-side answer, which is what keeps a test-only
    diff of such a suite off the eleven; the seven MCP/swap suites the list
    DOES name stay source-side and still run the eleven."""
    for changed in ("src/revl/mcp/session.py", "src/revl/runtime.py",
                    "tests/test_self_extending_334.py",
                    "backends/python/setup.sh",
                    ".github/workflows/ci.yml"):
        assert _swap_source([changed]), changed
    for changed in ("tests/test_mcp_foo.py", "tests/test_mcp_proxy.py",
                    "tests/test_swap_foo.py"):
        assert not _swap_source([changed]), changed


def test_the_changes_job_publishes_the_swap_test_selection():
    jobs = _jobs()
    assert jobs[CHANGES]["outputs"]["swap_tests"] == (
        "${{ steps.swap.outputs.swap_tests }}")
    run = _step(CHANGES, "swap")["run"]
    assert "SWAP_TEST_RE" in run, (
        "the swap step no longer applies its test-suite glob")
    assert 'echo "swap_tests=${swap_tests}" >> "$GITHUB_OUTPUT"' in run, (
        "the swap step no longer publishes swap_tests, so the job cannot run "
        "the changed suite that selected it")


def test_a_deleted_suite_is_not_put_in_swap_tests():
    """`git diff --name-only` lists a DELETED path. pytest exits 4 ("no tests
    ran") on a missing file, and the #2642 receipt would turn that into a red
    advisory job. So `swap_tests` is built from `--diff-filter=ACMR`. The
    explicit list is unchanged: `swap` still reads the unfiltered diff, so
    deleting a path on it selects the eleven, as it always did (and the
    missing path then reds pytest, also unchanged)."""
    run = _step(CHANGES, "swap")["run"]
    assert "--diff-filter=ACMR" in run, (
        "a deleted tests/test_mcp_*.py would enter swap_tests, reach pytest, "
        "which exits 4 and reds the job")
    assert "changed_suites" in run, (
        "the ACMR diff no longer feeds the suite glob")
    assert run.count("git diff --name-only") == 2, (
        "expected the unfiltered diff for `swap` and the ACMR diff for the "
        "suites")


def _job_selection(tmp_path, swap: str, swap_tests: str):
    """Run the job's REAL `run:` body with pytest and the receipt stubbed, and
    return (pytest argv, receipt argv).

    The cost bound this rule adds lives in that shell, so substring assertions
    would not see a regression that always starts from `$SUITES`. The step
    body is executed by the same bash GitHub uses on ubuntu-latest.
    """
    jobs = _jobs()
    step = next(s for s in jobs[SWAP_JOB]["steps"]
                if s.get("name") == SWAP_STEP)
    pytest_bin = tmp_path / "backends/python/.venv/bin/python"
    pytest_bin.parent.mkdir(parents=True)
    pytest_bin.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$STUB_PYTEST_ARGS\"\n"
        "printf '1 passed in 0.01s\\n'\nexit 0\n", encoding="utf-8")
    pytest_bin.chmod(0o755)
    shim = tmp_path / "shim"
    shim.mkdir()
    python3 = shim / "python3"
    python3.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$STUB_RECEIPT_ARGS\"\n",
        encoding="utf-8")
    python3.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "SUITES": step["env"]["SUITES"],
        "SWAP": swap,
        "SWAP_TESTS": swap_tests,
        "STUB_PYTEST_ARGS": str(tmp_path / "pytest-args"),
        "STUB_RECEIPT_ARGS": str(tmp_path / "receipt-args"),
        "PATH": str(shim) + os.pathsep + env.get("PATH", ""),
    })
    subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env,
                   check=True, capture_output=True, text=True)
    pytest_argv = (tmp_path / "pytest-args").read_text(encoding="utf-8").split()
    receipt_argv = (tmp_path / "receipt-args").read_text(encoding="utf-8").split()
    return pytest_argv, receipt_argv


def _targets(argv) -> list:
    return [a for a in argv if a.endswith(".py")]


def test_a_test_only_change_runs_the_changed_suite_not_the_eleven(tmp_path):
    """The central cost bound, executed rather than pattern-matched: an MCP
    suite the explicit list does not name runs ALONE under the pin, and the
    receipt reads back that same one-file selection."""
    pytest_argv, receipt_argv = _job_selection(
        tmp_path / "only", "false", "tests/test_mcp_foo.py")
    assert _targets(pytest_argv) == ["tests/test_mcp_foo.py"], pytest_argv
    assert receipt_argv[receipt_argv.index("--selection") + 1] == (
        "tests/test_mcp_foo.py")


def test_the_source_side_runs_the_eleven_and_dedupes(tmp_path):
    suites = _jobs()[SWAP_JOB]["steps"]
    expected = next(s for s in suites if s.get("name") == SWAP_STEP)["env"][
        "SUITES"].split()
    # the source side alone, unchanged: the eleven
    pytest_argv, _ = _job_selection(tmp_path / "source", "true", "")
    assert _targets(pytest_argv) == expected, pytest_argv
    # plus a changed suite already on the explicit list: still eleven
    pytest_argv, _ = _job_selection(
        tmp_path / "dupe", "true", "tests/test_swap_author_flag_2518.py")
    assert _targets(pytest_argv) == expected, pytest_argv
    # plus a new suite: twelve, the new one last
    pytest_argv, _ = _job_selection(
        tmp_path / "extra", "true", "tests/test_mcp_foo.py")
    assert _targets(pytest_argv) == expected + ["tests/test_mcp_foo.py"]


def test_neither_answer_fails_open_to_the_eleven(tmp_path):
    """A selector crash publishes neither answer. The job runs the eleven
    rather than a bare `pytest` (which would run the whole suite), and a
    'false' answer with nothing selected cannot reach a bare pytest either."""
    expected = next(s for s in _jobs()[SWAP_JOB]["steps"]
                    if s.get("name") == SWAP_STEP)["env"]["SUITES"].split()
    for swap, swap_tests in (("", ""), ("false", "")):
        pytest_argv, _ = _job_selection(tmp_path / swap or "empty",
                                        swap, swap_tests)
        assert _targets(pytest_argv) == expected, (swap, swap_tests)


def test_the_swap_job_runs_the_changed_suite_that_selected_it():
    """The selection is not only "the job wakes up": the changed suite is
    executed under the pinned runtime, deduped against the eleven, and the
    #2642 receipt reads back exactly the pytest selection."""
    jobs = _jobs()
    step = next(s for s in jobs[SWAP_JOB]["steps"]
                if s.get("name")
                == "swap and authoring suites under the pinned runtime")
    assert step["env"]["SUITES"].count(".py") == 11, step["env"]["SUITES"]
    assert step["env"]["SWAP_TESTS"] == (
        "${{ needs.changes.outputs.swap_tests }}")
    run = step["run"]
    assert "${SWAP_TESTS:-}" in run and "case \" $selection \"" in run, (
        "the job no longer adds/dedupes the changed suites")
    assert "pytest $selection -q" in run, "pytest no longer runs the selection"
    assert '--selection "$selection"' in run, (
        "the receipt no longer reads back the selection pytest ran")


def test_the_swap_job_selects_on_either_answer_and_fails_open():
    """Both answers select the job, an empty answer (selector crash) fails
    open to the eleven, and the exact 'false' answer with no changed suite
    still keeps it off."""
    jobs = _jobs()
    for swap, swap_tests in (("true", ""),
                             ("false", "tests/test_mcp_foo.py"),
                             ("", "")):
        assert SWAP_JOB in _runs(jobs, "pull_request", "true", "true",
                                 swap=swap, swap_tests=swap_tests), (
            swap, swap_tests)
    assert SWAP_JOB not in _runs(jobs, "pull_request", "true", "true",
                                 swap="false", swap_tests=""), (
        "a docs-only pull request wakes the cordis swap job")


# --------------------------------------------------------------------------
# The `changes` step itself, executed (security review fix).
#
# The pins above re-implement both regexes in Python and hand the JOB body an
# answer pair, so none of them could see that the step's own `swap` condition
# was `[ -n "$swap_tests" ] || grep "$SWAP_RE"`: that made a test-only diff of
# an unnamed suite publish swap=true, and the job then ran the eleven plus the
# file (twelve suites for a one-file test diff). These tests run the step's
# REAL `run:` shell with `git diff --name-only` stubbed and assert the exact
# GITHUB_OUTPUT pairs, then feed the published pair to the job body.
# --------------------------------------------------------------------------

def _git_stub(shim: Path) -> None:
    """A `git` that answers the two calls the step makes: the fetch (no-op)
    and the two `diff --name-only` shapes (all changed paths vs the ACMR
    list). The step is the only thing under test; git itself is not."""
    git = shim / "git"
    git.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  fetch) exit 0 ;;\n"
        "  diff)\n"
        "    case \"$*\" in\n"
        "      *--diff-filter=ACMR*) cat \"$STUB_SUITES\" ;;\n"
        "      *) cat \"$STUB_CHANGED\" ;;\n"
        "    esac ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8")
    git.chmod(0o755)


def _changes_step_outputs(tmp_path, changed, event="pull_request"):
    """Run the `changes` step's real shell with the diff stubbed and return the
    GITHUB_OUTPUT pairs it published, exactly as GitHub would read them."""
    step = _step(CHANGES, "swap")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "changed").write_text("".join(f"{p}\n" for p in changed),
                                      encoding="utf-8")
    # every stubbed path exists at HEAD, so the ACMR list is the same
    (tmp_path / "suites").write_text("".join(f"{p}\n" for p in changed),
                                     encoding="utf-8")
    shim = tmp_path / "shim"
    shim.mkdir()
    _git_stub(shim)
    out = tmp_path / "github-output"
    out.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(step.get("env") or {})
    env.update({
        "EVENT_NAME": event,
        "BASE_REF": "main",
        "GITHUB_OUTPUT": str(out),
        "STUB_CHANGED": str(tmp_path / "changed"),
        "STUB_SUITES": str(tmp_path / "suites"),
        "PATH": str(shim) + os.pathsep + env.get("PATH", ""),
    })
    subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env,
                   check=True, capture_output=True, text=True)
    pairs = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        pairs[key] = value
    return pairs


def test_the_changes_step_publishes_swap_false_for_a_test_only_unnamed_diff(
        tmp_path):
    """The defect security found: a test-only diff to a suite the explicit
    list does not name publishes swap=false plus swap_tests=<file>. That pair
    selects the job through swap_tests alone, and the body runs ONE suite. The
    first push published swap=true here (the `[ -n "$swap_tests" ]` OR), which
    is why the job ran the eleven plus the file."""
    published = _changes_step_outputs(tmp_path, ["tests/test_mcp_foo.py"])
    assert published == {"swap_tests": "tests/test_mcp_foo.py",
                         "swap": "false"}
    assert SWAP_JOB in _runs(_jobs(), "pull_request", "true", "true",
                             swap=published["swap"],
                             swap_tests=published["swap_tests"]), (
        "the job's `if:` no longer wakes on the pair the step published")
    pytest_argv, receipt_argv = _job_selection(
        tmp_path / "body", published["swap"], published["swap_tests"])
    assert _targets(pytest_argv) == ["tests/test_mcp_foo.py"], pytest_argv
    assert receipt_argv[receipt_argv.index("--selection") + 1] == (
        "tests/test_mcp_foo.py")


def test_the_changes_step_publishes_swap_true_and_no_tests_for_a_source_diff(
        tmp_path):
    """The source side is unchanged: swap=true and an EMPTY swap_tests, which
    is the answer that runs the eleven. A changed suite on the explicit list
    still publishes swap=true; its path also lands in swap_tests, and the job
    body dedupes it against the eleven."""
    published = _changes_step_outputs(tmp_path / "source",
                                      ["src/revl/mcp/session.py"])
    assert published == {"swap_tests": "", "swap": "true"}
    listed = _changes_step_outputs(
        tmp_path / "listed", ["tests/test_swap_author_flag_2518.py"])
    assert listed == {"swap_tests": "tests/test_swap_author_flag_2518.py",
                      "swap": "true"}
    pytest_argv, _ = _job_selection(tmp_path / "body", listed["swap"],
                                    listed["swap_tests"])
    expected = next(s for s in _jobs()[SWAP_JOB]["steps"]
                    if s.get("name") == SWAP_STEP)["env"]["SUITES"].split()
    assert _targets(pytest_argv) == expected, pytest_argv
