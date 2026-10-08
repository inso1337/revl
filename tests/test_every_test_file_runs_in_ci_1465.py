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

"Runs" is an invocation, not the bare token (issue #2160): a step that only
`pip install`s the pytest package names a package, so it is neither a pytest
runner that must be listed in `WORKFLOWS` nor an unresolved invocation that
must be carved out in `_DYNAMIC_STEPS`. `pages.yml` installs pytest -- the
reference classifier it verifies against lives in `tests/test_selfhost_lower.py`
and imports it -- and runs none.

Hermetic: real YAML parses of the checked-in workflow files, real filesystem
walks. No CI run, no toolchain, no subprocess.
"""
from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Every root workflow file that has EVER been observed to RUN pytest. A new
# workflow file with its own pytest invocation must be added here deliberately
# (test_every_pytest_running_workflow_is_scanned enforces that it cannot just
# sit outside the scan). A file that only INSTALLS the pytest package is not a
# runner and is deliberately absent: `pip install ... pytest` names a package,
# not an invocation (issue #2160 -- `pages.yml` used to be listed here for
# exactly that token and no longer is).
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

# Steps that RUN pytest but whose target is a shell variable, so it cannot be
# resolved to a fixed set of paths by reading the YAML. Each has to be named
# here with a reason, or test_dynamic_steps_are_all_named_explicitly fails:
# that is the "list it explicitly, not silently" rule for anything this scan
# cannot parse. Keyed by (workflow file, job id).
#
# A `pip install` of the pytest package is NOT one of these: it is not an
# invocation, it selects no target, and it belongs in neither this registry
# nor WORKFLOWS (issue #2160). `_pytest_statements` drops those statements
# before they can be read as one.
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
_PIP_RE = re.compile(r"pip[0-9.]*")


def _jobs(workflow_name):
    doc = yaml.safe_load((WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{workflow_name} did not parse to a mapping"
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{workflow_name} has no jobs"
    return jobs


def _statements(run_text):
    """(cwd, tokens) for every shell statement in one step's `run`.

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
            out.append((cwd, tokens))
    return out


def _pip_install_index(tokens):
    """Index of the `pip` in a `pip install ...` statement, else None.

    Matches `pip install`, `pip3 install`, `uv pip install` and
    `python3 -m pip install` (the token before `install` is a `pip[0-9.]*`
    basename). Anything else is not read as an install.
    """
    for i, tok in enumerate(tokens):
        if (i + 1 < len(tokens) and tokens[i + 1] == "install"
                and _PIP_RE.fullmatch(posixpath.basename(tok))):
            return i
    return None


def _pytest_statements(run_text):
    """(cwd, target_tokens) for every pytest INVOCATION in one step's `run`.

    A `pip install` statement is not an invocation: `pip install pytest` (and
    `python3 -m pip install --quiet -e . pytest`) names the pytest PACKAGE, so
    the statement yields nothing here -- no invocation and no target, hence no
    WORKFLOWS entry and no _DYNAMIC_STEPS carve-out (issue #2160). Only that
    statement is dropped, so `python3 -m pip install -e . pytest && python3 -m
    pytest tests/ -q` still reports the second one.
    """
    out = []
    for cwd, tokens in _statements(run_text):
        if _pip_install_index(tokens) is not None:
            continue
        for i, tok in enumerate(tokens):
            if tok == "pytest" or tok.rstrip(";").endswith("/pytest"):
                rest = [t.rstrip(";") for t in tokens[i + 1:]]
                targets = [t for t in rest if not t.startswith("-")]
                out.append((cwd, targets))
    return out


def _installs_pytest(run_text):
    """True when a step INSTALLS the pytest package, as opposed to running it.

    Kept separate from `_pytest_statements` so the diagnostics -- and the
    tests that pin them -- can name which of the two a step actually does,
    instead of calling a package name an "invocation" (issue #2160)."""
    for _cwd, tokens in _statements(run_text):
        i = _pip_install_index(tokens)
        if i is not None and any(t.rstrip(";") == "pytest" for t in tokens[i + 1:]):
            return True
    return False


def _pytest_steps(workflow_name):
    """((workflow, job), run_text) for every step whose `run` mentions pytest
    at all -- the filter the coverage scan, the WORKFLOWS anti-staleness check
    and the install/run distinction all share."""
    for job_id, spec in _jobs(workflow_name).items():
        if not isinstance(spec, dict):
            continue
        key = (workflow_name, job_id)
        for step in spec.get("steps") or []:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str) and "pytest" in run:
                yield key, run


def _runs_pytest(run_texts):
    """True when any of these `run:` texts contains a real pytest invocation
    -- static target, shell-variable target or bare. A step that only installs
    the package is not a runner (issue #2160)."""
    return any(_pytest_statements(run) for run in run_texts)


def _invocation_facts(run_text):
    """-> (static, dynamic) for one step's `run`.

    `static` is every (cwd, target) pair the scan can resolve on disk.
    `dynamic` is every (cwd, targets) invocation it cannot: a shell-variable
    target, or a bare `pytest` from a cwd with no modelled testpaths. A
    `pip install` of the package contributes to neither (issue #2160).
    """
    static = []
    dynamic = []
    for cwd, targets in _pytest_statements(run_text):
        if any(t.startswith("$") for t in targets):
            dynamic.append((cwd, list(targets)))
            continue
        if not targets:
            bare = _BARE_TESTPATHS.get(cwd)
            if bare is None:
                dynamic.append(
                    (cwd, [f"(bare pytest, no testpaths modelled for cwd {cwd!r})"]))
                continue
            targets = list(bare)
        static.append((cwd, list(targets)))
    return static, dynamic


def _scan():
    """-> (covered: set[str], dynamic: list[((workflow, job), [target, ...])])

    `covered` is every `test_*.py` path (relative to ROOT, posix) that some
    static pytest invocation resolves to. `dynamic` is every invocation this
    scan could not resolve: a shell-variable target, or a bare `pytest` from
    a cwd with no modelled testpaths. A `pip install` of the pytest package is
    neither -- it is not an invocation, so it enters no bookkeeping at all
    (issue #2160).
    """
    covered = set()
    dynamic = []
    for wf_name in WORKFLOWS:
        for key, run in _pytest_steps(wf_name):
            static, dyn = _invocation_facts(run)
            for cwd, targets in static:
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
            for _cwd, targets in dyn:
                dynamic.append((key, list(targets)))
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
    RUNS pytest must be added deliberately, not discovered by this scan
    quietly missing it.

    "Runs" is the real invocation test, not the bare token: a file whose only
    `pytest` occurrence is an argument to `pip install` installs the package
    and runs nothing, so it is not a runner and needs no entry here (issue
    #2160 -- `pages.yml` is exactly that file)."""
    on_disk = sorted(p.name for p in WORKFLOWS_DIR.glob("*.yml"))
    assert on_disk, "no workflow files found; the scan below would be vacuous"
    leaking = [
        name for name in on_disk
        if name not in WORKFLOWS
        and _runs_pytest(run for _key, run in _pytest_steps(name))
    ]
    assert not leaking, (
        f"{leaking} run pytest but are not in WORKFLOWS, so this file's scan "
        "does not see their steps at all; add them to WORKFLOWS deliberately. "
        "Only a real pytest invocation counts here: an argument to "
        "`pip install` names a package, not an invocation (issue #2160)."
    )


def test_no_install_only_workflow_is_registered_as_a_pytest_runner():
    """The other side of the same rule: WORKFLOWS may only hold files that
    really run pytest. This is what keeps an install-only file -- the shape
    that used to be listed because a package name read as an invocation --
    out of the scan's bookkeeping instead of in it (issue #2160)."""
    for name in WORKFLOWS:
        runs = [run for _key, run in _pytest_steps(name)]
        assert _runs_pytest(runs), (
            f"{name} is in WORKFLOWS but contains no pytest invocation; if its "
            "only `pytest` token is a `pip install` argument, drop the entry "
            "instead of crediting the file with a run it does not perform"
        )


def test_a_pip_install_of_the_pytest_package_is_not_an_invocation():
    """The distinction itself (issue #2160): `pip install ... pytest` names
    the pytest PACKAGE. The job it appears in runs no pytest, selects no
    target, and must not enter WORKFLOWS or _DYNAMIC_STEPS bookkeeping."""
    install_only = [
        "python3 -m pip install --quiet -e . pytest",
        "pip install pytest",
        "pip3 install --upgrade pip pytest pytest-cov",
        "uv pip install -e . pytest",
        "python3 -m pip install -r requirements.txt",
    ]
    for run in install_only:
        assert _pytest_statements(run) == [], f"{run!r} was read as an invocation"
        static, dynamic = _invocation_facts(run)
        assert static == [] and dynamic == [], f"{run!r} entered the scan"
        assert not _runs_pytest([run]), f"{run!r} was credited with a pytest run"
    assert _installs_pytest("python3 -m pip install --quiet -e . pytest")
    assert not _installs_pytest("python3 -m pytest tests/ -q")


def test_a_genuine_pytest_invocation_is_still_reported():
    """The direction the distinction must NOT swallow: a static target, a
    shell-variable target and a bare `pytest` all still count as running
    pytest, and the two the scan cannot resolve still have to be named in
    _DYNAMIC_STEPS (issue #2160)."""
    assert _runs_pytest(["python3 -m pytest tests/ -q"])
    assert _runs_pytest(["pytest $SEL_PYTEST -q"])
    assert _runs_pytest(["pytest"])
    # a step that installs AND runs: the install is dropped, the run is not
    both = "python3 -m pip install --quiet -e . pytest && python3 -m pytest tests/ -q"
    assert _runs_pytest([both])
    assert _pytest_statements(both) == [(".", ["tests/"])]
    # ...and the run after an install is still subject to the dynamic rule
    both_dynamic = "python3 -m pip install pytest && pytest $SEL_PYTEST -q"
    assert _invocation_facts(both_dynamic) == ([], [(".", ["$SEL_PYTEST"])])
    assert _invocation_facts("pytest -q") == ([], [(".", ["(bare pytest, no testpaths modelled for cwd '.')"])])
    # the modelled bare case still resolves rather than going dynamic
    assert _invocation_facts("cd backends/python && pytest") == (
        [("backends/python", ["tests"])], [])


def test_the_install_only_workflow_on_disk_needs_no_bookkeeping():
    """The real file the issue is about, not only the synthetic strings.
    `pages.yml` installs pytest -- because tools/census_artifact.py loads the
    reference classifier out of tests/test_selfhost_lower.py by path and that
    module imports pytest -- and runs none, so it is in neither registry
    (issue #2160)."""
    runs = [run for _key, run in _pytest_steps("pages.yml")]
    assert runs, "pages.yml no longer mentions pytest; this pin is stale"
    assert not _runs_pytest(runs), "pages.yml is credited with running pytest again"
    assert any(_installs_pytest(run) for run in runs), (
        "pages.yml no longer installs the pytest package, so the install/run "
        "distinction is no longer exercised by a real workflow; drop this pin "
        "rather than leaving it vacuous"
    )
    assert "pages.yml" not in WORKFLOWS
    assert not [key for key in _DYNAMIC_STEPS if key[0] == "pages.yml"]


def test_a_new_pytest_running_workflow_still_reddens_the_scan(tmp_path, monkeypatch):
    """End-to-end on real YAML, both directions at once: a workflow that RUNS
    pytest must still fail the WORKFLOWS anti-staleness check when it is not
    registered, and one that only INSTALLS the package must not -- while an
    unresolved invocation in a registered file still has to be named in
    _DYNAMIC_STEPS (issue #2160)."""
    (tmp_path / "install-only.yml").write_text(
        "jobs:\n"
        "  deploy:\n"
        "    steps:\n"
        "      - run: |\n"
        "          python3 -m pip install --quiet -e . pytest\n"
        "          python3 tools/census_artifact.py --verify --strict\n",
        encoding="utf-8",
    )
    (tmp_path / "runner.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: python3 -m pytest tests/ -q\n",
        encoding="utf-8",
    )
    (tmp_path / "dynamic.yml").write_text(
        "jobs:\n  root-suite:\n    steps:\n      - run: pytest $SEL_PYTEST -q\n",
        encoding="utf-8",
    )
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(module, "WORKFLOWS", ())
    monkeypatch.setattr(module, "_DYNAMIC_STEPS", {})
    with pytest.raises(AssertionError) as caught:
        test_every_pytest_running_workflow_is_scanned()
    assert "runner.yml" in str(caught.value)
    assert "dynamic.yml" in str(caught.value)
    assert "install-only.yml" not in str(caught.value), (
        "an install of the pytest package was reported as a pytest run again"
    )
    # Registering the two runners clears ratchet A; the unresolved invocation
    # in one of them still has to be named in _DYNAMIC_STEPS.
    monkeypatch.setattr(module, "WORKFLOWS", ("runner.yml", "dynamic.yml"))
    test_every_pytest_running_workflow_is_scanned()
    with pytest.raises(AssertionError) as caught:
        test_dynamic_steps_are_all_named_explicitly()
    assert "[('dynamic.yml', 'root-suite')]" in str(caught.value)
    monkeypatch.setattr(
        module, "_DYNAMIC_STEPS",
        {("dynamic.yml", "root-suite"): "synthetic, for this test only"})
    test_dynamic_steps_are_all_named_explicitly()
    # ...and the install-only file never needed a WORKFLOWS entry at all.
    assert "install-only.yml" not in WORKFLOWS


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
        "silently drop it from coverage. This fires on a real invocation only: "
        "an argument to `pip install` names a package, not an invocation, and "
        "is not reported here (issue #2160)."
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
