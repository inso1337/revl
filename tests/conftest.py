"""Test-root path bootstrap: make `pytest tests/ -q` work from any checkout
or worktree with no PYTHONPATH juggling.

Inserts `<rootdir>/src` ahead of sys.path only when it actually exists —
an installed-package environment (no in-tree src/) is left untouched, and
the backends/*/ suites keep owning their own loader paths (this conftest
scopes to tests/ only).
"""

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

if (_SRC / "revl").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


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
