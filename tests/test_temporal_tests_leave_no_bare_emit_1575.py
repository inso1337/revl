"""The ts Temporal tests leave no bare `emit` behind (issue #1575).

WHAT WAS WRONG. backends/typescript/test_temporal_target.py loaded the
typescript emitter under the bare module name `emit` at import time, and put
backends/typescript on sys.path, for the rest of the session. Every backend
directory has an `emit.py`, and the py tier reaches its own through that bare
name (`revl._paths.python_backend_emitter`), which refuses another backend's
module rather than run it. So any test that booted a py composition AFTER that
file refused:

    ImportError: the module `emit` is .../backends/typescript/emit.py, not the
    python backend's emitter ...

Measured on main: `tests/test_realm_placement_over_the_transport.py` passes
alone (19 passed) and fails 3 tests when it runs after
`backends/typescript/test_temporal_target.py`. It was found as an
order-dependent red in a combined targeted run.

WHAT IT DOES NOW. The Temporal test loads the emitter by path under a name
nothing else binds (`tests/_load_by_path.py`) and touches neither global. The
emitter binds the bare name only for the duration of its own import of the
Temporal sink (`emit.py::_emit_temporal`, pinned by
tests/test_emit_temporal_leaves_no_global_state.py).

Both checks run in a subprocess: the defect is a property of a whole pytest
session, which a test inside this session cannot observe cleanly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPORAL = "backends/typescript/test_temporal_target.py"
TRANSPORT = "tests/test_realm_placement_over_the_transport.py"

_AFTER_SESSION = r"""
import json, sys
import pytest
rc = pytest.main(["-q", "-p", "no:cacheprovider", sys.argv[1]])
emit = sys.modules.get("emit")
state = {
    "rc": int(rc),
    "bare_emit": getattr(emit, "__file__", None) if emit is not None else None,
}
try:
    from revl._paths import python_backend_emitter
    python_backend_emitter()
    state["py_emitter"] = "ok"
except Exception as error:
    state["py_emitter"] = f"{type(error).__name__}: {error}"
print("STATE " + json.dumps(state))
"""


def _pytest_env() -> dict:
    return dict(os.environ, PYTHONPATH=str(ROOT / "src"))


def test_the_temporal_tests_leave_no_bare_emit():
    """pytest's own `prepend` import mode puts a test file's directory on
    sys.path, so this does not assert on sys.path; what broke the py tier was
    the module registered under the bare name."""
    ran = subprocess.run(
        [sys.executable, "-c", _AFTER_SESSION, TEMPORAL],
        capture_output=True, text=True, timeout=900, cwd=ROOT, env=_pytest_env())
    lines = [line for line in ran.stdout.splitlines() if line.startswith("STATE ")]
    assert lines, ran.stdout + ran.stderr
    state = json.loads(lines[-1][len("STATE "):])
    assert state["rc"] == 0, ran.stdout
    assert state["bare_emit"] is None, state
    assert state["py_emitter"] == "ok", state


@pytest.mark.skipif(importlib.util.find_spec("cordis") is None,
                    reason="the transport file boots a live cordis-py composition; "
                           "install the pinned fork with `sh backends/python/setup.sh`")
def test_the_realm_transport_tests_pass_after_the_temporal_tests():
    """The bad order itself, end to end."""
    ran = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", TEMPORAL, TRANSPORT],
        capture_output=True, text=True, timeout=900, cwd=ROOT, env=_pytest_env())
    assert ran.returncode == 0, ran.stdout[-4000:] + ran.stderr[-2000:]
    assert " passed" in ran.stdout and " failed" not in ran.stdout, ran.stdout[-2000:]
