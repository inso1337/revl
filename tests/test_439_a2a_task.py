"""Item 439 slice T0 + the T1 pure-revl vocabulary (issue #118).

`docs/design/439-a2a-task-lifecycle.md`. Two things land here, the first
implementable slice for #118:

  * **T0** — the activation runtime maps a declared `TransportFault` raised by
    a synthesized remote provider's crossing to provider WITHDRAWAL (decision
    4). Until this slice the fault only unwound the calling fiber; now it
    withdraws the provider, so its consumers deactivate reactively (R2/R3),
    exactly as peer death does. Under `on_failure(result)` no fault is raised
    (the failure is the `Err`), so the provider stays wired.

  * **T1 vocabulary** — `stdlib/a2a.rvl`: the pure-revl Task-lifecycle types
    (`TaskRef`, `TaskState`, `TaskEvent`) and the total `is_terminal` /
    `task_state_from_wire` gates the four-op projection speaks. Pure revl, so
    it lowers on every tier and carries no host surface of its own.

This file pins the parts testable without a live cordis runtime — the declared
type, the cause taxonomy, the synthesized body that raises the typed fault, and
the driver seam that maps it — the same honesty layering test_477_liveness_*.py
follows for the liveness-expiry complement.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import textwrap
import types
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, why_runtime as wr  # noqa: E402
from revl.synthesize import _py_body, _py_body_a2a, _pascal  # noqa: E402

STDLIB = ROOT / "stdlib" / "a2a.rvl"


# ============================================================ T1: the vocabulary

def _compile_with_a2a(main_src: str):
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "stdlib").mkdir()
    (d / "stdlib" / "a2a.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    (d / "main.rvl").write_text(main_src, encoding="utf-8")
    import os
    cwd = os.getcwd()
    os.chdir(d)
    try:
        return compile_files(["main.rvl"])
    finally:
        os.chdir(cwd)


CONSUMER = """\
use "stdlib/a2a.rvl" { TaskState, is_terminal, task_state_from_wire }

fn stop(w: Str) -> Bool { return is_terminal(task_state_from_wire(w)) }
"""


def test_stdlib_a2a_compiles_and_is_pure_revl():
    """The module is pure revl (no `@py`/`@ts`), so it lowers on every tier."""
    src = STDLIB.read_text(encoding="utf-8")
    assert "= @py" not in src and "= @ts" not in src
    assert "extern" not in src
    doc = _compile_with_a2a(CONSUMER)
    assert doc is not None


def _exec_py(ir: dict) -> dict:
    """Emit the py backend for `ir` and exec it, returning the module namespace
    — the technique test_list_transforms.py uses to run a pure-revl stdlib fn.
    """
    spec = importlib.util.spec_from_file_location(
        "pyemit_a2a_task", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        ns: dict = {}
        exec(compile(module.emit(ir), "a2a_task.py", "exec"), ns)
    finally:
        if previous is not None:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return ns


def test_is_terminal_and_state_from_wire_execute_on_py():
    """The two total gates run: terminal states are terminal, live states are
    not, an unrecognised wire state is `Unknown` (surfaced, not guessed), and
    `Unknown` is NOT terminal."""
    ns = _exec_py(_compile_with_a2a(CONSUMER))
    stop = ns["stop"]
    # terminal states
    for w in ("completed", "failed", "canceled", "rejected"):
        assert stop(w) is True, w
    # live states
    for w in ("submitted", "working", "input-required", "auth-required"):
        assert stop(w) is False, w
    # an unrecognised wire state is Unknown, which is not terminal
    assert stop("nonsense-state") is False


# ============================================================ T0: the declared type

def test_runtime_transport_fault_is_the_declared_type():
    """The runtime ships `TransportFault` as the declared type, subclassing
    `RuntimeError` (so a `RuntimeError` catch still sees it) and carrying the
    duck-typed marker plus the row/crossing accounting the withdrawal reads."""
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import runtime  # noqa: PLC0415
    fault = runtime.TransportFault("x", row="agent", crossing="ask")
    assert isinstance(fault, RuntimeError)
    assert getattr(fault, "_revl_transport_fault", False) is True
    assert fault._revl_row == "agent"
    assert fault._revl_crossing == "ask"
    assert "TransportFault" in runtime.__all__


def test_cause_transport_fault_is_a_distinct_kind():
    """The withdrawal root is distinct in KIND from a fault, an operator
    trigger, and a silence expiry, and carries the row + crossing and NO
    diagnostic code (the fault is the peer's, not a classifiable RevlError)."""
    tf = wr.cause_transport_fault("agent", "ask")
    assert tf["kind"] == wr.TRANSPORT_FAULT
    assert tf["kind"] != wr.cause_trigger("x", code="R2")["kind"]
    assert tf["kind"] != wr.cause_liveness_expired(1000, 1500)["kind"]
    assert tf["kind"] != wr.cause_provider_withdrawn("P", "k")["kind"]
    assert tf["row"] == "agent" and tf["crossing"] == "ask"
    assert "code" not in tf


# ============================================ T0: the synthesized body raises it

def _run_withdraw_body(*, a2a: bool, label: str, op: str):
    """Execute a synthesized `on_failure(withdraw)` `@py` body against a
    transport that fails, and return the exception it raised. Mirrors
    test_439's `_run_row_body`, but keeps the raised exception so the marker,
    row and crossing the runtime keys on can be asserted directly."""
    if a2a:
        body = _py_body_a2a("agent.example:8443", op, in_band=False, label=label)
    else:
        body = _py_body("agent.example:8443", "agent", op, in_band=False,
                        label=label)
    src = "def _crossing(_arg):\n    _args = [_arg]\n" + textwrap.indent(
        textwrap.dedent(body), "    ")

    class _Opener:
        def open(self, request, *a, **k):
            raise urllib.request.HTTPError(
                request.full_url, 503, "err", {}, io.BytesIO(b""))

    ns = {"__name__": "generated"}
    exec(compile(src, "<withdraw-body>", "exec"), ns)
    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *h: _Opener()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            ns["_crossing"]("ping")
    finally:
        urllib.request.build_opener = original
    return excinfo.value


@pytest.mark.parametrize("a2a", [True, False])
def test_withdraw_body_raises_a_marked_transport_fault(a2a):
    """Both wires (A2A `message/send` and the canonical envelope): a transport
    failure raises a `TransportFault` carrying the marker the activation runtime
    keys on and the row label + crossing it withdraws by."""
    fault = _run_withdraw_body(a2a=a2a, label="agent", op="ask")
    assert getattr(fault, "_revl_transport_fault", False) is True
    assert fault._revl_row == "agent"
    assert fault._revl_crossing == "ask"


# ============================================ T0: the driver seam maps it

class _FakeFiber:
    def __init__(self, state, error=None):
        self.state = state
        self._error = error


def _fake_driver(fibers):
    """A minimal stand-in carrying just what `_withdraw_transport_faulted`
    touches: the fibers map, a `FiberState` that yields a `.name`, the static
    fault detector, and a recorder in place of the real withdrawal."""
    from revl.run import _Driver  # noqa: PLC0415
    calls: list[tuple[str, str, str]] = []

    async def _record(provider, row, crossing):
        calls.append((provider, row, crossing))

    fake = types.SimpleNamespace(
        fibers=fibers,
        FiberState=lambda s: types.SimpleNamespace(name=s),
        _transport_fault=_Driver._transport_fault,
        _perform_transport_fault_withdrawal=_record,
    )
    return _Driver._withdraw_transport_faulted, fake, calls


def test_driver_seam_withdraws_the_provider_named_by_the_fault():
    """A FAILED consumer whose error is a transport fault naming row `agent`
    withdraws the live provider `RemoteAgentProvider` (the `Remote<Pascal(row)>
    Provider` the synthesizer named)."""
    import asyncio
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import runtime  # noqa: PLC0415
    err = runtime.TransportFault("boom", row="agent", crossing="ask")
    fibers = {
        f"Remote{_pascal('agent')}Provider": _FakeFiber("ACTIVE"),
        "Consumer": _FakeFiber("FAILED", error=err),
    }
    fn, fake, calls = _fake_driver(fibers)
    asyncio.run(fn(fake))
    assert calls == [("RemoteAgentProvider", "agent", "ask")]


def test_driver_seam_is_inert_without_a_transport_fault():
    """A run with no transport fault is byte-identical: a FAILED fiber with an
    ORDINARY error, or a healthy provider, withdraws nothing."""
    import asyncio
    fibers = {
        "RemoteAgentProvider": _FakeFiber("ACTIVE"),
        "Consumer": _FakeFiber("FAILED", error=RuntimeError("unrelated")),
    }
    fn, fake, calls = _fake_driver(fibers)
    asyncio.run(fn(fake))
    assert calls == []


def test_driver_seam_skips_a_provider_that_is_not_live():
    """Fail-safe: if the named provider is already gone (not ACTIVE), the seam
    does not try to withdraw it a second time."""
    import asyncio
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import runtime  # noqa: PLC0415
    err = runtime.TransportFault("boom", row="agent", crossing="ask")
    fibers = {
        "RemoteAgentProvider": _FakeFiber("DISPOSED"),
        "Consumer": _FakeFiber("FAILED", error=err),
    }
    fn, fake, calls = _fake_driver(fibers)
    asyncio.run(fn(fake))
    assert calls == []
