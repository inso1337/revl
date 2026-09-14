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
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"

if (_SRC / "revl").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

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
#   * `revl.run._Driver._emit_module` registers a `revl_run_gen{N}` module in
#     `sys.modules` per generation, numbered from a PER-DRIVER counter, so the
#     names are shared across every driver in the process. A test that leaves one
#     behind both inflates the count a later test sees and collides by name with
#     that later test's own generations, which is what made
#     `tests/test_issue_541_admit_module_reclaim.py` fail in a multi-file session.
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
