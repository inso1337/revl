"""A cordis-py install that is present but fails on import.

Finding the `cordis` package is not the same as being able to boot it. The
pinned cordis-py fork raises `TypeError: NoneType takes no arguments` from
`cordis.hmr` when its optional `watchdog` extra is missing. revl treated
"found" as "usable": the MCP runtime gate reported a runtime it could not
start, and `revl run` (and the pool's `run-once-py` runner, which shells out
to it) died with a raw traceback.

Each test plants a stand-in `cordis` package that raises on import, ahead of
any real one on the path, and runs in a subprocess so nothing it imports
leaks into this process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

COMPOSITION = """service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 42
  }
}
"""


@pytest.fixture
def broken_cordis(tmp_path) -> dict:
    """An environment whose first `cordis` on the path raises on import, the
    way the fork does without `watchdog`."""
    package = tmp_path / "shadow" / "cordis"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "class _WatchHandler(None):\n    pass\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path / "shadow"), str(ROOT / "src")])
    return env


def test_the_runtime_probe_reads_a_broken_install_as_not_importable(broken_cordis):
    probe = subprocess.run(
        [sys.executable, "-c",
         "from revl.mcp import runtime_gate as g; print(g.cordis_importable())"],
        env=broken_cordis, capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False"


def test_revl_run_names_the_import_failure_instead_of_a_traceback(
        broken_cordis, tmp_path):
    source = tmp_path / "counter.rvl"
    source.write_text(COMPOSITION, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-P", "-m", "revl", "run", "--once", str(source)],
        env=broken_cordis, capture_output=True, text=True, timeout=300,
        cwd=tmp_path)
    assert run.returncode == 3, run.stdout + run.stderr
    out = run.stdout + run.stderr
    assert "installed but does not import" in out, out
    assert "TypeError" in out and "setup.sh" in out, out
    assert "Traceback" not in out, out
