"""Issue #1465: every `test_*.py` file in the tree is collected by SOME CI step.

The issue started narrow -- `test_issue_319_dunder.py` and `test_issue_322.py`
sat at the repository root, where `pyproject.toml`'s missing `testpaths` and
every workflow's explicit-path `pytest` invocations meant they never ran
anywhere. A root-only guard catches that one shape. The real defect is
broader: a test file that no CI step's `pytest` invocation resolves to, root
or not. `tck/tests/test_tck.py` was exactly that -- outside `tests/`, never
named by any workflow step, discovered only by grepping the workflow files for
"tck" and finding nothing. It has now been added to the `backend-python` job
(the venv `tck/README.md` documents running it under).

This file computes the same answer generally: read every workflow file that
ever runs pytest, resolve each `run:` step's pytest invocations to concrete
paths (honoring `cd`, multiple targets, and the one directory in the tree that
sets its own `testpaths`), and diff that against every `test_*.py` file that
actually exists. A step whose target is a shell variable is not silently
skipped -- it has to be named in `_DYNAMIC_STEPS` with a reason, or the scan
fails loudly, so a new dynamic selector cannot quietly carve out an unaudited
blind spot the way the root-level gap did.

Hermetic: real YAML parses of the checked-in workflow files, real filesystem
walks. No CI run, no toolchain, no subprocess.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Every root workflow file that has EVER been observed to run pytest. A new
# workflow file with its own pytest step must be added here deliberately
# (test_every_pytest_running_workflow_is_scanned enforces that it cannot just
# sit outside the scan).
WORKFLOWS = ("ci.yml", "arm64-smoke.yml", "x64-smoke.yml")

# Directories that are not part of the checked-in test tree (vendored or
# build output); a test_*.py under one of these, if it ever appeared, is not
# a file anyone maintains here.
SKIP_PARTS = {"__pycache__", ".venv", ".cordis-py", "node_modules", "target",
              "golden", "stubs", ".git"}

# A bare `pytest` invocation (no explicit target) collects whatever that
# directory's own pytest config names as `testpaths`. Only one directory in
# the tree sets one: `backends/python/pytest.ini` -> `testpaths = tests`. The
# repository root sets none -- that absence is issue #1465's own finding, so
# a bare `pytest` from the root is never seen in these workflows and is
# deliberately not modelled here; if one appears, the scan below reports it
# as unresolved rather than guessing.
_BARE_TESTPATHS = {
    "backends/python": ("tests",),
}

# Steps whose pytest target is a shell variable, so it cannot be resolved to
# a fixed set of paths by reading the YAML. Each has to be named here with a
# reason, or test_dynamic_steps_are_all_named_explicitly fails: that is the
# "list it explicitly, not silently" rule for anything this scan cannot
# parse. Keyed by (workflow file, job id).
_DYNAMIC_STEPS = {
    ("ci.yml", "root-suite-affected"): (
        "runs `pytest $SEL_PYTEST -q`, where SEL_PYTEST is filled by "
        "tools/affected_tests.py from the pull request's diff. It degrades "
        "to `tests/` only when the selector fails or returns nothing, so on "
        "a narrow selection it does not cover every file in tests/ -- it "
        "cannot be credited with covering any specific file unconditionally, "
        "and tests/ is covered by the plain `pytest tests/ -q` steps anyway."
    ),
}

_JUNCTION_RE = re.compile(r"&&|;")


def _jobs(workflow_name):
    doc = yaml.safe_load((WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{workflow_name} did not parse to a mapping"
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{workflow_name} has no jobs"
    return jobs


def _pytest_statements(run_text):
    """(cwd, target_tokens) for every pytest invocation in one step's `run`.

    `cd` is tracked across the WHOLE step, not just within one shell line: a
    literal block scalar (`run: |`) can `cd` on one line and invoke pytest on
    a later one with no `&&` joining them, and a folded scalar (`run: >`)
    already arrives here as a single space-joined line from PyYAML's own
    parse, so splitting on real newlines first and `&&`/`;` second covers
    both without needing a real shell parser.
    """
    out = []
    cwd = "."
    for raw_line in run_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for stmt in _JUNCTION_RE.split(line):
            stmt = stmt.strip()
            if not stmt:
                continue
            tokens = stmt.split()
            if not tokens:
                continue
            if tokens[0] == "cd" and len(tokens) > 1:
                cwd = posixpath.normpath(posixpath.join(cwd, tokens[1]))
                continue
            for i, tok in enumerate(tokens):
                if tok == "pytest" or tok.rstrip(";").endswith("/pytest"):
                    rest = [t.rstrip(";") for t in tokens[i + 1:]]
                    targets = [t for t in rest if not t.startswith("-")]
                    out.append((cwd, targets))
    return out


def _scan():
    """-> (covered: set[str], dynamic: list[((workflow, job), [target, ...])])

    `covered` is every `test_*.py` path (relative to ROOT, posix) that some
    static pytest invocation resolves to. `dynamic` is every invocation this
    scan could not resolve: a shell-variable target, or a bare `pytest` from
    a cwd with no modelled testpaths.
    """
    covered = set()
    dynamic = []
    for wf_name in WORKFLOWS:
        for job_id, spec in _jobs(wf_name).items():
            if not isinstance(spec, dict):
                continue
            key = (wf_name, job_id)
            for step in spec.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if not isinstance(run, str) or "pytest" not in run:
                    continue
                for cwd, targets in _pytest_statements(run):
                    var_targets = [t for t in targets if t.startswith("$")]
                    if var_targets:
                        dynamic.append((key, list(targets)))
                        continue
                    if not targets:
                        bare = _BARE_TESTPATHS.get(cwd)
                        if bare is None:
                            dynamic.append((key, [f"(bare pytest, no testpaths modelled for cwd {cwd!r})"]))
                            continue
                        targets = list(bare)
                    for t in targets:
                        resolved = posixpath.normpath(posixpath.join(cwd, t.rstrip("/")))
                        abspath = ROOT / resolved
                        if abspath.is_dir():
                            for p in abspath.rglob("test_*.py"):
                                if any(part in SKIP_PARTS for part in p.relative_to(ROOT).parts):
                                    continue
                                covered.add(p.relative_to(ROOT).as_posix())
                        elif abspath.is_file() and abspath.name.startswith("test_") and abspath.suffix == ".py":
                            covered.add(Path(resolved).as_posix())
                        # a target that resolves to nothing on disk is not
                        # coverage of anything; not an error here, since a
                        # workflow may reasonably pass a glob-expanded or
                        # conditionally-present path
    return covered, dynamic


def _all_test_files():
    found = []
    for p in ROOT.rglob("test_*.py"):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        found.append(rel.as_posix())
    return sorted(found)


def test_every_pytest_running_workflow_is_scanned():
    """Anti-staleness for the WORKFLOWS list itself: a new workflow file that
    runs pytest must be added deliberately, not discovered by this scan
    quietly missing it."""
    on_disk = sorted(p.name for p in WORKFLOWS_DIR.glob("*.yml"))
    assert on_disk, "no workflow files found; the scan below would be vacuous"
    unscanned = [name for name in on_disk if name not in WORKFLOWS]
    leaking = [
        name for name in unscanned
        if "pytest" in (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
    ]
    assert not leaking, (
        f"{leaking} run pytest but are not in WORKFLOWS, so this file's scan "
        "does not see their steps at all; add them to WORKFLOWS deliberately"
    )


def test_dynamic_steps_are_all_named_explicitly():
    """Every unresolved (shell-variable or unmodelled-bare) pytest invocation
    this scan hits must be listed in _DYNAMIC_STEPS with a reason -- that is
    the "too dynamic to parse: list it explicitly, not silently" rule. Checked
    both ways: a NEW unresolved step must be added here, and a STALE entry
    (one the scan no longer hits) must be removed, so the registry stays an
    exact account of what this file cannot resolve rather than growing a
    pin nobody re-derives."""
    _, dynamic = _scan()
    seen = {key for key, _targets in dynamic}
    unnamed = seen - set(_DYNAMIC_STEPS)
    assert not unnamed, (
        f"{sorted(unnamed)} has an unresolved pytest invocation (a shell-"
        "variable target or an unmodelled bare `pytest`) not listed in "
        "_DYNAMIC_STEPS; add it with a reason instead of letting the scan "
        "silently drop it from coverage"
    )
    stale = set(_DYNAMIC_STEPS) - seen
    assert not stale, (
        f"{sorted(stale)} is pinned in _DYNAMIC_STEPS but this scan no longer "
        "finds an unresolved invocation there; remove the stale entry (or, if "
        "it can now be resolved statically, the coverage below should include "
        "it and this pin is no longer needed)"
    )


def test_every_test_file_is_collected_by_some_ci_step():
    """The property #1465 is about. Every checked-in `test_*.py` must be
    reachable from some workflow's pytest invocation, or it has never run in
    CI regardless of whether it passes locally."""
    covered, _dynamic = _scan()
    all_files = _all_test_files()
    assert len(all_files) > 500, (
        f"only {len(all_files)} test_*.py files found under {ROOT}; the walk "
        "looks broken, and the assertion below would be vacuous"
    )
    assert len(covered) > 500, (
        f"the scan resolved only {len(covered)} covered files; that looks "
        "broken rather than a real answer, and the assertion below would be "
        "vacuous"
    )
    uncollected = [f for f in all_files if f not in covered]
    assert not uncollected, (
        f"{uncollected} are collected by NO CI step in {WORKFLOWS}: pytest "
        "never sees them, in CI, on any branch. Either add the file to the "
        "right pytest invocation (match a neighbouring test's venv and "
        "deps -- see tck/tests/test_tck.py's addition to the backend-python "
        "job for the shape), or delete it if it is dead and say why."
    )


def test_the_tck_incident_is_fixed():
    """The named incident, not just the generalisation of it.
    `tck/tests/test_tck.py` was the file that proved the root-only guard was
    too narrow: it sits outside both the repository root and `tests/`, so
    nothing collected it. Pinned here so a future edit to the backend-python
    step cannot silently drop it again without the broader scan above also
    having to fail first."""
    covered, _dynamic = _scan()
    assert "tck/tests/test_tck.py" in covered, (
        "tck/tests/test_tck.py is uncollected again; see backend-python's "
        "`backends/python/.venv/bin/python -m pytest tck/tests/test_tck.py "
        "-q` step in .github/workflows/ci.yml"
    )
