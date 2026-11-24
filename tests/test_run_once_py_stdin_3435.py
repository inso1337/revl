"""`revl run --once` on the py tier tears down without reading stdin (#3435).

WHAT WAS WRONG. `run_command` ended in `driver.hold_repl(once=hold_once)`, and
`hold_once` was a parameter only `revl dev` set. The `--once` of the command
line reached the other tiers and `--placement`, never the py path, so
`revl run --once FILE` opened the interactive prompt and waited for end of
input on stdin, exactly like `revl run` without the flag. Run from a script or
a service manager, whose stdin is never closed, it hung.

WHAT HOLDS NOW. `--once` boots, prints the once banner, tears down and exits
whatever stdin is. The stdin here is a pipe nobody writes to or closes.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the py tier needs the cordis-py runtime (backends/python/setup.sh)",
)

COMPOSITION = """service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 42
  }
}
"""

# Generous for a loaded machine, tiny against the hang it guards: before the
# fix the process never exits on its own.
DEADLINE_S = 60


def _run_with_open_stdin(tmp_path, *flags):
    rvl = tmp_path / "app.rvl"
    rvl.write_text(COMPOSITION, encoding="utf-8")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(
               p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p)}
    log = tmp_path / "run.log"
    with log.open("w", encoding="utf-8") as sink:
        # `Popen.communicate` closes stdin when it has nothing to send, which
        # would hide the defect: wait on the process and read its log instead.
        proc = subprocess.Popen(
            [sys.executable, "-m", "revl", "run", *flags, str(rvl)],
            stdin=subprocess.PIPE, stdout=sink, stderr=subprocess.STDOUT,
            text=True, cwd=str(tmp_path), env=env)
        try:
            # Nothing is written to stdin and it is not closed: the process
            # has to leave by itself.
            proc.wait(timeout=DEADLINE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail(f"`revl run --once` still waiting on stdin after "
                        f"{DEADLINE_S}s:\n{log.read_text(encoding='utf-8')}")
        finally:
            proc.stdin.close()
    out = log.read_text(encoding="utf-8")
    return proc.returncode, out


def test_run_once_exits_on_its_own_with_stdin_held_open(tmp_path):
    code, out = _run_with_open_stdin(tmp_path, "--once")
    assert code == 0, out
    assert "== once" in out and "boot complete, tearing down ==" in out, out
    assert "== live" not in out, out
    assert "== teardown" in out, out
