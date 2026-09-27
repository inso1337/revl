"""Test-root path bootstrap: make `pytest tests/ -q` work from any checkout
or worktree with no PYTHONPATH juggling.

Inserts `<rootdir>/src` ahead of sys.path only when it actually exists —
an installed-package environment (no in-tree src/) is left untouched, and
the backends/*/ suites keep owning their own loader paths (this conftest
scopes to tests/ only).

Appends `<rootdir>` itself, so a module under tests/ can import a repo-root
directory. `tools/`, `backends/`, `tests/` and friends are plain directories
at the repository root with no `__init__.py`, so importing them needs the root
on sys.path. `python -m pytest` supplies that by prepending the cwd; the
`pytest` console script does NOT, and the console script is what the
`root-suite-affected` CI job (`.github/workflows/ci.yml`) and
`tools/pre_merge.sh` both run. Three modules here import a repo-root directory
inside a test body -- `tests/test_affected_tests.py` (tools.affected_tests),
`tests/test_274_navigable_slice2.py` (tests.test_evidence_policy) and
`tests/test_inverse_capture_by_value.py` (backends.<tier>.emit) -- so under the
CI invocation they raised `ModuleNotFoundError` instead of checking anything,
and the affected suite reported a red that had nothing to do with the change
under test. It looked green locally for two independent reasons: `python -m
pytest` prepends the cwd, and pytest imports every collected module before
running any test, so one module that bootstraps the root itself
(`tests/test_reserved_lexicon_sweep.py`) papered over the others whenever the
selector picked it too. Appended rather than inserted, so the resolution order
of everything that already resolved is unchanged.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# The repository a git hook was running for is not the repository under test. #
# --------------------------------------------------------------------------- #
#
# git exports the committing repository into every hook it runs. Measured on
# git 2.50: a pre-commit hook in a linked worktree gets GIT_DIR (that worktree's
# gitdir under the shared .git/worktrees/) and GIT_INDEX_FILE (its index, or a
# temporary index for `commit -a` and `commit <paths>`); `git -c k=v commit`
# adds GIT_CONFIG_PARAMETERS. tools/hooks/pre-commit runs pytest, and every
# fixture that shells out to git inherits them. GIT_DIR outranks both the
# working directory and `git -C`, so a fixture's `git init` in its tmp dir
# re-initialises the COMMITTING repository instead:
#
#   * `git init` with a GIT_DIR that does not end in `/.git` guesses a bare
#     repository and writes `core.bare = true`. A worktree's gitdir never ends
#     in `/.git`, and its config IS the shared `.git/config`, so every checkout
#     of the repository went bare at once.
#   * `git config user.name t` wrote the shared identity, which is how commits
#     authored `t <t@example.com>` reached main.
#   * `git add` wrote the fixture's README into that worktree's own index,
#     and `git checkout -b` / `git commit` would move its branch.
#
# The fix is here, once, rather than in each fixture: these are stripped when
# this file is imported, before any test module is collected and so before any
# fixture or tool runs. tools/hooks/pre-commit strips the same set before it
# starts pytest, for runs this file does not reach.
#
# The list is git's own (`git rev-parse --local-env-vars`): the variables git
# clears when it runs a command in ANOTHER repository, e.g. a submodule. Every
# one of them names or configures the repository a command acts on:
#   GIT_DIR, GIT_COMMON_DIR                 where the repository and its refs are
#   GIT_WORK_TREE, GIT_IMPLICIT_WORK_TREE   where checkouts are written
#   GIT_INDEX_FILE                          which index `add` and `commit` use
#   GIT_OBJECT_DIRECTORY,
#   GIT_ALTERNATE_OBJECT_DIRECTORIES        where objects are written and read
#   GIT_PREFIX, GIT_INTERNAL_SUPER_PREFIX   the hook's path inside that tree
#   GIT_SHALLOW_FILE, GIT_GRAFT_FILE,
#   GIT_NO_REPLACE_OBJECTS,
#   GIT_REPLACE_REF_BASE                    how that repository's history reads
#   GIT_CONFIG, GIT_CONFIG_PARAMETERS,
#   GIT_CONFIG_COUNT                        config given to the committing
#                                           command (`git -c ...`); without
#                                           the count, GIT_CONFIG_KEY_<n> and
#                                           GIT_CONFIG_VALUE_<n> are inert
#
# A second, smaller set names no repository but is still the hook's and not
# the test's: the identity and date of the commit being made. `git commit`
# exports GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL and GIT_AUTHOR_DATE to its hooks
# (measured), and they outrank a fixture's own `git config user.name`, so
# under the hook every fixture commit was authored as the committer, with one
# frozen date, and a run under the hook differed from the same run in CI.
#
# Kept on purpose: GIT_EXEC_PATH and GIT_EDITOR, which change nothing a test
# can observe, and GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM / GIT_CONFIG_NOSYSTEM,
# which a test may set for its own subprocesses.
# tests/test_hook_git_env_isolation.py holds the first list against the
# installed git's and runs a fixture under a simulated hook environment.
REPOSITORY_LOCAL_GIT_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)

HOOK_COMMIT_IDENTITY_ENV = (
    "GIT_AUTHOR_DATE",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_DATE",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_NAME",
)

for _name in REPOSITORY_LOCAL_GIT_ENV + HOOK_COMMIT_IDENTITY_ENV:
    os.environ.pop(_name, None)
del _name

def pytest_configure(config):
    # tools/hooks/pre-commit runs with pytest-timeout's `--timeout=60`. A test
    # that is slow by nature (not re-deriving anything a session could share)
    # carries `@pytest.mark.timeout(N)` with the measurement and the reason
    # beside it (issue #1449). CI does not install pytest-timeout; declaring the
    # marker here keeps it a known marker there, where it has no effect.
    config.addinivalue_line(
        "markers", "timeout(seconds): per-test limit for pytest-timeout")


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"

if (_SRC / "revl").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The same for every python a test starts. Without it, a child resolves revl
# through the interpreter's own install, which for the main checkout's `.venv`
# (the one tools/hooks/pre-commit falls back to in a worktree) is an editable
# `.pth` entry naming the MAIN checkout's src/: in-process imports read this
# tree and `python -m revl` in a subprocess read another. PYTHONPATH outranks
# site-packages and `.pth` entries. Where revl is installed from this same tree
# (CI) this changes nothing. tests/test_child_python_resolves_this_tree.py.
if (_SRC / "revl").is_dir():
    _inherited = os.environ.get("PYTHONPATH", "")
    if _inherited.split(os.pathsep)[0] != str(_SRC):
        os.environ["PYTHONPATH"] = (
            str(_SRC) + (os.pathsep + _inherited if _inherited else ""))
    del _inherited

if _ROOT.is_dir() and str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))


# A test that needs the cordis-py runtime carries its own guard
# (`pytest.mark.skipif(importlib.util.find_spec("cordis") is None, ...)`). When
# an author forgets one, the test does not skip on a runtime-less checkout: it
# FAILS, with the runtime's own "not installed" diagnostic, and reds the
# `frontend` CI job for a reason that has nothing to do with the change under
# test. That happened twice in two days (tests/test_mcp_authoring_trust.py,
# tests/test_r2_allowlist_key_binding.py).
#
# This converts exactly that failure into a skip, and ONLY when cordis really is
# absent. When the runtime IS installed -- the `frontend-cordis` CI job, and any
# dev running under backends/python/.venv -- the net is inert and the same error
# still fails loudly, so it can never hide a real runtime defect. It is a
# net under the `frontend` job, not a substitute for the guard: the guard is
# what makes the skip reason readable.
_RUNTIME_MISSING = "the cordis-py runtime is not installed"


def _cordis_is_absent() -> bool:
    return importlib.util.find_spec("cordis") is None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if (report.when == "call" and report.failed and call.excinfo is not None
            and _RUNTIME_MISSING in str(call.excinfo.value)
            and _cordis_is_absent()):
        report.outcome = "skipped"
        report.longrepr = (
            str(item.path), item.location[1] + 1,
            "Skipped: this test drives a live composition and is missing its "
            "cordis-py guard (net in tests/conftest.py) -- add "
            'pytest.mark.skipif(importlib.util.find_spec("cordis") is None, ...)')
    return report


def _reset_cordis_globals() -> None:
    """Drop the process-wide runtime state one test can leave for the next.

    The cordis-py backend keeps two module globals that outlive a single test:

      * `runtime._LIVE_INSTANCES` (backends/python/runtime.py) maps a template
        name to its live spawned instances. A test that leaves an instance live
        (or one whose teardown does not fully dispose) leaves an entry behind,
        so a later test that enumerates the same template name counts too many
        instances and fails only because of collection order. The hot-swap
        migration tests read this registry, which is exactly where the order
        dependence showed up.
      * `cordis.timer._clock` caches a clock bound to the event loop that was
        current when it was first built. Once dropped it is rebuilt against the
        loop the next test uses, so no test inherits a clock bound to another
        test's loop.

    This only touches modules a test has already imported (looked up in
    `sys.modules`, never imported here), so a checkout without the cordis-py
    runtime -- where neither module is ever loaded -- is left completely alone.
    """
    runtime = sys.modules.get("runtime")
    live = getattr(runtime, "_LIVE_INSTANCES", None)
    if isinstance(live, dict):
        live.clear()

    timer = sys.modules.get("cordis.timer")
    set_clock = getattr(timer, "set_clock", None)
    if callable(set_clock):
        set_clock(None)


@pytest.fixture(autouse=True)
def _isolate_cordis_runtime_state():
    """Give every test the clean runtime globals it sees when run alone.

    Reset before the test so a leak from an earlier test cannot reach it, and
    again after so this test cannot reach the next one. A no-op on a checkout
    that never loads the cordis-py runtime.
    """
    _reset_cordis_globals()
    try:
        yield
    finally:
        _reset_cordis_globals()


# --------------------------------------------------------------------------- #
# Issue #1021: import state one test leaves behind for the next.
# --------------------------------------------------------------------------- #
#
# Two independent order-dependent failures were found in one day, both of this
# shape: a test mutates process-global import state, never puts it back, and a
# later test reads it as if it had run alone.
#
#   * `tests/test_packaging.py` prepended the repository root to `sys.path` to
#     reach `hatch_build.py`. The repository root is ALSO the working directory
#     `pytest tests/` runs from, so what it left at `sys.path[0]` was a SECOND
#     working-directory entry. `tests/test_317_cwd_import_shadowing.py` asserts
#     that `drop_cwd_entry()` leaves no working-directory entry at the head, and
#     that call removes exactly one -- so two of its tests passed or failed on
#     collection order. That file is the cwd-shadowing regression test, so a real
#     regression in import isolation and a test-order artefact looked identical.
#
#   * `revl.run._Driver._emit_module` registers a `revl_run_gen*` module in
#     `sys.modules` per generation. The name used to be `revl_run_gen{N}` from a
#     PER-DRIVER counter, so the names were shared across every driver in the
#     process: a test that left one behind both inflated the count a later test
#     saw and COLLIDED by name with that later test's own generations, which is
#     what made `tests/test_issue_541_admit_module_reclaim.py` fail in a
#     multi-file session.
#
#     #1046 fixed the naming itself -- each driver now owns a distinct slice of
#     the namespace, so a leftover generation can no longer collide with anyone,
#     and that test now reads its own driver rather than a process-wide count.
#     This half of the fixture stays, narrowed in purpose: not collision
#     containment any more, just the leak hygiene its name says. A generation a
#     test registers and does not reclaim is still a whole emitted module pinned
#     in `sys.modules` for the rest of the session, and still something a later
#     test can enumerate. Dropping it here would only move that cleanup into
#     every test that drives a composition.
#
# Restoring is deliberately silent rather than a failure: the point is that no
# test can observe another's import state, whatever the collection order, and a
# leak becomes a local matter for the test that causes it. The non-vacuity anchor
# is `tests/test_1021_import_state_isolation.py`, which drives both halves in a
# subprocess with and without this fixture.

_GEN_MODULE_PREFIX = "revl_run_gen"


def _generation_modules() -> set:
    """The `revl_run_gen{N}` entries `_Driver._emit_module` registers."""
    return {name for name in sys.modules if name.startswith(_GEN_MODULE_PREFIX)}


@pytest.fixture(autouse=True)
def _isolate_import_state():
    """Give every test the `sys.path` and generation-module namespace it sees
    when its own file runs alone.

    `sys.path` is restored wholesale, so an entry a test adds to reach a helper
    (`hatch_build`, a `backends/<tier>` emitter, a bench module) is gone by the
    time the next test runs. Anything the test imported through that entry stays
    in `sys.modules`, so a later test in the same file still resolves it.

    Generation modules are only ever REMOVED -- an entry the test registered and
    did not reclaim is dropped. One a test evicted is left evicted: absent is the
    state a file sees when it runs alone, and re-pinning a disposed generation
    would undo exactly the reclaim #541 added.
    """
    path_before = list(sys.path)
    generations_before = _generation_modules()
    try:
        yield
    finally:
        sys.path[:] = path_before
        for name in _generation_modules() - generations_before:
            del sys.modules[name]
