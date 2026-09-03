"""The operator E-Stop across a PLACEMENT — roadmap item 443, issue #122.

Item 443 landed the halt on the py reference tier: a latch file, a crossing
seam that refuses once it is armed, and an in-flight inventory
(`tests/test_estop_443.py`). What it did not land is the conductor. A
composition split across processes had no operator halt at all — every stop
`run_placement` had was `_stop_all`, which asks each child to unwind and waits
on its own `DOWN` line, the child's statement that its LIFO walk covered every
registered entry (G7) and its no-residue proof printed (R4).

That is the graceful path, and an operator emergency is precisely the case
where it is the wrong one. So this suite pins the conductor half, and the
half that matters is the ACCOUNTING, not the stop:

  * the halt is prompt and it is NOT a teardown — no child is asked to unwind,
    no `DOWN` is waited for or earned;
  * the report NAMES every component left un-torn-down, one line each, with
    the process and tier it was on;
  * a component on a tier with no E-Stop seam is named as exactly that —
    killed outright, residue UNKNOWN — rather than being quietly folded into
    the group. Five of revl's six tiers are in that state today, and a halt
    that hid it would be reporting a stop it did not perform;
  * a latch-honoring child's own inventory (stranded entries, the at-most-one
    ambiguous crossing) is merged into the report by NAME;
  * a halt is never clean: the run's exit status says so;
  * an unarmed placement is byte-identical to the pre-443 conductor.

Everything drives the real `run_placement` with the stubbed-child pattern from
`tests/test_teardown_kill_reporting_239.py`. No runtime, no port.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
component Edge requires work: Work provides control: Control {
  provide control { async fn go() = work.compute("crossed") }
}
"""

# One py process and one node process: the mixed-tier placement is the whole
# point, because the two halves of the halt are different and the report has to
# say which one each component got.
PLACEMENT = """
[processes.provider]
components = ["HotWorker"]

[processes.edge]
backend = "node"
components = ["Edge"]
"""

PY_ONLY = """
[processes.provider]
components = ["HotWorker"]

[processes.edge]
components = ["Edge"]
"""


def _inventory(name: str) -> dict:
    """What the py runner's watcher prints when the latch trips it: the halt
    record's two books, in the merged residue schema (`_estop_record`)."""
    return {
        "process": name,
        "verdict": "halted",
        "reason": "runaway loop",
        "operator": "ops@example",
        "activations": [{"component": "HotWorker", "stranded": 1}],
        "inFlight": [{"kind": "estop-ambiguous", "state": "unresolved",
                      "component": "HotWorker", "method": "write", "seq": 7,
                      "entry": "crossing", "attemptedFlag": True,
                      "outcome": "unknown"}],
        "stranded": [{"kind": "estop-stranded", "state": "unresolved",
                      "component": "HotWorker", "method": "remove", "seq": 4,
                      "entry": "transactional", "attemptedFlag": False,
                      "outcome": "not-attempted"}],
        "resumable": False,
    }


class _StubProc:
    """A Popen stand-in that models one child's response to the latch.

    `honors_latch` is the tier distinction the report exists to carry. A py
    child watches the latch, halts itself, prints its inventory and dies where
    it stands (`os._exit`, no teardown). A child on any other tier never looks
    at the latch: it keeps running until something kills it.
    """

    def __init__(self, name: str, spec: dict, honors_latch: bool,
                 latch: str | None, silent: bool = False):
        self.name = name
        self.spec = spec
        self._lines = [f"[{name}] UP"]
        self._exited = False
        self.killed = False
        self.terminated = False
        self.stdin = self
        self.returncode = 0
        self._silent = silent
        self.written: list[str] = []
        if honors_latch and latch:
            threading.Thread(target=self._watch, args=(latch,),
                             daemon=True).start()

    def _watch(self, latch: str) -> None:
        while not self._exited:
            if os.path.exists(latch):
                if not self._silent:
                    self._lines.append(
                        f"[{self.name}] HALTED {json.dumps(_inventory(self.name))}")
                    # give the conductor's pump a moment to read the line off
                    # the pipe before the process disappears under it
                    time.sleep(0.05)
                self._exited = True
                return
            time.sleep(0.01)

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

    def write(self, text):
        # The control channel. The real `_process_runner` answers a `repoint`
        # with a `REPOINTED` line on its own stdout, and `do_swap` waits for
        # that acknowledgement before it cuts the seam over; a stub that stayed
        # mute would make every swap here stall for the full 30s timeout rather
        # than complete. Same shape as `tests/test_swap.py::_Stdin`.
        self.written.append(text)
        for line in text.splitlines():
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            if cmd.get("op") == "repoint":
                self._lines.append(
                    f"[{self.name}] REPOINTED {cmd['key']} -> {cmd['socket']}")

    def flush(self):
        pass

    def close(self):
        self.terminate()

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
        # The COOPERATIVE stop: unwind, prove no residue, say DOWN. An E-Stop
        # must never reach this, and one test asserts exactly that.
        self.terminated = True
        if not self._exited:
            self._lines.append(f"[{self.name}] DOWN")
            self._exited = True

    def kill(self):
        self.killed = True
        self._exited = True


def _run(tmp_path, monkeypatch, *, latch: str | None, placement: str = PLACEMENT,
         arm_after: float | None = 0.2, arm_body: str | None = None,
         silent: set[str] | None = None, live: float = 8.0,
         commands: list[str] | None = None, arm_when=None):
    """Boot the composition through the real conductor and, optionally, hit the
    button from `another terminal` while it is live."""
    silent = silent or set()
    procs: dict[str, _StubProc] = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        name = spec["name"]
        procs[name] = _StubProc(
            name, spec,
            honors_latch=name not in silent and spec.get("estopLatch") is not None,
            latch=latch, silent=name in silent)
        return procs[name]

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    # the node BUILD is not what is under test here; the halt is
    monkeypatch.setattr(_placement, "_emit_ts_module",
                        lambda ir, tmp: str(Path(tmp) / "mod.ts"))
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
    # a LIVE placement: the conductor parks in its REPL, as it does for a real
    # operator, so the halt has to interrupt it rather than be polled for
    monkeypatch.setattr(_placement, "_interactive", lambda: True)

    feed = iter(commands or [])

    def blocking_input(_prompt=""):
        # Scripted operator commands first (`swap ...`), then the REPL parks
        # exactly as it does for a real operator, so the halt still has to
        # interrupt a live conductor rather than be polled for.
        try:
            return next(feed)
        except StopIteration:
            pass
        deadline = time.monotonic() + live
        while time.monotonic() < deadline:
            time.sleep(0.02)
        raise EOFError

    monkeypatch.setattr(_placement, "input", blocking_input, raising=False)

    if latch and arm_after is not None:
        ready = arm_when or (lambda p: len(p) >= 2)

        def press_the_button() -> None:
            while not ready(dict(procs)):
                time.sleep(0.01)
            time.sleep(arm_after)
            Path(latch).write_text(
                arm_body if arm_body is not None else json.dumps({
                    "halted": True, "verdict": "halted",
                    "reason": "runaway loop", "operator": "ops@example",
                    "resumable": False}),
                encoding="utf-8")
        threading.Thread(target=press_the_button, daemon=True).start()

    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    plc = tmp_path / "p.toml"
    plc.write_text(placement, encoding="utf-8")
    started = time.monotonic()
    try:
        rc = _placement.run_placement([str(app)], str(plc), once=False,
                                      estop_latch=latch)
    except KeyboardInterrupt:  # pragma: no cover — the interrupt raced the return
        rc = 1
    return rc, procs, time.monotonic() - started


# ---------------------------------------------------------------------------
# THE test: a halt from a live placement, and what it is obliged to say
# ---------------------------------------------------------------------------


def test_an_estop_halts_a_live_placement_and_names_every_component(
        tmp_path, monkeypatch, capsys):
    """The headline. An operator hits the button on a mixed-tier placement
    that is up and parked, and the report has to name every component left
    un-torn-down — including the one on a tier that has no E-Stop at all."""
    latch = str(tmp_path / "halt.estop")
    rc, procs, elapsed = _run(tmp_path, monkeypatch, latch=latch, live=8.0)
    out = capsys.readouterr()

    # 1. it was PROMPT: the REPL would have parked for eight seconds
    assert elapsed < 5.0, f"the halt did not interrupt a live placement ({elapsed:.1f}s)"

    # 2. it is not a teardown. Nothing was asked to unwind and nothing earned
    #    a DOWN line, which is the G7/R4 proof a graceful stop ends with.
    assert not any(p.terminated for p in procs.values()), \
        "the E-Stop asked a child to unwind — that is the graceful path"
    assert "DOWN" not in out.out

    # 3. a halt is never clean
    assert rc != 0

    err = out.err
    assert "E-STOP ENGAGED" in err
    assert "runaway loop" in err and "ops@example" in err

    # 4. it says what it gave up, in the formal layer's own terms
    assert "VACUOUS" in err and "R4" in err

    # 5. EVERY component is named, with its process and tier
    assert "components left UN-TORN-DOWN" in err
    assert "HotWorker" in err and "process provider" in err and "tier py" in err
    assert "Edge" in err and "process edge" in err and "tier node" in err

    # 6. the tier with no seam is named as exactly that, not folded in
    assert "NO E-Stop seam" in err
    edge_line = [ln for ln in err.splitlines() if "NO E-Stop seam" in ln][0]
    assert "node" in edge_line
    assert "UNKNOWN" in edge_line

    # 7. the honoring child's own inventory is merged in BY NAME: the entry
    #    that was registered and never attempted, and the one crossing that
    #    was already out when the latch was read
    assert "estop-stranded" in err and "remove" in err
    assert "estop-ambiguous" in err and "write" in err
    assert "outcome unknown" in err

    # 8. and the un-nameable half is counted as un-nameable rather than omitted
    assert "of them UNKNOWN" in err
    assert "1 of them UNKNOWN" in err

    # 9. the way back
    assert "there is no resume" in err
    assert "revl recover --wal" in err


def test_the_halt_kills_the_seamless_tier_and_never_stops_it_gracefully(
        tmp_path, monkeypatch, capsys):
    """The only halt a tier without an E-Stop seam has is a kill, and the
    conductor must use it rather than politely asking. `terminate` here is the
    cooperative stop (`_stop_all`'s SIGTERM / stdin close); reaching it would
    mean the emergency stop had turned back into an unwind."""
    latch = str(tmp_path / "halt.estop")
    _rc, procs, _elapsed = _run(tmp_path, monkeypatch, latch=latch)
    capsys.readouterr()
    assert procs["edge"].killed is True
    assert procs["edge"].terminated is False
    # the py child halted itself and died where it stood; it needed no kill
    assert procs["provider"].terminated is False


def test_a_py_child_that_names_no_inventory_is_reported_unknown(
        tmp_path, monkeypatch, capsys):
    """A latch-honoring child that says nothing in the window is killed and
    reported UNKNOWN. Silence must never read as `nothing was owed` — that is
    the same mistake a kill mid-teardown made before issue 239."""
    monkeypatch.setenv("REVL_ESTOP_HALT_WINDOW", "0.3")
    latch = str(tmp_path / "halt.estop")
    rc, procs, _elapsed = _run(tmp_path, monkeypatch, latch=latch,
                               placement=PY_ONLY, silent={"provider"})
    err = capsys.readouterr().err
    assert rc != 0
    assert procs["provider"].killed is True
    assert "without naming an inventory" in err
    assert "HotWorker" in err
    assert "2 of them UNKNOWN" not in err  # only the silent one is unknown
    assert "1 of them UNKNOWN" in err


def test_a_malformed_latch_still_halts_the_placement(tmp_path, monkeypatch, capsys):
    """Failing open on an emergency stop is the one failure mode this feature
    exists to prevent, and the conductor applies the same rule as the runtime
    seam and the CLI."""
    latch = str(tmp_path / "halt.estop")
    rc, _procs, elapsed = _run(tmp_path, monkeypatch, latch=latch,
                               arm_body="{not json at all")
    err = capsys.readouterr().err
    assert rc != 0
    assert elapsed < 5.0
    assert "E-STOP ENGAGED" in err
    assert "unreadable latch" in err


# ---------------------------------------------------------------------------
# the control: nothing above may be bought by changing an ordinary run
# ---------------------------------------------------------------------------


def test_an_unarmed_placement_tears_down_gracefully_and_says_nothing(
        tmp_path, monkeypatch, capsys):
    """UNARMED is the default. With no latch there is no watcher, no latch
    read, and the cooperative teardown is exactly what it was."""
    rc, procs, _elapsed = _run(tmp_path, monkeypatch, latch=None, live=0.2)
    out = capsys.readouterr()
    assert rc == 0
    assert all(p.terminated for p in procs.values())
    assert not any(p.killed for p in procs.values())
    assert "[provider] DOWN" in out.out and "[edge] DOWN" in out.out
    assert "E-STOP" not in out.out and "E-STOP" not in out.err


def test_the_latch_is_handed_only_to_tiers_that_can_honor_it(
        tmp_path, monkeypatch, capsys):
    """The spec carries the latch to a py process — a sandboxed child (item
    411) need not inherit the conductor's environment, and an emergency stop a
    confined process cannot see is not one. A tier with no seam is not told a
    latch it would silently ignore; it is killed instead, and said to be."""
    latch = str(tmp_path / "halt.estop")
    _rc, procs, _elapsed = _run(tmp_path, monkeypatch, latch=latch)
    capsys.readouterr()
    assert procs["provider"].spec.get("estopLatch") == latch
    assert "estopLatch" not in procs["edge"].spec


# ---------------------------------------------------------------------------
# ... and it must still be handed to a process the OPERATOR created after boot.
#
# `revl swap` replaces a running provider with a synthesized successor whose
# spec `do_swap` builds fresh. Every key it forgot there has been a security
# property that held until the first swap and then silently stopped holding:
# the host-module pins (item 410) and the correlation guard (421 F8) both went
# that way. The latch is the same kind of key and the worst one to lose — a
# successor booted without it is un-haltable by the button that armed its
# predecessor, so an operator would press it, see a report, and still have a
# live process running.
# ---------------------------------------------------------------------------


def _swap_is_complete(procs: dict) -> bool:
    """The cutover is DONE: the successor exists and the old provider has
    finished its graceful unwind. The button is pressed after this so the
    question under test is `is the successor haltable`, not the separate (and
    racy) one of what a halt lands on mid-swap."""
    return (any(n.startswith("HotWorker__t") for n in procs)
            and "provider" in procs and procs["provider"].poll() is not None)


def test_a_swapped_successor_is_still_haltable_by_the_same_button(
        tmp_path, monkeypatch, capsys):
    """An operator swaps `HotWorker` onto a fresh process and THEN hits the
    button. The successor must halt at its own seams and be accounted for by
    name, and the report must be about the composition that is actually
    running rather than the one the placement file describes.

    Driven entirely through operator-facing output. The stub child honors the
    latch only when its SPEC carries it — which is precisely the population the
    spec key exists for, a sandboxed child (item 411) that never inherited the
    conductor's environment — so a successor that lost the key shows up here as
    a silent process killed after the window with residue UNKNOWN, not as a
    halted one naming its books.
    """
    latch = str(tmp_path / "halt.estop")
    rc, procs, elapsed = _run(
        tmp_path, monkeypatch, latch=latch,
        commands=["swap HotWorker --to py"],
        arm_when=_swap_is_complete, live=8.0)
    err = capsys.readouterr().err

    succ = next(n for n in procs if n.startswith("HotWorker__t"))

    # the halt still interrupts a live conductor, and a halt is never clean
    assert elapsed < 8.0, f"the halt did not interrupt the placement ({elapsed:.1f}s)"
    assert rc != 0
    assert "E-STOP ENGAGED" in err

    # 1. the successor HALTED at its own crossing seams. It was never asked to
    #    unwind, and it named its own in-flight books — which it can only do
    #    if it was ever told where the latch is.
    assert procs[succ].terminated is False, \
        "the E-Stop asked the successor to unwind — that is the graceful path"
    assert "HALTED at its own crossing seams" in err
    assert "without naming an inventory" not in err

    # 2. it is named as the process it actually is, with its component and tier
    assert f"process {succ}" in err
    assert "HotWorker" in err and "tier py" in err

    # 3. its residue is attributed to it BY NAME, not to the process the
    #    placement file happens to call the host of `HotWorker`.
    assert f"{succ}/HotWorker  estop-stranded" in err
    assert f"{succ}/HotWorker  estop-ambiguous" in err

    # 4. the predecessor is NOT in the report. It unwound with a no-residue
    #    proof on the cutover, before the button was ever pressed, so listing
    #    it would be inventing residue that nobody holds.
    assert "process provider" not in err
    assert "provider  UNKNOWN" not in err

    # 5. and the property the whole verb is for is unchanged by the swap: the
    #    seamless tier is still named as un-nameable, and counted.
    assert "Edge" in err and "process edge" in err and "NO E-Stop seam" in err
    assert "1 of them UNKNOWN" in err


# ---------------------------------------------------------------------------
# the runtime seam the conductor depends on
# ---------------------------------------------------------------------------


def test_an_idle_process_halts_on_the_button_not_on_its_next_crossing(tmp_path):
    """`_estop_check` engages the latch lazily, at the next crossing, which is
    right for a busy process and useless for an idle one: a process parked
    waiting to be stopped crosses nothing and would sit through the emergency.
    `estop_from_latch` is what the watcher calls to close that gap."""
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import runtime as rt  # noqa: PLC0415

    rt.clear_estop()
    rt.arm_estop_latch(None)
    try:
        latch = tmp_path / "halt.estop"
        rt.arm_estop_latch(str(latch))
        assert rt.estop_from_latch() is None  # unarmed: nothing happens
        assert rt.estop_engaged() is False

        latch.write_text(json.dumps({"reason": "runaway loop",
                                     "operator": "ops@example"}),
                         encoding="utf-8")
        record = rt.estop_from_latch()
        assert record is not None
        assert record["verdict"] == "halted"
        assert record["reason"] == "runaway loop"
        assert record["operator"] == "ops@example"
        assert record["resumable"] is False
        # idempotent: the button twice is not two halts
        assert rt.estop_from_latch()["at"] == record["at"]
    finally:
        rt.clear_estop()
        rt.arm_estop_latch(None)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
