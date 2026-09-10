"""#854: a diff that misses the fast path must still run the root suite.

`.github/workflows/ci.yml` owns the root `pytest tests/` suite in two places,
`frontend` and `frontend-cordis`, and both are routed through the `changes`
job's fast-path filter (`^(src/revl/|selfhost/|stdlib/)`). A pull request whose
diff touches no language source therefore selected no job that ran the root
suite, and both jobs reported `skipping`, which branch protection cannot tell
apart from `success`.

That is not hypothetical. PR #845 added a guard test to
`tests/test_multi_realm_require.py`, edited the item-429 ledger at
`tests/fixtures/selfhost_uncovered_lines.json` and changed
`tools/selfhost_line_coverage.py`: nothing collected the new test and nothing
evaluated the ledger it edited, which is the same mechanism that let a Java
ledger drift land on `main`.

The fix is an unconditional job (`root-suite-affected`) that runs the selection
`tools/affected_tests.py` already computes. The assertions below read ci.yml's
own routing and are written to hold whether or not someone later widens the
fast-path regex:

  * some job runs the root suite for a `tests/` + `tools/` + fixtures-only diff,
    and that job cannot report `skipping`;
  * the existing selector, and only it, decides what that job runs;
  * the 3-version matrix stays on the fast path, so a documentation-only diff
    does not pay for it.

Hermetic: one real YAML parse of the workflow plus control assertions that keep
the scan from passing vacuously.
"""
from __future__ import annotations

import importlib.util
import posixpath
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
_SPEC = importlib.util.spec_from_file_location(
    "revl_affected_tests_for_854", ROOT / "tools" / "affected_tests.py"
)
at = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(at)

# The unconditional owner of root-suite coverage. Named here so that a rename
# is a deliberate edit to this pin rather than a scan that quietly matches some
# other job.
JOB = "root-suite-affected"

# The only jobs allowed to carry the fast-path routing condition. A new
# `needs.changes.outputs.frontend` gate outside this pair would be a new
# conditional root-suite runner, which is the shape #854 exists to remove.
MATRIX_JOBS = ("frontend", "frontend-cordis")

# Diff classes that selected NO job running the root suite before #854. The
# first, second and third entries are PR #845's own change set, the worked
# example in the issue; the fourth is that change set as a whole; the fifth is
# the un-mapped `tests/` shape the issue names. All of them are paths the
# selector maps to FULL (an un-mapped file, or a tool with no same-named
# covering test), which is the fail-safe the new job leans on.
FAST_PATH_INVISIBLE_DIFFS = (
    ("tests/test_multi_realm_require.py",),
    ("tests/fixtures/selfhost_uncovered_lines.json",),
    ("tools/selfhost_line_coverage.py",),
    (
        "tests/test_multi_realm_require.py",
        "tools/selfhost_line_coverage.py",
        "tests/fixtures/selfhost_uncovered_lines.json",
    ),
    ("tests/foo.py",),
)

# Only used to keep the scan from blessing every job as a root-suite runner.
# Each of these runs its OWN tier's tests, never the root suite.
_NOT_ROOT_SUITE = ("backend-python", "backend-java", "backend-go", "backend-rust")


def _jobs():
    """Job id -> job spec, from a real parse of the workflow."""
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "ci.yml did not parse to a mapping"
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and len(jobs) >= 10, (
        "ci.yml parsed to "
        f"{len(jobs) if isinstance(jobs, dict) else jobs!r} jobs; the parse is "
        "broken and every assertion in this file would be vacuous"
    )
    for anchor in ("changes", "lint", "frontend", "frontend-cordis"):
        assert anchor in jobs, f"ci.yml has no {anchor!r} job; the parse is broken"
    return jobs


def _spec(jobs, job_id):
    spec = jobs.get(job_id)
    assert isinstance(spec, dict), f"ci.yml has no {job_id!r} job"
    return spec


def _steps(jobs, job_id):
    return [s for s in (_spec(jobs, job_id).get("steps") or []) if isinstance(s, dict)]


def _script(steps):
    """Shell bodies and names only, comment lines dropped.

    Dropping whole-line `#` comments keeps a prose mention of `pytest tests/`
    from counting as an invocation, which would let the root-suite detector
    match on documentation instead of on a command.
    """
    lines = []
    for step in steps:
        for key in ("run", "name"):
            body = step.get(key)
            if not isinstance(body, str):
                continue
            lines.extend(
                ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
            )
    return "\n".join(lines)


def _needs(spec):
    raw = spec.get("needs")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(raw)


def _fast_path_pattern(jobs):
    """The `changes` job's keep-pattern, read out of the workflow itself."""
    for step in _steps(jobs, "changes"):
        run = step.get("run") or ""
        found = re.search(r"grep\s+-E\S*\s+'([^']*)'", run)
        if found:
            return found.group(1)
    raise AssertionError(
        "the `changes` job no longer contains a `grep -E '<pattern>'` filter; "
        "this file reads that filter to model job selection, so update it "
        "deliberately rather than letting the model go stale"
    )


def _visible_to_fast_path(jobs, paths):
    """Whether the fast-path filter can see any of `paths`."""
    keep = re.compile(_fast_path_pattern(jobs))
    return any(keep.match(p) for p in paths)


def _python_versions(spec):
    versions = []
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("uses") or "").startswith("actions/setup-python@"):
            with_ = step.get("with") or {}
            if "python-version" in with_:
                versions.append(str(with_["python-version"]))
    return versions


_JUNCTIONS = re.compile(r"&&|;")

# A `pytest` target that resolves to the whole tree when read from the repo
# root: the bare invocation (no target), `.`, or `tests`.
_WHOLE_TREE = (".", "tests", "")


def _pytest_invocations(spec):
    """(cwd, target words) for every `pytest` invocation in a job.

    The cwd matters: `cd backends/python && ... pytest ../wasm/ tests/ -q` runs
    a tier's own tests, so the `tests/` token there must NOT be read as the root
    suite. Targets are resolved against the cwd the same way the shell would.
    """
    found = []
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for line in (step.get("run") or "").splitlines():
            if line.lstrip().startswith("#"):
                continue
            cwd = "."
            for segment in _JUNCTIONS.split(line):
                tokens = segment.split()
                if not tokens:
                    continue
                if tokens[0] == "cd" and len(tokens) > 1:
                    cwd = posixpath.normpath(posixpath.join(cwd, tokens[1]))
                    continue
                for i, token in enumerate(tokens):
                    if token == "pytest" or token.rstrip(";").endswith("/pytest"):
                        rest = [t.rstrip(";") for t in tokens[i + 1 :]]
                        found.append(
                            (cwd, [t for t in rest if not t.startswith("-")])
                        )
    return found


def _root_suite_source(jobs, job_id):
    """Why `job_id` counts as running the root suite, or None.

    Two shapes are accepted, and both have to be visible in the job's own text:

      * a `pytest` invocation whose target resolves to the whole tree from the
        repo root (`tests/`, `.`, or none at all);
      * a target variable that the same job fills from `tools/affected_tests.py`,
        which emits `tests/` for every un-mapped path and for anything it cannot
        decide (the fail-safe pinned separately below).
    """
    spec = _spec(jobs, job_id)
    if not _python_versions(spec):
        return None
    script = _script(_steps(jobs, job_id))
    selector_fed = "tools/affected_tests.py" in script and "tests/" in script
    for cwd, targets in _pytest_invocations(spec):
        resolved = {posixpath.normpath(posixpath.join(cwd, t)) for t in targets}
        if not targets and cwd == ".":
            # a bare `pytest` at the repo root runs the whole suite
            resolved = {"."}
        if resolved & set(_WHOLE_TREE):
            return f"pytest targets {targets or '(none)'} from {cwd!r}"
        for target in targets:
            if target.startswith("$") and selector_fed:
                return f"pytest target {target!r} fed by tools/affected_tests.py"
    return None


def _root_suite_jobs(jobs):
    return {j for j in jobs if _root_suite_source(jobs, j) is not None}


def _routed_on_the_fast_path(spec):
    """Whether a job's `if` sends it down the fast path.

    Both arms matter: `github.event_name != 'pull_request' ||` is what a push to
    `main` takes, so only a pull request can skip one of these jobs.
    """
    cond = str(spec.get("if") or "")
    return "pull_request" in cond and "needs.changes.outputs.frontend" in cond


def _selected_jobs(diff, jobs):
    """Job ids that would RUN for a pull request with this diff.

    A job is skipped when its `if` is false for the event or when a job it
    `needs` was itself skipped, which is the closure GitHub applies. This is the
    same reading of the topology that `tests/test_affected_tests.py` uses.
    """
    fast_path = _visible_to_fast_path(jobs, diff)
    skipped = {
        job
        for job, spec in jobs.items()
        if not fast_path and _routed_on_the_fast_path(spec)
    }
    changed = True
    while changed:
        changed = False
        for job, spec in jobs.items():
            if job in skipped:
                continue
            if set(_needs(spec)) & skipped:
                skipped.add(job)
                changed = True
    return set(jobs) - skipped


def _report(diff, selected):
    return (
        f"diff={list(diff)}\n"
        f"  selected: {sorted(selected)}\n"
        f"  skipped:  {sorted(set(_jobs()) - selected)}"
    )


# --- the property #854 is about -------------------------------------------- #
def test_the_root_suite_detector_discriminates():
    """Anti-vacuity for the detector itself, before it is used to assert
    anything: it has to see the jobs that run `pytest tests/`, and it must not
    count a tier's own suite that merely passes a `tests/`-shaped argument.
    `cd backends/python && ... pytest ../wasm/ tests/ -q` runs the TIER's tests,
    because both targets resolve against that cwd."""
    jobs = _jobs()
    runners = _root_suite_jobs(jobs)
    assert set(MATRIX_JOBS) <= runners, (
        f"the detector missed {sorted(set(MATRIX_JOBS) - runners)}, which run "
        f"`pytest tests/`; it detected {sorted(runners)}"
    )
    for job in _NOT_ROOT_SUITE:
        assert job not in runners, (
            f"{job} runs its own tier's tests, but the detector counted it as a "
            "root-suite runner; a detector this loose would let every assertion "
            "in this file pass vacuously"
        )
    assert _root_suite_source(jobs, JOB) is not None, (
        f"{JOB} is not recognised as running the root suite. It runs "
        "`pytest $SEL_PYTEST`, where SEL_PYTEST is the selector's own `PYTEST` "
        "line, and degrades an empty selection to `tests/`."
    )


def test_a_diff_that_misses_the_fast_path_still_runs_the_root_suite():
    """PR #845's shape: no language source, so the fast path stays cold, and the
    root suite still has to run somewhere."""
    jobs = _jobs()
    runners = _root_suite_jobs(jobs)
    assert runners, "no job in ci.yml runs the root suite at all"
    for diff in FAST_PATH_INVISIBLE_DIFFS:
        selected = _selected_jobs(diff, jobs)
        hits = sorted(selected & runners)
        assert hits, (
            "no selected job runs the root suite:\n" + _report(diff, selected)
        )
        # Anti-vacuity: a model that selects everything models nothing. At least
        # one job must be skipped for these diffs, and the detector must not have
        # counted a tier-local suite as the root suite.
        assert set(jobs) - selected, (
            "this model claims every job runs for every diff, which is not a "
            "model of ci.yml's routing, so the check above proves nothing:\n"
            + _report(diff, selected)
        )
        assert JOB in selected, (
            f"{JOB!r} is not selected for a diff of {list(diff)}:\n"
            + _report(diff, selected)
        )


def test_the_unconditional_job_cannot_report_skipping():
    """A conditional job's `skipping` is indistinguishable from `success` in the
    merge box, which is half of #854. The new job must have no `if` of its own,
    nothing skippable in its `needs` closure, and no step that can be skipped
    away from the suite."""
    jobs = _jobs()
    spec = _spec(jobs, JOB)
    assert "if" not in spec, (
        f"{JOB} carries `if: {spec.get('if')!r}`; a skipped job reads as a "
        "passing one to branch protection, so the coverage job must be "
        "unconditional"
    )
    skipped_prone = {j for j in jobs if _routed_on_the_fast_path(_spec(jobs, j))}
    assert not set(_needs(spec)) & skipped_prone, (
        f"{JOB} needs {list(_needs(spec))}, which overlaps the skippable "
        f"{sorted(set(_needs(spec)) & skipped_prone)}, so the coverage job can be "
        "skipped along with them"
    )
    step_ifs = [
        (s.get("name"), s.get("if")) for s in _steps(jobs, JOB) if s.get("if")
    ]
    assert not step_ifs, (
        f"{JOB} has step-level condition(s) {step_ifs!r}; a step that does not "
        "run leaves the same hole as a job that does not run"
    )


def test_the_job_runs_the_suite_even_when_the_selection_is_empty():
    """The step that runs pytest must not be conditional, and an empty node list
    must degrade to the whole tree rather than to "nothing to do"."""
    jobs = _jobs()
    script = _script(_steps(jobs, JOB))
    assert re.search(r'if \[ -z "\$nodes" \].*nodes="tests/"', script, re.S), (
        f"{JOB} does not turn an empty node list into the full `tests/` "
        "selection; an empty selection would otherwise mean a green job that ran "
        "no test at all, which is #854 one level down"
    )


def test_the_selector_decides_what_the_job_runs():
    """One notion of "affected", not two. The job must invoke the existing
    selector, and must not name a second one."""
    jobs = _jobs()
    script = _script(_steps(jobs, JOB))
    assert "tools/affected_tests.py" in script, (
        f"{JOB} does not invoke tools/affected_tests.py; #854 asks for the "
        "selection that tool already computes, not a second notion of "
        "'affected'"
    )
    assert "--format machine" in script, (
        f"{JOB} does not ask the selector for machine-readable output, so its "
        "node list cannot be read back out"
    )
    for other in ("pytest --collect-only", "testmon", "pytest-testmon"):
        assert other not in script, (
            f"{JOB} appears to compute its own selection via {other!r} instead "
            "of reusing tools/affected_tests.py"
        )


def test_the_job_installs_what_the_root_suite_needs():
    """`tests/` imports PyYAML (this file included) and executes emitted Go and
    Java through the cross-tier probes, so the job has to provision them, the
    same way `frontend` does."""
    jobs = _jobs()
    script = _script(_steps(jobs, JOB))
    assert '.[test]' in script or ".[test]" in script, (
        f"{JOB} does not install the `.[test]` extra, so the suite's YAML and "
        "coverage imports are missing"
    )
    for uses in ("actions/setup-go@", "actions/setup-java@"):
        found = any(
            str(s.get("uses") or "").startswith(uses) for s in _steps(jobs, JOB)
        )
        assert found, (
            f"{JOB} does not pin {uses}...; the cross-tier probes decide whether "
            "to run from whether a toolchain answers on PATH, so an absent pin "
            "silently drops them"
        )


# --- the selector really is a fail-safe for these diffs -------------------- #
def test_the_selector_returns_a_non_empty_selection_for_the_pr_845_shape():
    """Why the new job is safe: it never turns these diffs into "run nothing".
    A modified test maps to itself (so it is collected), a `tools/` script with
    no same-named covering test and an un-mapped path fall back to FULL, and the
    diff #845 actually shipped is FULL for both reasons."""
    for diff in FAST_PATH_INVISIBLE_DIFFS:
        result = at.select(list(diff), ROOT)
        assert result["pytest"], (
            f"tools/affected_tests.py selects nothing for {list(diff)}: "
            f"reason={result['reason']!r}"
        )
        if "tests/" not in result["pytest"]:
            # A narrow selection is only sound if it still names what the diff
            # touches: a modified test file is collected rather than dropped (a
            # modified tool maps to its same-named covering test instead).
            for path in diff:
                if not path.startswith("tests/"):
                    continue
                assert path in result["pytest"], (
                    f"the selector drops {path!r} from a narrow selection: "
                    f"pytest={result['pytest']!r}"
                )
    pr_845 = FAST_PATH_INVISIBLE_DIFFS[3]
    combined = at.select(list(pr_845), ROOT)
    assert combined["full"] is True and "tests/" in combined["pytest"], (
        f"PR #845's own change set is not a FULL selection: {combined!r}"
    )
    empty = at.select([], ROOT)
    assert empty["full"] is True, (
        "an empty changed set must stay FULL (fail safe), not an empty "
        "selection: a base ref the selector cannot resolve reaches it as no "
        "changed files at all"
    )


# --- the cost decision is unchanged ---------------------------------------- #
def test_a_documentation_only_diff_does_not_pay_for_the_matrix():
    """#854's constraint: the fix buys coverage without pulling the 3-version
    matrix into documentation-only pull requests, which are most of the cheap
    tail of the uncollected class."""
    jobs = _jobs()
    doc_only = ("docs/arithmetic.md",)
    assert not _visible_to_fast_path(jobs, doc_only), (
        "docs/ is visible to the fast-path filter, so this assertion no longer "
        "describes a documentation-only pull request"
    )
    selected = _selected_jobs(doc_only, jobs)
    heavy = sorted({"frontend", "frontend-cordis", "conformance", "formal"} & selected)
    assert not heavy, (
        f"a documentation-only diff now selects {heavy}, so the fix moved the "
        "3-version matrix onto docs-only pull requests:\n" + _report(doc_only, selected)
    )
    assert JOB in selected, (
        "a documentation-only diff selects no root-suite job at all, which "
        "leaves the docs-example test uncollected:\n" + _report(doc_only, selected)
    )


def test_the_matrix_jobs_are_still_routed_on_the_fast_path():
    """The companion to the test above: the matrix is still gated, and only the
    matrix jobs and their dependants carry that gate."""
    jobs = _jobs()
    routed = {j for j, spec in jobs.items() if _routed_on_the_fast_path(spec)}
    assert routed == set(MATRIX_JOBS), (
        f"the jobs gated on needs.changes.outputs.frontend are {sorted(routed)}, "
        f"not {sorted(MATRIX_JOBS)}. Widening that gate is the alternative #854 "
        "rejected: it pulls the 3-version matrix onto every pull request that "
        "touches the root suite's owned paths. If that is now the intent, change "
        "this pin deliberately and re-cost it."
    )
    assert JOB not in routed, (
        f"{JOB} is routed on the fast-path filter, which is the bug #854 fixes"
    )
