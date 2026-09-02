"""A child SIGKILLed mid-teardown must not report success (issue 239).

`_stop_all` terminated every child, then gave each one a flat five wall-clock
seconds before SIGKILL. Two things were wrong with that, and the second is the
one that made the first dangerous:

1. Five seconds is a proxy for "the child finished unwinding", and a bad one:
   it scales with the machine, not with the teardown. A consumer with a map
   inverse, a residue proof and a flush to get through lost that race on a slow
   CI runner while the provider next to it, with strictly less to do, finished.
   The wait is now on the child's own `[<name>] DOWN` line -- the runner's
   statement that its LIFO unwind covered every registered entry (G7) and its
   no-residue proof printed (R4) -- with the clock demoted to a hang backstop.

2. The kill was INDISTINGUISHABLE FROM A CLEAN EXIT at the call site, so the
   conductor still returned 0. A truncated trace with `rc=0` passes
   `assert result.returncode == 0` and fails some later teardown assertion,
   which is how this surfaced: as an intermittent red on unrelated PRs. A
   killed child is now reported, and the run's exit status is non-zero.

The conductor says the same thing about it that `revl estop` says about a halt
(item 443) and `revl recover` about an unreconciled entry (item 440): the
entries are STRANDED and the residue is UNKNOWN. That is exactly the epistemic
position a kill mid-unwind leaves you in, and it already had a vocabulary.

Everything here drives the real `run_placement` conductor with the stubbed-child
pattern from `tests/test_capability_realm_placement.py`. No runtime, no
subprocess, no port.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

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
    """A Popen stand-in whose teardown behaviour is the thing under test.

    `wedged=True` models the child this issue is about: it accepts the stop
    signal and keeps grinding through its unwind, so it has said nothing yet
    when the conductor's patience runs out. A SIGKILL then lands mid-teardown --
    no `UNLOADING`, no inverse, no residue line, no `DOWN`.
    """

    def __init__(self, name: str, spec: dict, wedged: bool = False,
                 unwind: float = 0.0):
        self.name = name
        self.spec = spec
        self._lines = [f"[{name}] UP"]
        self._exited = False
        self._wedged = wedged
        self._unwind = unwind
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
            if self._exited:
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
        if self._wedged:
            return  # asked to stop; still unwinding, still silent
        if self._unwind:
            time.sleep(self._unwind)
        self._finish()

    def kill(self):
        # SIGKILL: it dies where it stands. Whatever it had left to unwind is
        # not unwound, and it never gets to say DOWN.
        self.killed = True
        self._exited = True

    def _finish(self):
        if not self._exited:
            self._lines.append(f"[{self.name}] DOWN")
            self._exited = True


def _run(tmp_path, monkeypatch, wedge: set[str] | None = None,
         unwind: float = 0.0):
    """Boot the composition through the real conductor with stubbed children."""
    wedge = wedge or set()
    procs: dict[str, _StubProc] = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        name = spec["name"]
        procs[name] = _StubProc(name, spec, wedged=name in wedge, unwind=unwind)
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
# the regression: a killed child is loud
# ---------------------------------------------------------------------------


def test_a_child_killed_mid_teardown_is_reported_and_the_run_fails(
        tmp_path, monkeypatch, capsys):
    """THE test. Force the window, then assert BOTH halves of the symptom:
    the trace really is truncated, AND the conductor no longer calls that
    success. Before the fix the second assertion is the one that fails, which
    is precisely the silence this issue is about."""
    monkeypatch.setenv("REVL_TEARDOWN_GRACE", "0.2")
    rc, procs = _run(tmp_path, monkeypatch, wedge={"consumer"})
    out = capsys.readouterr()

    # the child really was killed mid-teardown
    assert procs["consumer"].killed is True
    # ... and its trace really is truncated: it came up and then stopped dead,
    # with no DOWN and so no residue proof behind it
    assert "[consumer] UP" in out.out
    assert "[consumer] DOWN" not in out.out
    # ... while the process that had less to do finished cleanly
    assert "[provider] DOWN" in out.out
    assert procs["provider"].killed is False

    # THE HALF THAT MATTERS: this is not a clean exit and must not read as one
    assert rc != 0, "a child SIGKILLed mid-teardown reported success"

    # and the conductor says WHAT it does not know, in the vocabulary item 443
    # and item 440 already established for exactly this state
    err = out.err
    assert "consumer" in err
    assert "HALTED" in err
    assert "STRANDED" in err
    assert "UNKNOWN" in err
    assert "REVL_TEARDOWN_GRACE" in err
    # the provider unwound cleanly, so it is not accused of stranding anything
    assert "provider" not in err.split("SIGKILLed before saying DOWN:")[1].split("\n")[0]


def test_a_clean_teardown_is_still_rc_zero_and_says_nothing(
        tmp_path, monkeypatch, capsys):
    """The control. Nothing above may be bought by failing a healthy run: a
    composition whose children unwind normally still exits 0 and prints no
    stranding report."""
    rc, procs = _run(tmp_path, monkeypatch)
    out = capsys.readouterr()
    assert rc == 0
    assert all(not p.killed for p in procs.values())
    assert "[provider] DOWN" in out.out and "[consumer] DOWN" in out.out
    assert "HALTED" not in out.err and "STRANDED" not in out.err


def test_a_slow_but_finishing_teardown_is_not_killed(
        tmp_path, monkeypatch, capsys):
    """The wait is on the DOWN line, not on the clock. A child that takes real
    time to unwind but gets there is not killed and does not fail the run --
    the backstop is for a wedged child, not a busy one."""
    monkeypatch.setenv("REVL_TEARDOWN_GRACE", "5")
    rc, procs = _run(tmp_path, monkeypatch, unwind=0.4)
    out = capsys.readouterr()
    assert rc == 0
    assert all(not p.killed for p in procs.values())
    assert "[consumer] DOWN" in out.out


# ---------------------------------------------------------------------------
# `_stop_all` itself
# ---------------------------------------------------------------------------


def test_stop_all_returns_the_names_it_had_to_kill():
    """The unit contract the conductor is built on: `_stop_all` names every
    child it killed before that child said DOWN, and names no other."""
    down: set[str] = set()
    good = _StubProc("good", {})
    bad = _StubProc("bad", {}, wedged=True)

    def finish(proc):
        proc._finish()
        down.add(proc.name)

    # `terminate` on the clean child both ends it and lands its DOWN line, the
    # way the conductor's pump thread would.
    good.terminate = lambda: finish(good)

    killed = _placement._stop_all(
        {"good": (good, "term"), "bad": (bad, "term")},
        is_down=lambda n: n in down, grace=0.2)

    assert killed == ["bad"]
    assert bad.killed is True and good.killed is False


def test_stop_all_does_not_hang_on_a_wedged_child():
    """The design constraint: the goal is that a kill is REPORTED, not that it
    never happens. A child that never unwinds must still not hang the conductor,
    and the group shares one backstop rather than paying it per child."""
    wedged = {f"p{i}": (_StubProc(f"p{i}", {}, wedged=True), "term")
              for i in range(4)}
    started = time.monotonic()
    killed = _placement._stop_all(wedged, is_down=lambda _n: False, grace=0.3)
    elapsed = time.monotonic() - started

    assert sorted(killed) == ["p0", "p1", "p2", "p3"]
    assert elapsed < 4 * 0.3, "the backstop is per group, not per child"


def test_the_backstop_is_generous_and_operator_overridable(monkeypatch):
    """The old constant was five seconds and was reached by ordinary teardowns.
    The new one is a hang backstop, so it is generous, and an operator whose
    unwind legitimately needs longer can say so without patching the source."""
    assert _placement._TEARDOWN_GRACE >= 30
    monkeypatch.delenv("REVL_TEARDOWN_GRACE", raising=False)
    assert _placement._teardown_grace() == _placement._TEARDOWN_GRACE
    monkeypatch.setenv("REVL_TEARDOWN_GRACE", "120")
    assert _placement._teardown_grace() == 120.0
    # junk falls back rather than crashing a teardown on a typo
    monkeypatch.setenv("REVL_TEARDOWN_GRACE", "soon")
    assert _placement._teardown_grace() == _placement._TEARDOWN_GRACE
    monkeypatch.setenv("REVL_TEARDOWN_GRACE", "0")
    assert _placement._teardown_grace() == _placement._TEARDOWN_GRACE


def test_the_report_reuses_the_estop_vocabulary():
    """No new word for an old state: a child killed mid-unwind is in the same
    position as an E-Stopped session (item 443) -- entries registered, not run,
    not dropped -- so it borrows that verdict's language, including the pointer
    at `revl recover` that reconciles it."""
    text = _placement._stranded_teardown_report(["consumer"])
    assert "STRANDED" in text and "UNKNOWN" in text
    assert "G7" in text and "R4" in text
    assert "revl recover" in text
    assert "1 process had to be" in text
    assert "2 processes had to be" in _placement._stranded_teardown_report(["a", "b"])


@pytest.mark.parametrize("names", [["a"], ["a", "b"]])
def test_the_report_names_every_stranded_process(names):
    text = _placement._stranded_teardown_report(names)
    for name in names:
        assert name in text
