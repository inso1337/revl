"""v3.0 release gate E3 — the live-systems demo runs from a clean checkout.

This is the thin gate wrapper: it drives `demo/live_systems/run_demo.py`, the
scripted swap/why/apply demo, and asserts it passes. The demo itself holds the
substance (it shells out to the real `revl` CLI and asserts each stage's
observable outcome); this test is what makes `pytest tests/` — the frontend CI
job and a local run — carry the gate.

The live stages need the cordis-py runtime, so this skips cleanly where it is
absent (the frontend matrix), exactly like the other runtime-integration tests.
It runs for real anywhere the runtime is installed (a `backends/python/.venv`
interpreter, or the CI job that sets it up). See docs/swap.md "Running the
live-systems demo".
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "live_systems" / "run_demo.py"

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the live-systems demo drives a live cordis-py composition — install "
           "it (sh backends/python/setup.sh) and run under that interpreter",
)


@needs_runtime
def test_live_systems_demo_from_clean():
    # REVL_DEMO_REQUIRE=1 turns the runtime-absent skip inside the demo into a
    # failure; we only reach here with cordis present, so require it — a silent
    # skip must never read as a pass.
    proc = subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=str(ROOT),
        env={**__import__("os").environ, "REVL_DEMO_REQUIRE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout
    assert "live-systems demo OK" in proc.stdout, proc.stdout
