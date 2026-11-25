"""Issue #3440: under `revl run` / `revl serve --http` nothing drove the timer
clock from a wall source, so an `every` timer armed at boot never fired at run
time (only a test's explicit `advance` moved the clock).

`revl run` now pumps `Clock.advance` from `time.monotonic()` as a task on the
run loop (`pump_clock_from_wall` in backends/python/runtime.py, started by
`hold_repl`/`watch`), and `revl serve` runs a `WallClockPump` thread while the
session is held served. `revl test`/replay keep the virtual clock — nothing
starts a pump there (tests/test_time_coeffect.py pins that, unchanged).

Acceptance (the issue's bar): hold a composition with an `every 100ms` timer
for one second of wall time under `revl run`, and the same under
`revl serve --http`, and count the firings — between 5 and 15.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="drives a live cordis-py composition; install it with "
           "`sh backends/python/setup.sh`",
)

_SOURCE = """
extern emission[log] fn note(m: Str) -> Int = @py {
    with open(__LOG__, "a") as f:
        f.write(m + "\\n")
    return 0
}

component Beat {
  every 100ms { emit note("tick") }
}
"""


def _write_app(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "ticks.log"
    app = tmp_path / "pump.rvl"
    app.write_text(_SOURCE.replace("__LOG__", repr(str(log))), encoding="utf-8")
    return app, log


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT / "backends" / "python")]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.pop("REVL_ESTOP_LATCH", None)
    return env


def _ticks(log: Path) -> int:
    if not log.exists():
        return 0
    return len([line for line in log.read_text(errors="replace").splitlines()
                if line.strip()])


def _wait_for(marker: str, out: Path, deadline: float = 20.0) -> None:
    """Poll the child's captured output until `marker` appears (boot done)."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if out.exists() and marker in out.read_text(errors="replace"):
            return
        time.sleep(0.05)
    raise AssertionError(f"{marker!r} never appeared in {out}")


@needs_cordis
def test_revl_run_drives_the_timer_clock_from_the_wall_3440(tmp_path):
    app, log = _write_app(tmp_path)
    out = tmp_path / "run.out"
    with out.open("w") as out_f:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "revl", "run", str(app)],
            cwd=str(tmp_path), env=_env(), stdin=subprocess.PIPE,
            stdout=out_f, stderr=subprocess.STDOUT)
        try:
            _wait_for("== live", out)
            time.sleep(1.0)          # one second of wall time, held live
            assert proc.stdin is not None
            proc.stdin.close()       # EOF: the run tears down cleanly
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    ticks = _ticks(log)
    assert 5 <= ticks <= 15, f"expected 5..15 ticks in 1s under revl run, got {ticks}"


@needs_cordis
def test_revl_serve_http_drives_the_timer_clock_from_the_wall_3440(tmp_path):
    app, log = _write_app(tmp_path)
    out = tmp_path / "serve.out"
    with out.open("w") as out_f:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "revl", "serve", "--http", str(app),
             "--host", "127.0.0.1", "--port", "0"],
            cwd=str(tmp_path), env=_env(), stdin=subprocess.DEVNULL,
            stdout=out_f, stderr=subprocess.STDOUT)
        try:
            _wait_for("revl serve --http:", out)
            time.sleep(1.0)          # one second of wall time, served live
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    ticks = _ticks(log)
    assert 5 <= ticks <= 15, f"expected 5..15 ticks in 1s under revl serve, got {ticks}"
