"""#854: the root suite is skipped only for a docs-only diff, never by default.

`.github/workflows/ci.yml` owns the root `pytest tests/` suite in two places,
`frontend` and `frontend-cordis`, and both were routed through the `changes`
job's fast-path filter. That filter was a DENY-list,
`^(src/revl/|selfhost/|stdlib/)`, an enumeration of what counts as a language
change, so any path its author had not thought of silently disabled the entire
root suite and both jobs reported `skipping`, which branch protection cannot
tell apart from `success`.

That is not hypothetical, and the file below is the case that proved it: PR #850
(`e6067cd1`) changed only `backends/python/emit.py`, `backends/python/replay.py`,
`backends/python/runtime.py` and `tests/test_crash_recovery.py`. None matched,
the suite was skipped, the PR merged green, and `backends/python/emit.py` is the
REFERENCE emitter that `tests/test_selfhost_emit_py.py` holds byte-identical to
its revl twin `selfhost/emit_py.rvl`. The twin was never ported, nothing
collected the test that says so, and `main` has been red on
`test_selfhosted_emitter_is_byte_identical[../policy_agents.rvl]` ever since.
PR #845 is the same shape without the aftermath: a guard test added to
`tests/test_multi_realm_require.py`, the item-429 ledger at
`tests/fixtures/selfhost_uncovered_lines.json` edited, and
`tools/selfhost_line_coverage.py` changed, with nothing collecting any of it.

So the filter is inverted. What is left of the fast path is an ALLOW-list
(`SKIP_RE`) of paths that cannot affect the suite -- documentation, and nothing
else -- and every other path, including every path no one has enumerated yet,
runs the matrix. The assertions below read ci.yml's own routing:

  * every suite-relevant diff, and the real #850 change set first among them,
    selects a job that runs the root suite;
  * the allow-list admits documentation and rejects everything else, so a
    documentation-only diff still skips the 3-version matrix;
  * an ungated job (`root-suite-affected`) runs the selection
    `tools/affected_tests.py` already computes and cannot report `skipping`, so
    coverage does not depend on the allow-list being complete.

Hermetic: one real YAML parse of the workflow plus control assertions that keep
the scan from passing vacuously.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

import pytest
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

# The module this pin re-runs under the console script, in a fresh process, to
# observe the invocation difference directly. Chosen for being one of the three
# modules that genuinely failed this way AND cheap (~0.2s): the other two are
# tests/test_affected_tests.py (~21s) and tests/test_inverse_capture_by_value.py.
# tests/test_reserved_lexicon_sweep.py also imports a repo-root directory but
# bootstraps the root itself, so it cannot fail this way and it costs ~36s.
_ROOT_IMPORT_PROBE = "tests/test_274_navigable_slice2.py"

# The only jobs allowed to carry the fast-path routing condition. A new
# `needs.changes.outputs.frontend` gate outside this pair would be a new
# conditional root-suite runner, which is the shape #854 exists to remove.
MATRIX_JOBS = ("frontend", "frontend-cordis")

# The real change set of PR #850 (`e6067cd1`), the merge that put `main` red
# because the root suite was skipped. `backends/python/emit.py` is the reference
# twin of `selfhost/emit_py.rvl`; the diff is the fixture that matters, and the
# fast path must not classify it as skippable.
PR_850_DIFF = (
    "backends/python/emit.py",
    "backends/python/replay.py",
    "backends/python/runtime.py",
    "tests/test_crash_recovery.py",
)

# Diffs that must select a job which runs the root suite: PR #845's own change
# set (entries 1 to 3), that set as a whole (entry 4), the un-mapped `tests/`
# shape the issue names (entry 5), one path per other directory the suite owns
# but the old deny-list did not enumerate (entries 6 to 10), and PR #850 (entry
# 11). The paths the old filter could see (`src/revl/`, `selfhost/`, `stdlib/`)
# are deliberately last: they were never the hole, so if the model only passes
# on those, it has proved nothing.
SUITE_RELEVANT_DIFFS = (
    ("tests/test_multi_realm_require.py",),
    ("tests/fixtures/selfhost_uncovered_lines.json",),
    ("tools/selfhost_line_coverage.py",),
    (
        "tests/test_multi_realm_require.py",
        "tools/selfhost_line_coverage.py",
        "tests/fixtures/selfhost_uncovered_lines.json",
    ),
    ("tests/foo.py",),
    ("backends/wasm/emit.py",),
    ("backends/python/runtime.py",),
    ("crates/revl-gate/src/lib.rs",),
    ("tests/conftest.py",),
    ("pyproject.toml",),
    PR_850_DIFF,
    ("src/revl/typecheck.py",),
)

# Diff classes the fast path may legitimately skip: documentation only. Both are
# one path or a few, so the allow-list cannot pass this by matching everything.
SKIPPABLE_DIFFS = (
    ("docs/arithmetic.md",),
    ("README.md",),
    ("docs/arithmetic.md", "docs/status.md", "README.md"),
)

# The self-host oracles that guard a tier's REFERENCE emitter, keyed by the tier
# directory. `backends/<tier>/emit.py` is the file each `selfhost/emit_*.rvl`
# port is held byte-identical to; the two entries in the second tuple load the
# reference emitter of EVERY tier (`tools/selfhost_differential_survey.py` builds
# the paths at runtime) or measure every self-host line, so they are guarded by
# any tier's emitter. A `backends/<tier>/**` diff must select these, or degrade
# to the whole `tests/` tree. Before the fix for #850 it selected none of them.
_REFERENCE_ORACLE = {
    "python": "tests/test_selfhost_emit_py.py",
    "typescript": "tests/test_selfhost_emit_ts.py",
    "go": "tests/test_selfhost_emit_go.py",
    "java": "tests/test_selfhost_emit_java.py",
    "rust": "tests/test_selfhost_emit_rust.py",
    "wasm": "tests/test_selfhost_emit_wasm.py",
}
_REFERENCE_ORACLE_ALWAYS = (
    "tests/test_selfhost_line_coverage.py",
    "tests/test_selfhost_differential_survey.py",
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


def _root_directory_imports():
    """`tests/**/*.py` -> the repo-root directories it imports.

    A repo-root directory is a plain directory at the repository root with no
    `__init__.py` (`tools/`, `backends/`, `tests/`), so importing one needs the
    repository root ITSELF on sys.path -- `<root>/src` does not cover it.
    """
    root_dirs = {
        p.name for p in ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }
    found = {}
    for path in sorted((ROOT / "tests").rglob("*.py")):
        if "fixtures" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - collected elsewhere
            continue
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module)
            elif isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else "")
                if called not in ("__import__", "import_module") or not node.args:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    for piece in arg.values:
                        if (isinstance(piece, ast.Constant)
                                and isinstance(piece.value, str) and piece.value):
                            names.add(piece.value)
                            break
        hits = sorted(n for n in names if n.split(".")[0] in root_dirs)
        if hits:
            found[path.relative_to(ROOT).as_posix()] = hits
    return found


def _needs(spec):
    raw = spec.get("needs")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(raw)


def _allow_list(jobs):
    """(pattern, negated) of the `changes` job's allow-list, or (None, False).

    Two readings have to agree for the model to be trustworthy: the pattern has
    to be declared where the job can see it (a `SKIP_RE` env var), and the diff
    has to be filtered with it NEGATED (`grep -Ev`, i.e. "paths that are not on
    the list"). If the negation disappears, the sense of the filter has been
    inverted back into a deny-list, which is the bug.
    """
    pattern = None
    negated = False
    for step in _steps(jobs, "changes"):
        env = step.get("env") or {}
        if isinstance(env, dict) and env.get("SKIP_RE"):
            pattern = str(env["SKIP_RE"])
            negated = bool(
                re.search(
                    r"grep\s+-E\S*v\S*\s+\"\$SKIP_RE\"", step.get("run") or ""
                )
            )
    return pattern, negated


# The pre-#854 filter shape: a single quoted pattern handed to `grep -E`, whose
# matches run the suite. Read only so that a reverted workflow is modelled as it
# behaves; nothing asserts that this shape is present.
_LEGACY_DENY_LIST = re.compile(r"grep\s+-E\S*\s+'([^']*)'")


def _skip_re(jobs):
    """The compiled allow-list, asserting the fix's shape is intact."""
    pattern, negated = _allow_list(jobs)
    assert pattern is not None, (
        "the `changes` job no longer declares a `SKIP_RE` allow-list; this file "
        "reads that list to model job selection, so update it deliberately "
        "rather than letting the model go stale"
    )
    assert negated, (
        "the `changes` job declares SKIP_RE but does not apply it to the diff "
        "with `grep -Ev`; without the negation the list selects the paths that "
        "RUN the suite instead of the paths that SKIP it, which is the deny-list "
        "shape #854 removed"
    )
    return re.compile(pattern)


def _matrix_skippable(jobs, paths):
    """Whether the fast path classifies this diff as suite-skippable.

    Mirrors the shell: the allow-list is applied to every changed path, and the
    diff is skippable only when it is non-empty and nothing survives the filter.
    An empty diff therefore takes the matrix, which is the job's own fail-safe.

    A workflow still carrying the legacy DENY-list is modelled as it behaves --
    paths matching it RUN the suite, so a diff it does not match is skipped --
    which is what makes the behavioural assertions in this file fail with the
    ROUTING fact rather than with a complaint about the filter's shape when the
    fix is reverted.
    """
    if not paths:
        return False
    pattern, _ = _allow_list(jobs)
    if pattern is not None:
        return all(re.match(pattern, p) for p in paths)
    for step in _steps(jobs, "changes"):
        found = _LEGACY_DENY_LIST.search(step.get("run") or "")
        if found:
            return not any(re.match(found.group(1), p) for p in paths)
    raise AssertionError(
        "the `changes` job has no diff filter at all, so it cannot be modelled; "
        "if the fast path was removed deliberately, delete this file with it"
    )


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
    skippable = _matrix_skippable(jobs, diff)
    skipped = {
        job
        for job, spec in jobs.items()
        if skippable and _routed_on_the_fast_path(spec)
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


def test_every_suite_relevant_diff_selects_a_root_suite_runner():
    """The property #854 is about, over the whole fixture table: a diff the root
    suite can observe selects a job that runs it. PR #845's shape, PR #850's
    shape, one path per other owned directory, and the language-source paths the
    old filter already caught."""
    jobs = _jobs()
    runners = _root_suite_jobs(jobs)
    assert runners, "no job in ci.yml runs the root suite at all"
    for diff in SUITE_RELEVANT_DIFFS:
        selected = _selected_jobs(diff, jobs)
        hits = sorted(selected & runners)
        assert not _matrix_skippable(jobs, diff), (
            "the fast path classifies this diff as suite-skippable:\n"
            + _report(diff, selected)
        )
        assert hits, (
            "no selected job runs the root suite:\n" + _report(diff, selected)
        )
        assert JOB in selected, (
            f"{JOB!r} is not selected for a diff of {list(diff)}:\n"
            + _report(diff, selected)
        )


def test_the_backends_only_diff_that_broke_main_selects_the_matrix():
    """The real incident, as a fixture, and the regression test for it.

    PR #850 (`e6067cd1`) changed only `backends/python/emit.py`,
    `backends/python/replay.py`, `backends/python/runtime.py` and
    `tests/test_crash_recovery.py`. The deny-list matched none of them, so
    `frontend` and `frontend-cordis` both reported `skipping`, the root suite
    never ran, and the PR merged green while `backends/python/emit.py` (the
    REFERENCE emitter that `tests/test_selfhost_emit_py.py` holds byte-identical
    to `selfhost/emit_py.rvl`) was left un-ported. `main` has been red on
    `test_selfhosted_emitter_is_byte_identical[../policy_agents.rvl]` since.

    So: this exact change set must not be classified as suite-skippable, and it
    must select both matrix jobs.
    """
    jobs = _jobs()
    selected = _selected_jobs(PR_850_DIFF, jobs)
    assert not _matrix_skippable(jobs, PR_850_DIFF), (
        "PR #850's change set is still classified as skippable by the fast "
        "path, so the only jobs that collect the selfhost emitter twin check "
        "would be skipped again:\n" + _report(PR_850_DIFF, selected)
    )
    missing = sorted(set(MATRIX_JOBS) - selected)
    assert not missing, (
        f"{missing} are not selected for PR #850's change set; before the fix "
        "the diff matched nothing in `^(src/revl/|selfhost/|stdlib/)` and both "
        "reported skipping:\n" + _report(PR_850_DIFF, selected)
    )
    assert sorted(selected & _root_suite_jobs(jobs)), (
        "PR #850's change set selects no root-suite runner:\n"
        + _report(PR_850_DIFF, selected)
    )


def test_the_fast_path_allow_list_admits_documentation_and_nothing_else():
    """The shape of the fix, asserted in both directions so neither can pass
    vacuously: every fixture path the fast path claims to skip must match the
    allow-list, and every fixture path the suite can observe must miss it.

    The second direction is the one that matters. Adding a path to this list is
    the claim "the root suite cannot observe this file", and #854 exists because
    the previous filter made the opposite claim by omission -- anything not
    enumerated was treated as unobservable, including, for real,
    `backends/python/emit.py`.
    """
    jobs = _jobs()
    keep = _skip_re(jobs)
    for diff in SKIPPABLE_DIFFS:
        for path in diff:
            assert keep.match(path), (
                f"{path!r} does not match the fast-path allow-list "
                f"{keep.pattern!r}, so a documentation-only diff touching it "
                "pays for the 3-version matrix"
            )
    for diff in SUITE_RELEVANT_DIFFS:
        for path in diff:
            assert not keep.match(path), (
                f"{path!r} matches the fast-path allow-list {keep.pattern!r}, "
                "so a diff touching only it would skip the root suite. The "
                "allow-list may name only paths whose effect on the suite has "
                "been proven absent, file by file."
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


def test_a_failed_selection_degrades_to_the_whole_tree():
    """The other half of the same fail-safe: a selector that EXITS non-zero must
    not be read as "nothing to do" either. The failure path has to substitute the
    FULL selection before the node list is read, so the only two outcomes of this
    job stay "ran the suite" and "failed", never "ran nothing"."""
    jobs = _jobs()
    script = _script(_steps(jobs, JOB))
    assert re.search(
        r"if ! out=\$\(.*?tools/affected_tests\.py.*?;\s*then\b.*?PYTEST tests/",
        script,
        re.S,
    ), (
        f"{JOB} does not replace a failed `tools/affected_tests.py` run with the "
        "FULL `tests/` selection; a selector that errors would otherwise leave "
        "an empty node list, which is the same hole as an empty diff"
    )
    assert "|| true" not in script, (
        f"{JOB} swallows a failing command with `|| true`, so a real selection "
        "failure would be read as an empty (passing) run"
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


def test_the_job_can_import_a_repo_root_directory_from_a_test_body():
    """`root-suite-affected` runs the `pytest` console script, and the console
    script -- unlike `python -m pytest` -- does NOT prepend the cwd to sys.path.
    A module under tests/ that imports a repo-root directory inside a test body
    therefore needs the repository root on sys.path for some other reason, and
    `tests/conftest.py` is that reason.

    Not hypothetical: `tests/test_affected_tests.py` (tools.affected_tests),
    `tests/test_274_navigable_slice2.py` (tests.test_evidence_policy) and
    `tests/test_inverse_capture_by_value.py` (backends.<tier>.emit) raised
    `ModuleNotFoundError` inside the test body under this job's invocation, so
    the job reported a red that had nothing to do with the change under test.
    It looked green locally for two independent reasons: `python -m pytest`
    prepends the cwd, and pytest imports every collected module before running
    any test, so a module that bootstraps the root itself
    (`tests/test_reserved_lexicon_sweep.py`,
    `tests/test_542_statement_nesting_bound.py`) covered for the others whenever
    the selector happened to pick one of those too.

    That second reason is why this has to run in a FRESH process over one file:
    in-process, the root is on sys.path whenever any collected module put it
    there, so an in-process assertion cannot see the defect at all.
    """
    imports = _root_directory_imports()
    assert imports, (
        "no module under tests/ imports a repo-root directory, so this scan "
        "proves nothing. If those imports are really gone, delete this pin "
        "deliberately rather than leaving a scan that can no longer fail."
    )
    assert _ROOT_IMPORT_PROBE in imports, (
        f"{_ROOT_IMPORT_PROBE} no longer imports a repo-root directory, so it no "
        "longer proves anything as the probe; choose another module from "
        f"{sorted(imports)} deliberately."
    )
    script = _script(_steps(_jobs(), JOB))
    assert re.search(r"(?m)^\s*pytest\s", script), (
        f"{JOB} no longer invokes the `pytest` console script, which is the only "
        "reason this pin exists: the console script and `python -m pytest` differ "
        "on whether the cwd reaches sys.path. If the invocation really is "
        "`python -m pytest` now, re-cost this pin deliberately."
    )
    entry = Path(sys.executable).parent / "pytest"
    if not entry.is_file():  # pragma: no cover - a checkout without the script
        pytest.skip(f"no `pytest` console script beside {sys.executable}")
    proc = subprocess.run(
        [str(entry), "-q", "-p", "no:cacheprovider", _ROOT_IMPORT_PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    assert proc.returncode == 0, (
        f"`{entry} -q {_ROOT_IMPORT_PROBE}` exits {proc.returncode} with the "
        f"interpreter's own directory first on sys.path and no cwd entry, which "
        f"is the invocation {JOB} uses. The repository root has to reach sys.path "
        "for the modules listed above to import anything, and tests/conftest.py "
        "owns that: it APPENDS the root rather than inserting it, so the "
        "resolution order of everything that already resolved is unchanged.\n"
        + proc.stdout[-2000:] + proc.stderr[-2000:]
    )


# --- the selector really is a fail-safe for these diffs -------------------- #
def test_the_selector_returns_a_non_empty_selection_for_every_fixture():
    """Why the ungated job is safe: it never turns any of these diffs into "run
    nothing". A modified test maps to itself (so it is collected), a `tools/`
    script with no same-named covering test and an un-mapped path (`tests/foo.py`,
    `pyproject.toml`, `crates/**`) fall back to FULL, a tier change maps to that
    tier's tests, and a documentation-only diff maps to the one root-suite test
    that reads documentation. The diff #845 actually shipped is FULL, and so is
    the empty changed-set the selector is reached with when a base ref will not
    resolve."""
    for diff in SUITE_RELEVANT_DIFFS:
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
    for diff in SKIPPABLE_DIFFS:
        result = at.select(list(diff), ROOT)
        assert result["pytest"] == ["tests/test_doc_examples.py"], (
            f"a documentation-only diff is not mapped to the doc-example sweep: "
            f"pytest={result['pytest']!r} reason={result['reason']!r}. If this "
            "changed, re-cost the fast path: documentation-only pull requests "
            "still skip the matrix, so this selection is the only thing that "
            "collects them"
        )
    pr_845 = SUITE_RELEVANT_DIFFS[3]
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


# --- the `backends/**` shape, at the selector ----------------------------- #
def test_a_backends_only_diff_selects_the_selfhost_reference_oracles():
    """Why the #850 diff is covered even before the routing fix, and why the
    selector had to be repaired as well.

    `backends/<tier>/emit.py` is the reference emitter its `selfhost/emit_*.rvl`
    port must stay byte-identical to. The oracles that assert that reach the file
    in different ways: `tests/test_selfhost_emit_py.py` names it in prose, and
    `tests/test_selfhost_line_coverage.py` names it too, but
    `tests/test_selfhost_differential_survey.py` builds
    `backends/<tier>/emit.py` at runtime and never spells out a tier, so the
    selector's text heuristic missed it. This asserts the selector's own answer
    for each tier's emitter, independently of the workflow: the oracle nodes must
    be in it, or the selection must have degraded to the whole tree."""
    missing_on_disk = [
        node
        for node in list(_REFERENCE_ORACLE.values()) + list(_REFERENCE_ORACLE_ALWAYS)
        if not (ROOT / node).is_file()
    ]
    assert not missing_on_disk, (
        f"the oracle nodes this test pins do not exist: {missing_on_disk}, so "
        "the loop below would be vacuous"
    )
    for tier, oracle in _REFERENCE_ORACLE.items():
        diff = (f"backends/{tier}/emit.py",)
        result = at.select(list(diff), ROOT)
        selected = result["pytest"]
        if selected == ["tests/"]:
            continue  # the fail-safe FULL answer covers every oracle by name
        for node in (oracle,) + _REFERENCE_ORACLE_ALWAYS:
            assert node in selected, (
                f"{node} guards {diff[0]} and tools/affected_tests.py does not "
                f"select it: reason={result['reason']!r}, "
                f"selected={len(selected)} nodes. That is the #850 hole inside "
                "the selector: the guard is real, the file it guards is real, and "
                "nothing connects them"
            )


def test_the_incident_file_selects_all_three_tests_main_is_red_on():
    """The named incident, not a generalisation of it. `e6067cd1` put `main` red
    on three tests; a change to the reference emitter it touched must select all
    three, so a `backends/python/emit.py`-only pull request cannot reach `main`
    with them uncollected again."""
    red_on_main = (
        "tests/test_selfhost_emit_py.py",
        "tests/test_selfhost_line_coverage.py",
        "tests/test_selfhost_differential_survey.py",
    )
    emitter = "backends/python/emit.py"
    assert emitter in PR_850_DIFF, (
        f"the #850 fixture no longer names {emitter}, so this test has stopped "
        "covering the incident it exists for"
    )
    diffs = [
        [path] for path in PR_850_DIFF if path.startswith("backends/")
    ]
    diffs.append([emitter])
    diffs.append(list(PR_850_DIFF))
    for diff in diffs:
        result = at.select(diff, ROOT)
        selected = result["pytest"]
        if selected == ["tests/"]:
            continue  # the fail-safe FULL answer covers every oracle by name
        for node in red_on_main:
            assert node in selected, (
                f"{diff} does not select {node}: reason={result['reason']!r}. "
                "This is the #850 change set; if this is the only thing standing "
                "between the diff and an uncollected regression, it has to hold"
            )


# --- the cost decision the fix must not break ------------------------------ #
def test_a_documentation_only_diff_does_not_pay_for_the_matrix():
    """#854's hard constraint: the fix buys coverage without pulling the
    3-version matrix into documentation-only pull requests. This is also the
    anti-vacuity control for the selection model: it shows the model can skip a
    job, so "everything ran" is a statement about the diff and not about a model
    that never skips anything."""
    jobs = _jobs()
    for diff in SKIPPABLE_DIFFS:
        assert _matrix_skippable(jobs, diff), (
            f"{list(diff)} is not classified skippable, so the fast path is dead "
            "and documentation-only pull requests pay for the matrix"
        )
        selected = _selected_jobs(diff, jobs)
        heavy = sorted(
            {"frontend", "frontend-cordis", "conformance", "formal"} & selected
        )
        assert not heavy, (
            f"a documentation-only diff now selects {heavy}, so the fix moved "
            "the 3-version matrix onto docs-only pull requests:\n"
            + _report(diff, selected)
        )
        assert JOB in selected, (
            "a documentation-only diff selects no root-suite job at all, which "
            "leaves the docs-example test uncollected:\n" + _report(diff, selected)
        )
        assert set(jobs) - selected, (
            "this model claims every job runs for every diff, so it is not a "
            "model of ci.yml's routing and nothing above proves anything:\n"
            + _report(diff, selected)
        )
    # ... but the allow-list is documentation only, so the matrix is the default
    # again for anything the suite can observe.
    for diff in SUITE_RELEVANT_DIFFS:
        assert not _matrix_skippable(jobs, diff), (
            f"{list(diff)} is classified suite-skippable, so the matrix is not "
            "the default\n" + _report(diff, _selected_jobs(diff, jobs))
        )


def test_the_matrix_jobs_are_still_routed_on_the_fast_path():
    """The companion to the test above: the matrix is still gated, and only the
    matrix jobs carry that gate. The 3-version matrix stays expensive, which is
    why the ungated job below exists rather than the gate being removed."""
    jobs = _jobs()
    routed = {j for j, spec in jobs.items() if _routed_on_the_fast_path(spec)}
    assert routed == set(MATRIX_JOBS), (
        f"the jobs gated on needs.changes.outputs.frontend are {sorted(routed)}, "
        f"not {sorted(MATRIX_JOBS)}. Removing that gate would put the 3-version "
        "matrix plus cordis-py on documentation-only pull requests; if that is "
        "now the intent, change this pin deliberately and re-cost it."
    )
    assert JOB not in routed, (
        f"{JOB} is routed on the fast-path filter, which is the bug #854 fixes"
    )
