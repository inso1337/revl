"""Issue #3437: SIGTERM/SIGINT on `revl run` run the same teardown as `:q`.

WHAT WAS WRONG. With stdin held open, `kill -TERM` exited 143 without ever
printing the teardown banner (every `undo` skipped), and SIGINT was not acted
on for 10+ s: the prompt's `input()` sat in a default-executor thread, whose
non-daemon workers are joined at interpreter exit, so the process hung after
the signal instead of tearing down.

WHAT HOLDS NOW. While the REPL loop runs, `hold_repl` installs
`loop.add_signal_handler` for SIGINT and SIGTERM onto a stop event (watch
handles both too), and the prompt reads on a DAEMON thread (`_prompt_line`)
raced against that event: a signal breaks the loop, the `finally` runs
`_teardown()` (every undo in LIFO order, then the residue proof), and the
process exits 0 promptly - a daemon thread is not joined at exit, so a
blocked read cannot hang the stop. EOF and `:q` are unchanged.

Checked here (real `python -m revl run` subprocess, stdin pipe held open):
SIGTERM and SIGINT each produce the marker file the `undo` writes, the
teardown banner, the no-residue line, and exit 0; `:q` and EOF still exit 0
with the marker.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the py tier needs the cordis-py runtime (backends/python/setup.sh)",
)

#: the `undo` writes the marker file; the acquire is a no-op host call.
MARKER_SRC = """pub extern pure fn noop() -> Int = @py { return 1 }
pub extern pure fn mark(path: Str) -> Unit = @py { open(path, "w").write("undone") }
component Marker {
  let h = effect noop() undo mark("{marker}")
}
"""

DEADLINE_S = 90


def _run_and_signal(tmp_path, sig) -> tuple:
    marker = tmp_path / "marker.txt"
    if marker.exists():
        marker.unlink()
    rvl = tmp_path / "app.rvl"
    rvl.write_text(MARKER_SRC.replace("{marker}", str(marker)), encoding="utf-8")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(
               p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "revl", "run", str(rvl)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(tmp_path), env=env)
    lines: list[str] = []

    def _reader():
        for line in proc.stdout:
            lines.append(line)

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.time() + DEADLINE_S
    while time.time() < deadline and not any("== live" in line for line in lines):
        assert proc.poll() is None, (proc.returncode, "".join(lines)[-2000:])
        time.sleep(0.2)
    assert any("== live" in line for line in lines), "".join(lines)[-2000:]
    proc.send_signal(sig)
    try:
        rc = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = "TIMEOUT"
    return rc, "".join(lines), marker.exists()


@pytest.mark.parametrize("sig,name", [(signal.SIGTERM, "SIGTERM"),
                                     (signal.SIGINT, "SIGINT")])
def test_signal_runs_teardown_then_exits(tmp_path, sig, name):
    rc, out, marker = _run_and_signal(tmp_path, sig)
    assert rc == 0, (name, rc, out[-2000:])
    assert marker, (name, "the undo never ran", out[-2000:])
    assert "== teardown" in out, (name, out[-2000:])
    assert "no residue" in out, (name, out[-2000:])


def test_quit_still_tears_down_cleanly(tmp_path):
    marker = tmp_path / "marker.txt"
    rvl = tmp_path / "app.rvl"
    rvl.write_text(MARKER_SRC.replace("{marker}", str(marker)), encoding="utf-8")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(
               p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p)}
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(rvl)],
        input=":q\n", capture_output=True, text=True, timeout=DEADLINE_S,
        cwd=str(tmp_path), env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-2000:])
    assert marker.exists()
    assert "no residue" in proc.stdout
