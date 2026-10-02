"""`tests/test_replay.py` must leave the MCP server's global session as it found it.

Two of its MCP-surface tests assigned `revl.mcp.server.SESSION = Session()` and
loaded a composition into it with no teardown. Any later test in the same
process that loads through the server then met "a composition is already
loaded". `tests/test_mcp.py::test_revl_call_surfaces_ticket_for_class_c_crossing`
failed that way whenever `test_replay.py` ran first, and passed on its own.

Each file passes alone, so only a run that puts them in the bad order can see
the leak. This test runs exactly that order in a fresh interpreter.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

LEAKERS = [
    "tests/test_replay.py::test_replay_tools_say_recording_must_be_switched_on_at_load",
    "tests/test_replay.py::test_real_cordis_bisect_is_exposed_as_an_mcp_tool",
]
VICTIM = "tests/test_mcp.py::test_revl_call_surfaces_ticket_for_class_c_crossing"


@pytest.mark.skipif(importlib.util.find_spec("cordis") is None,
                    reason="the leaking tests and the victim boot a live "
                           "cordis-py composition")
def test_the_mcp_ticket_test_passes_after_the_replay_mcp_tests(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", f"--basetemp={tmp_path / 'run'}",
         *LEAKERS, VICTIM],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    assert proc.returncode == 0 and "3 passed" in tail, (
        proc.stdout[-4000:] + proc.stderr[-2000:])
