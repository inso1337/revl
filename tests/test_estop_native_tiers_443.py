"""The operator E-Stop across the go and rust placement tiers — roadmap item
443, issue #122.

Item 443 landed the halt on the py reference tier and the conductor half
(`tests/test_estop_conductor_443.py`). The five non-py tiers kept their
cooperative teardown and had no E-Stop, so under a placement halt they were
SIGKILLed and reported residue UNKNOWN per component.

This suite pins the conductor honoring the go and rust tiers, which now read
the latch in their placement runners (`backends/go/placement_runner`,
`backends/rust/placement_runner`): the conductor hands them the latch in the
spec, gives them the bounded inventory window rather than an immediate kill,
and merges their inventory into the halt report by name. A tier that still has
no seam (here `java`, `wasm`) is killed and reported UNKNOWN, exactly as before.

The per-tier runtime behavior (the latch reader, the accept-seam refusal, the
in-flight registry, the idle watcher) is pinned by the native suites
(`backends/go/placement_runner/estop/estop_test.go`,
`backends/rust/placement_runner/src/estop.rs`). This suite is disjoint from
`tests/test_estop_conductor_443.py` (which #598 edits for the node tier) and
drives the real `run_placement` with the stubbed-child pattern, so it needs no
go/rust toolchain.
"""

from __future__ import annotations

import json
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


def _inventory(name: str) -> dict:
    """What a go/rust runner's idle watcher prints when the latch trips: the
    in-flight crossings shaped into the merged residue schema
    (`estop.EstopInventory` / `estop::estop_inventory`). This tier keeps no
    witnessed-inverse ledger, so `stranded` is honestly empty and the one
    crossing that was in flight is the ambiguous one (item 440)."""
    return {
        "process": name,
        "verdict": "halted",
        "reason": "runaway loop",
        "operator": "ops@example",
        "activations": [],
        "inFlight": [{"kind": "estop-ambiguous", "state": "unresolved",
                      "component": "work", "method": "compute", "seq": 1,
                      "entry": "crossing", "direction": "accept",
                      "attemptedFlag": True, "outcome": "unknown"}],
        "stranded": [],
        "resumable": False,
    }


class _StubProc:
    """A Popen stand-in that models one child's response to the latch. A child
    on a latch-honoring tier (whose spec carries `estopLatch`) watches the
    latch, prints its inventory and dies where it stands; a seamless-tier child
    never looks at the latch and runs until it is killed."""

    def __init__(self, name: str, spec: dict, honors_latch: bool, latch: str | None):
        self.name = name
        self.spec = spec
        self._lines = [f"[{name}] UP"]
        self._exited = False
        self.killed = False
        self.terminated = False
        self.stdin = self
        self.returncode = 0
        if honors_latch and latch:
            threading.Thread(target=self._watch, args=(latch,), daemon=True).start()

    def _watch(self, latch: str) -> None:
        while not self._exited:
            if Path(latch).exists():
                self._lines.append(
                    f"[{self.name}] HALTED {json.dumps(_inventory(self.name))}")
                time.sleep(0.05)  # let the pump read the line before we vanish
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
        pass

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
        self.terminated = True
        if not self._exited:
            self._lines.append(f"[{self.name}] DOWN")
            self._exited = True

    def kill(self):
        self.killed = True
        self._exited = True


def _run(tmp_path, monkeypatch, placement: str, *, latch: str | None, live: float = 8.0):
    procs: dict[str, _StubProc] = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        name = spec["name"]
        procs[name] = _StubProc(name, spec,
                                honors_latch=spec.get("estopLatch") is not None,
                                latch=latch)
        return procs[name]

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    # the toolchain BUILDS are not what is under test here; the halt is.
    monkeypatch.setattr(_placement, "_emit_ts_module", lambda ir, tmp: str(Path(tmp) / "mod.ts"))
    monkeypatch.setattr(_placement, "_build_go", lambda ir, tmp: str(Path(tmp) / "go-bin"))
    monkeypatch.setattr(_placement, "_build_rust", lambda ir, tmp: str(Path(tmp) / "rust-bin"))
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_placement, "_interactive", lambda: True)

    def blocking_input(_prompt=""):
        deadline = time.monotonic() + live
        while time.monotonic() < deadline:
            time.sleep(0.02)
        raise EOFError

    monkeypatch.setattr(_placement, "input", blocking_input, raising=False)

    if latch:
        def press_the_button() -> None:
            while len(procs) < 2:
                time.sleep(0.01)
            time.sleep(0.2)
            Path(latch).write_text(json.dumps({
                "halted": True, "verdict": "halted", "reason": "runaway loop",
                "operator": "ops@example", "resumable": False}), encoding="utf-8")
        threading.Thread(target=press_the_button, daemon=True).start()

    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    plc = tmp_path / "p.toml"
    plc.write_text(placement, encoding="utf-8")
    started = time.monotonic()
    try:
        rc = _placement.run_placement([str(app)], str(plc), once=False, estop_latch=latch)
    except KeyboardInterrupt:  # pragma: no cover — the interrupt raced the return
        rc = 1
    return rc, procs, time.monotonic() - started


@pytest.mark.parametrize("backend", ["go", "rust"])
def test_the_latch_is_handed_to_the_native_tier(backend, tmp_path, monkeypatch, capsys):
    """A go/rust process is in `TIERS_WITH_ESTOP`, so the conductor carries the
    latch to it in the spec — a sandboxed child (item 411) need not inherit the
    conductor's environment, and an emergency stop a confined process cannot see
    is not one."""
    placement = (f"[processes.provider]\ncomponents = [\"HotWorker\"]\n\n"
                 f"[processes.edge]\nbackend = \"{backend}\"\ncomponents = [\"Edge\"]\n")
    latch = str(tmp_path / "halt.estop")
    _rc, procs, _elapsed = _run(tmp_path, monkeypatch, placement, latch=latch)
    capsys.readouterr()
    assert procs["provider"].spec.get("estopLatch") == latch
    assert procs["edge"].spec.get("estopLatch") == latch


@pytest.mark.parametrize("backend", ["go", "rust"])
def test_a_native_tier_halts_at_its_seams_and_is_not_torn_down(
        backend, tmp_path, monkeypatch, capsys):
    """The headline: a go/rust child honors the latch. It HALTS at its own
    crossing seams (it is never asked to unwind and earns no `DOWN`), names its
    in-flight inventory, and the report merges that inventory by name."""
    placement = (f"[processes.provider]\ncomponents = [\"HotWorker\"]\n\n"
                 f"[processes.edge]\nbackend = \"{backend}\"\ncomponents = [\"Edge\"]\n")
    latch = str(tmp_path / "halt.estop")
    rc, procs, elapsed = _run(tmp_path, monkeypatch, placement, latch=latch, live=8.0)
    out = capsys.readouterr()

    assert elapsed < 5.0, f"the halt did not interrupt a live placement ({elapsed:.1f}s)"
    assert rc != 0
    # not a teardown: nothing was asked to unwind, nothing earned a DOWN line
    assert not any(p.terminated for p in procs.values())
    assert "DOWN" not in out.out

    err = out.err
    assert "E-STOP ENGAGED" in err
    # the edge process, on the native tier, halted at its own seams (not killed
    # for want of one) and is NOT reported as a seamless kill
    assert "HALTED at its own crossing seams" in err
    assert f"tier {backend}" in err
    # its inventory is merged in by name: the one crossing that was in flight
    assert "estop-ambiguous" in err and "outcome unknown" in err
    # and the native tier is NOT counted among the un-nameable UNKNOWNs
    assert "NO E-Stop seam" not in err
    assert "0 of them UNKNOWN" in err


def test_a_still_seamless_tier_is_killed_while_the_native_one_halts(
        tmp_path, monkeypatch, capsys):
    """A mixed placement: a rust process honors the latch, a java process (no
    seam yet) is killed and reported UNKNOWN. The report must tell them apart."""
    placement = ("[processes.rustproc]\nbackend = \"rust\"\ncomponents = [\"HotWorker\"]\n\n"
                 "[processes.javaproc]\nbackend = \"java\"\ncomponents = [\"Edge\"]\n")
    latch = str(tmp_path / "halt.estop")
    # the java build is stubbed so the placement never needs a JVM
    monkeypatch.setattr(_placement, "_build_java", lambda ir, tmp: str(Path(tmp) / "java-out"),
                        raising=False)
    monkeypatch.setattr(_placement, "_find_jdk21", lambda: None, raising=False)
    monkeypatch.setattr(_placement, "_find_cordis4j_classes", lambda: None, raising=False)
    rc, procs, _elapsed = _run(tmp_path, monkeypatch, placement, latch=latch)
    err = capsys.readouterr().err

    assert rc != 0
    # the rust child halted itself; the java child was killed for want of a seam
    assert procs["rustproc"].killed is False or procs["rustproc"].terminated is False
    assert procs["javaproc"].killed is True
    assert procs["javaproc"].terminated is False
    assert "HALTED at its own crossing seams" in err
    assert "NO E-Stop seam" in err
    java_line = [ln for ln in err.splitlines() if "NO E-Stop seam" in ln][0]
    assert "java" in java_line
    assert "1 of them UNKNOWN" in err


def test_an_unarmed_native_placement_tears_down_gracefully(tmp_path, monkeypatch, capsys):
    """UNARMED is the default: with no latch there is no watcher and the go/rust
    processes tear down cooperatively, byte-identically to the pre-443 run."""
    placement = ("[processes.provider]\ncomponents = [\"HotWorker\"]\n\n"
                 "[processes.edge]\nbackend = \"go\"\ncomponents = [\"Edge\"]\n")
    rc, procs, _elapsed = _run(tmp_path, monkeypatch, placement, latch=None, live=0.2)
    out = capsys.readouterr()
    assert rc == 0
    assert all(p.terminated for p in procs.values())
    assert not any(p.killed for p in procs.values())
    assert "E-STOP" not in out.out and "E-STOP" not in out.err


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
