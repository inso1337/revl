"""A child that dies on SIGTERM without saying DOWN is stranded too (issue 265).

Issue 239 made a teardown that was cut short LOUD, and #246 closed the SIGKILL
half of it: a child the conductor had to kill before it said `DOWN` is named and
the run exits non-zero. But the check it installed asked the wrong question --
"did we have to SIGKILL it?" -- and there is more than one way for a child to
die mid-unwind.

`_stop_all` sends SIGTERM (or closes stdin) and then waits. A child that dies
*on that SIGTERM* -- the runner losing the window between `UP` and its own
signal handler (#226), a crash inside the unwind, an external `kill` -- has
`proc.poll() is not None` the moment the wait loop looks at it, so the loop
`continue`d and the child counted as a CLEAN EXIT. No LIFO walk over its
registered entries, no inverse applied, no residue proof, no `DOWN` line, and
conductor rc **0**. G7 (LIFO teardown completeness) and R4 (no residue) both
violated, neither reported: exactly the silence #239 exists to break, arriving
through the door #246 left open.

The check now asks the question that actually distinguishes the two states:
**did it ever say `DOWN`?** Nothing else can answer it. `DOWN` is the runner's
own statement that its unwind reached every registered entry and its no-residue
proof printed; a signal number is not evidence either way.

The other half of widening it is not accusing a child that DID unwind. `DOWN`
is printed microseconds before the process exits and is read by a separate pump
thread, so `proc.poll()` can go non-None with that line still unread. The last
test here pins that: a `DOWN` that lands late is still a `DOWN`.

Same stubbed-child conductor harness as `test_teardown_kill_reporting_239.py`.
No runtime, no subprocess, no port.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement as _placement  # noqa: E402

APP = """
service Work { async fn compute(x: Str) -> Str }
service Control { async fn go() -> Str }

component HotWorker provides work: Work {
  provide work { async fn compute(x) = x }
}
component ControlPlane requires work: Work provides control: Control {
  provide control { async fn go() = work.compute("crossed") }
}
"""

PLACEMENT = """
[processes.provider]
components = ["HotWorker"]

[processes.consumer]
components = ["ControlPlane"]
"""


class _StubProc:
    """A Popen stand-in whose death is the thing under test.

    `silent_on_term=True` is the child this issue is about: it comes up, and
    then the stop signal kills it outright. It is dead before the conductor
    looks -- no `UNLOADING`, no inverse, no residue line, no `DOWN` -- and it
    was never SIGKILLed, so the #246 check saw nothing.

    `down_delay` models the ordinary race instead: the child says `DOWN` and
    exits, but the conductor's pump thread has not read that line yet at the
    instant the process is reaped.
    """

    def __init__(self, name: str, spec: dict, silent_on_term: bool = False,
                 down_delay: float = 0.0):
        self.name = name
        self.spec = spec
        self._lines = [f"[{name}] UP"]
        self._exited = False
        # stdout closes when the child's pipe drains, which is not the same
        # instant as the process being reaped -- the whole point of the race
        # below is that a written line outlives the writer.
        self._eof = False
        self._silent = silent_on_term
        self._down_delay = down_delay
        self.killed = False
        self.stdin = self
        self.returncode = 0

    # --- the conductor reads the child's trace off stdout
    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self._lines:
                return self._lines.pop(0)
            if self._eof:
                raise StopIteration
            time.sleep(0.005)

    # --- stdin (the rust stop mode closes it; py/node/java get SIGTERM)
    def write(self, _text):
        pass

    def flush(self):
        pass

    def close(self):
        self.terminate()

    # --- process control
    def poll(self):
        return 0 if self._exited else None

    def wait(self, timeout=None):
        deadline = time.monotonic() + (10.0 if timeout is None else timeout)
        while not self._exited and time.monotonic() < deadline:
            time.sleep(0.005)
        if not self._exited:
            raise subprocess.TimeoutExpired(cmd="stub", timeout=timeout)
        return 0

    def terminate(self):
        if self._silent:
            # SIGTERM landed before the unwind could run (or instead of it):
            # the process is simply gone, with everything it registered still
            # owed. This is the case that used to read as a clean exit.
            self._exited = True
            self._eof = True
            return
        if self._down_delay:
            # DOWN really was printed; the reader is just behind. Deliver it
            # after the exit, which is the order the real race produces.
            self._exited = True

            def late():
                time.sleep(self._down_delay)
                self._lines.append(f"[{self.name}] DOWN")
                self._eof = True

            threading.Thread(target=late, daemon=True).start()
            return
        self._lines.append(f"[{self.name}] DOWN")
        self._exited = True
        self._eof = True

    def kill(self):
        self.killed = True
        self._exited = True
        self._eof = True


def _run(tmp_path, monkeypatch, silent: set[str] | None = None,
         down_delay: float = 0.0):
    """Boot the composition through the real conductor with stubbed children."""
    silent = silent or set()
    procs: dict[str, _StubProc] = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        name = spec["name"]
        procs[name] = _StubProc(name, spec, silent_on_term=name in silent,
                                down_delay=down_delay)
        return procs[name]

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)

    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    plc = tmp_path / "p.toml"
    plc.write_text(PLACEMENT, encoding="utf-8")
    rc = _placement.run_placement([str(app)], str(plc), once=True)
    return rc, procs


# ---------------------------------------------------------------------------
# the regression
# ---------------------------------------------------------------------------


def test_a_child_that_dies_on_sigterm_without_down_fails_the_run(
        tmp_path, monkeypatch, capsys):
    """THE test (issue 265's exit test). A child killed by SIGTERM after `UP`
    and before it finished tearing down must make the conductor exit non-zero
    and NAME it. Asserted on the conductor's observable output -- rc plus the
    stranded child's name in the report -- not on internal state.

    Before the widening, the `rc != 0` assertion is the one that fails: the
    child was never SIGKILLed, so nothing recorded it and rc stayed 0.
    """
    rc, procs = _run(tmp_path, monkeypatch, silent={"consumer"})
    out = capsys.readouterr()

    # the trace really is truncated: it came up and then stopped dead, with no
    # residue proof behind it -- and no SIGKILL was involved
    assert "[consumer] UP" in out.out
    assert "[consumer] DOWN" not in out.out
    assert procs["consumer"].killed is False, "this is the SIGTERM path"
    # ... while the process next to it unwound normally
    assert "[provider] DOWN" in out.out

    # THE HALF THAT MATTERS
    assert rc != 0, "a child that died on SIGTERM mid-teardown reported success"

    err = out.err
    assert "consumer" in err, "the stranded child must be named"
    assert "HALTED" in err
    assert "STRANDED" in err
    assert "UNKNOWN" in err
    # the child that DID unwind is not accused
    named = err.split("exited before saying DOWN:")[1].split("\n")[0]
    assert "provider" not in named


def test_stop_all_names_a_child_that_exited_without_ever_saying_down():
    """The unit contract, stated the new way: `_stop_all` names every child
    that exited before saying `DOWN`, whether or not it had to be killed."""
    down: set[str] = set()
    good = _StubProc("good", {})
    quiet = _StubProc("quiet", {}, silent_on_term=True)

    def finish(proc):
        _StubProc.terminate(proc)
        down.add(proc.name)

    # the clean child's terminate both ends it and lands its DOWN line, the way
    # the conductor's pump thread would
    good.terminate = lambda: finish(good)

    stranded = _placement._stop_all(
        {"good": (good, "term"), "quiet": (quiet, "term")},
        is_down=lambda n: n in down, grace=0.2)

    assert stranded == ["quiet"]
    # and it was NOT a kill: the old check had nothing to see here
    assert quiet.killed is False and good.killed is False


def test_a_clean_teardown_is_still_rc_zero_and_says_nothing(
        tmp_path, monkeypatch, capsys):
    """The control, restated against the wider check: a composition whose
    children unwind normally still exits 0 and prints no stranding report."""
    rc, _procs = _run(tmp_path, monkeypatch)
    out = capsys.readouterr()
    assert rc == 0
    assert "[provider] DOWN" in out.out and "[consumer] DOWN" in out.out
    assert "HALTED" not in out.err and "STRANDED" not in out.err


def test_a_down_line_read_after_the_exit_is_still_a_down(
        tmp_path, monkeypatch, capsys):
    """The other half of widening the check: do not accuse a child that DID
    unwind. `DOWN` is printed microseconds before the process exits and is read
    on a separate thread, so `proc.poll()` routinely goes non-None with that
    line still in the pipe. Reporting on that instant would turn every healthy
    run into a coin flip -- so the check waits for the reader to catch up
    before concluding no `DOWN` is coming."""
    rc, _procs = _run(tmp_path, monkeypatch, down_delay=0.3)
    out = capsys.readouterr()
    assert "[consumer] DOWN" in out.out
    assert rc == 0, "a late-read DOWN is still a DOWN"
    assert "STRANDED" not in out.err
