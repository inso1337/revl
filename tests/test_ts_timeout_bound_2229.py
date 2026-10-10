"""The ts tier's wall-clock bound is raisable, and says so when it fires (#2229).

`revl test --backend ts` runs each generated module under `vitest run <file>`
with a hardcoded 180s bound (`src/revl/test.py::run_ts`), and re-runs it under
plain node with the same bound (`::_ts_runtime_contract`). The deadline is on a
process whose runtime is a property of the HOST, so on a loaded machine a run
that is still working is killed and reported as a failure: measured at a
15-minute load average around 12, two compositions reported
`subprocess.TimeoutExpired ... timed out after 180 seconds`, and both passed on a
quiet host.

Three things have to hold, and this file pins all three:

(a) `REVL_TS_TIMEOUT` (seconds) moves the bound at BOTH sites;
(b) a timeout on the vitest path is a NAMED failure that names the composition,
    the bound and the variable — not a raw `TimeoutExpired` traceback;
(c) with nothing exported, both bounds are exactly what they were (180s outer,
    60s inner in `backends/typescript/vitest.config.ts`).

The inner bound lives in a `.ts` file, so (c) is pinned by reading the
derivation out of it and checking the arithmetic — 180s/3 is exactly 60000ms.
That the exported variable really crosses into that file's `process.env` is
proven by a real invocation (recorded on #2229), not by this file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import test as T  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

EXAMPLE = ROOT / "examples" / "outcome.rvl"
CONFIG = ROOT / "backends" / "typescript" / "vitest.config.ts"


class _World:
    """The fake process world the test swaps into `revl.test`."""

    calls: "list[_FakeProc]" = []
    hits_the_bound: bool = False


class _FakeProc:
    """A `subprocess.Popen` stand-in: records argv and kwargs, and either exits
    0 with empty output or hits the bound the way a real child would."""

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = 4242  # never signalled: `os.killpg` is stubbed below
        self.returncode = 0
        self._timed_out = False
        _World.calls.append(self)

    def communicate(self, timeout=None):
        self.bound = timeout
        if self._timed_out:
            return "", ""
        if _World.hits_the_bound:
            self._timed_out = True
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return "", ""

    def kill(self):  # the fallback `_ts_foreground` takes when killpg fails
        self.killed_directly = True


class _Subprocess:
    """`revl.test`'s `subprocess` for the duration of a test: both the
    `Popen` shape (the runner's own helper) and the `run` shape (the literal
    the base revision used) are answered by the same fake, so the bound is
    observable whichever one the code under test reaches for."""

    TimeoutExpired = subprocess.TimeoutExpired
    CompletedProcess = subprocess.CompletedProcess
    PIPE = subprocess.PIPE

    @staticmethod
    def Popen(argv, **kwargs):
        return _FakeProc(argv, **kwargs)

    @staticmethod
    def run(argv, **kwargs):
        proc = _FakeProc(argv, **kwargs)
        out, err = proc.communicate(timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)


@pytest.fixture(autouse=True)
def _fake_world(monkeypatch):
    _World.calls = []
    _World.hits_the_bound = False
    monkeypatch.setattr(T, "subprocess", _Subprocess)
    monkeypatch.setattr(os, "killpg", lambda *a, **k: None)
    yield
    _World.calls = []
    _World.hits_the_bound = False


def _bound_of(argv_fragment: str) -> int:
    """The `timeout=` the runner handed the child whose argv contains
    `argv_fragment` — the one place both sites' bound is observable."""
    matched = [call for call in _World.calls
               if any(argv_fragment in str(a) for a in call.argv)]
    assert len(matched) == 1, (
        f"expected exactly one child matching {argv_fragment!r}, got "
        f"{[c.argv for c in _World.calls]}")
    timeout = getattr(matched[0], "bound", None)
    assert isinstance(timeout, int), f"no bound was handed to {argv_fragment!r}"
    return timeout


def _drive_vitest_site(monkeypatch):
    """`run_ts` down to the vitest call, with the emitter real and everything
    that leaves the process faked."""
    monkeypatch.setattr(T, "vitest_command", lambda: ["vitest"])
    ir = compile_files([str(EXAMPLE)])
    return T._RAW_RUNNERS["ts"](ir), ir


def _drive_node_site(monkeypatch):
    """`_ts_runtime_contract` down to its plain-node call. `_node_version` is
    stubbed so the call happens on a machine with no node at all: this is a
    check on the bound, not on the toolchain."""
    monkeypatch.setattr(T, "_node_version", lambda: (26, 0))
    return T._ts_runtime_contract(ROOT / "backends" / "typescript" / "x.test.ts")


# --------------------------------------------------------------- (a) and (c)

def test_default_bound_is_todays_180s_at_both_sites(monkeypatch):
    """(c): nothing exported -> the outer bound is exactly 180 at BOTH sites."""
    monkeypatch.delenv("REVL_TS_TIMEOUT", raising=False)
    assert T._ts_timeout_seconds() == 180
    _drive_vitest_site(monkeypatch)
    _drive_node_site(monkeypatch)
    assert _bound_of("vitest") == 180
    assert _bound_of("node-tier-runner.mjs") == 180


def test_env_var_raises_the_outer_bound_at_both_sites(monkeypatch):
    """(a): an exported REVL_TS_TIMEOUT is what the child is given."""
    monkeypatch.setenv("REVL_TS_TIMEOUT", "900")
    assert T._ts_timeout_seconds() == 900
    _drive_vitest_site(monkeypatch)
    _drive_node_site(monkeypatch)
    assert _bound_of("vitest") == 900
    assert _bound_of("node-tier-runner.mjs") == 900


# --------------------------------------------------------------------- (b)

def test_vitest_timeout_is_a_named_failure_naming_the_composition(monkeypatch):
    """(b): the vitest path returns a named verdict instead of letting
    `TimeoutExpired` escape, and the verdict says which composition ran out of
    time, what the bound was, and which variable moves it."""
    monkeypatch.setenv("REVL_TS_TIMEOUT", "900")
    _World.hits_the_bound = True
    (verdict, message), _ir = _drive_vitest_site(monkeypatch)
    assert verdict == "fail", message
    assert EXAMPLE.name in message, message
    assert "900s" in message, message
    assert "REVL_TS_TIMEOUT" in message, message
    assert "ran out of time" in message, message
    # the generated module is still cleaned up on the timeout path
    assert not (ROOT / "backends" / "typescript" / "tests" / "generated"
                / f"revl_test_{os.getpid()}.test.ts").exists()


def test_a_timed_out_run_has_its_process_group_killed(monkeypatch):
    """The orphan: a killed `vitest run` left its fork worker at PPID 1, 37%
    CPU, for ~7 minutes (its workspace already deleted). The child is put in
    its own session and the whole group goes down with it."""
    monkeypatch.setenv("REVL_TS_TIMEOUT", "900")
    _World.hits_the_bound = True
    killed = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    _drive_vitest_site(monkeypatch)
    vitest = [c for c in _World.calls if "vitest" in str(c.argv)]
    assert vitest, "the vitest call never happened"
    assert vitest[0].kwargs.get("start_new_session") is True, (
        "without its own session the group cannot be killed")
    assert killed == [(vitest[0].pid, 9)], killed


# ------------------------------------------------------- (c), the inner bound

def test_inner_vitest_bound_is_a_third_of_the_same_variable():
    """(c): vitest's own bound derives from the same variable, and by default
    is exactly the 60s it has always been."""
    text = CONFIG.read_text(encoding="utf-8")
    read = re.search(r"Number\(\s*process\.env\.REVL_TS_TIMEOUT\s*\|\|\s*(\d+)\s*\)",
                     text)
    assert read, "vitest.config.ts must read REVL_TS_TIMEOUT from process.env"
    default = int(read.group(1))
    assert default == T._TS_TIMEOUT_DEFAULT, (
        "the two defaults are one number, not two")
    ratio = re.search(r"const TS_TIMEOUT_MS = \(TS_TIMEOUT_S \* (\d+)\) / (\d+)",
                      text)
    assert ratio, "vitest.config.ts must derive its ms bound from TS_TIMEOUT_S"
    assert (int(ratio.group(1)), int(ratio.group(2))) == (1000, 3)
    for key in ("testTimeout", "hookTimeout"):
        assert re.search(rf"{key}: TS_TIMEOUT_MS", text), (
            f"{key} must be the derived bound, not a literal")
    # today's value, exactly, and the ratio stays exact at a raised bound
    assert (default * 1000) / 3 == 60_000
    assert (default * 1000) % 3 == 0
    # and the inner bound stays strictly below the outer at any exported value
    assert (900 * 1000) / 3 == 300_000
    assert 300 < 900, "vitest must report first, the outer bound is the backstop"
