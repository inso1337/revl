"""Test-root path bootstrap: make `pytest tests/ -q` work from any checkout
or worktree with no PYTHONPATH juggling.

Inserts `<rootdir>/src` ahead of sys.path only when it actually exists —
an installed-package environment (no in-tree src/) is left untouched, and
the backends/*/ suites keep owning their own loader paths (this conftest
scopes to tests/ only).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

if (_SRC / "revl").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


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
