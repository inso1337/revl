"""Item 130 Slice 1 runtime proof (py): `Stream[T]` subscribe / next / close.

These are the design's core-guarantee exit tests (docs/design/130-stream-
reactive-types.md §10) executed end to end against cordis-py: the frontend
admits `subscribe … undo …` and the `next`/`close` calls, the emitter renders
the subscription BRACKET (`sub = Stream.subscribe(...)` then `yield lambda:
sub.close()`) and the awaited `next`, and the runtime tears the subscription
down LIFO with no residue — the core guarantee (§0) pinned rather than asserted.

The two review-critical fixes live here:

  * cancellation-first `next` (§9 Part A) — test 4: an owner withdrawn while a
    `next` is parked on a never-emitting provider CLOSES the stream (the bracket
    inverse runs) without deadlocking teardown behind the parked `next`.
  * provider death is a terminal, never silence (§9 Part B) — test 5: a provider
    that faults while a `next` is outstanding resolves it to `Faulted`, and the
    consumer's bracket closes.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys
import types

import pytest

from cordis import Context
from cordis.fiber import FiberState

import emit
import runtime as runtime_mod

# revl (the frontend) lives beside this backend; import it so the proof runs the
# real compile -> emit -> run pipeline rather than a hand-built IR.
_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl import compile_source  # noqa: E402


def _module(src: str, name: str) -> types.ModuleType:
    code = emit.emit(compile_source(src))
    module = types.ModuleType(name)
    exec(compile(code, f"{name}.py", "exec"), module.__dict__)
    return module


def _ops(events: list[str]) -> list[str]:
    return [re.sub(r"#\d+", "", event) for event in events]


async def _flush() -> None:
    for _ in range(60):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _reset_streams():
    runtime_mod.Stream.reset()
    yield
    runtime_mod.Stream.reset()


@pytest.fixture
def trace():
    events: list[str] = []
    runtime_mod.set_trace(events.append)
    yield events
    runtime_mod.set_trace(None)


# ---------------------------------------------------------------------------
# Exit test 1 — subscription roundtrip: close AFTER the last next, no residue
# ---------------------------------------------------------------------------

_ROUNDTRIP = """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  await sub.next()
  await sub.next()
}
"""


@pytest.mark.asyncio
async def test_subscription_roundtrip(trace):
    module = _module(_ROUNDTRIP, "stream_roundtrip")
    root = Context()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE
    src = runtime_mod.Stream.last_source()
    # deliver two items; both `next`s consume and the activation completes
    src.emit("a")
    src.emit("b")
    await _flush()
    assert c.state is FiberState.ACTIVE, "both items delivered, activation stays live"

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    # R1: the bracket inverse runs, and the subscription closes before the
    # source (LIFO), leaving no residue.
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.close" in ops and "stream.source close" in ops
    assert ops.index("stream.close") < ops.index("stream.source close"), \
        "the subscription closes before its source (LIFO)"
    assert runtime_mod.Stream.pending() == 0, "no dangling listener (no residue)"


# ---------------------------------------------------------------------------
# Exit test 2 — unload closes the stream, LIFO (the core guarantee pinned)
# ---------------------------------------------------------------------------

_LIFO = """
component C {
  let a = effect Pool.open("A", 1) undo a.close()
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  await sub.next()
}
"""


@pytest.mark.asyncio
async def test_unload_closes_the_stream_lifo(trace):
    """Owner acquires a sync resource A, then subscribes; withdraw the owner
    while an item is mid-flight. Assert `close` runs before A's inverse (LIFO),
    the listener is released, `no_residue` holds — the core guarantee §0."""
    module = _module(_LIFO, "stream_lifo")
    root = Context()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    c.dispose()  # withdraw while parked in next (no item ever delivered)
    await _flush()
    assert c.state is FiberState.DISPOSED

    order = [e for e in _ops(trace)
             if e in ("stream.close", "stream.source close", "pool.close A")]
    assert order == ["stream.close", "stream.source close", "pool.close A"], \
        "unloading the owner CLOSES the stream first, then the source, then A (LIFO)"
    assert runtime_mod.Stream.pending() == 0, "no residue: the listener is released"


# ---------------------------------------------------------------------------
# Exit test 4 — CRITICAL Part A: cancel reaches a parked `next`
# ---------------------------------------------------------------------------

_PARKED = """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  await sub.next()
}
"""


@pytest.mark.asyncio
async def test_cancel_reaches_a_parked_next(trace):
    """A consumer parked in `next` on a provider that never emits; withdraw the
    owner. Assert the park resolves (the owner state trips the cancel token),
    `close` runs, `no_residue` holds, and teardown does NOT deadlock behind the
    parked `next` (§9 Part A — the event-loop face of the CRITICAL)."""
    module = _module(_PARKED, "stream_parked")
    root = Context()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE
    assert runtime_mod.Stream.pending() == 2, "parked: source open + subscription live"

    c.dispose()
    # a bounded flush is the whole point: if the parked `next` deadlocked
    # teardown, the fiber would still be UNLOADING here.
    await _flush()
    assert c.state is FiberState.DISPOSED, "teardown completed (no deadlock)"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.close" in ops, "the bracket inverse ran off the teardown path"
    assert runtime_mod.Stream.pending() == 0, "no dangling / parked next (no residue)"


# ---------------------------------------------------------------------------
# Exit test 5 — CRITICAL Part B: provider death is a terminal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_death_is_a_terminal(trace):
    """A provider faults with an outstanding consumer `next`. Assert the `next`
    resolves to `Faulted` (never a silent pending), the consumer's activation
    fails, its bracket closes, and residue is empty (§9 Part B)."""
    module = _module(_PARKED, "stream_faulted")
    root = Context()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    src = runtime_mod.Stream.last_source()
    src.fault("boom")  # provider dies while a `next` is outstanding
    await _flush()

    assert c.state is FiberState.FAILED, "the outstanding `next` faulted the activation"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.source fault boom" in ops
    assert "stream.close" in ops, "the consumer's bracket closed on the terminal"
    assert runtime_mod.Stream.pending() == 0, "no residue after a provider terminal"


# ---------------------------------------------------------------------------
# Exit test 7 — backpressure `error` default: overflow -> Faulted, no silent loss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backpressure_error_default_faults_on_overflow():
    """The default `error` policy: a full bounded buffer yields a terminal
    `Faulted(overflow)` and closes; no silent loss (§4.4). Exercised directly
    against the reference Subscription with a small capacity."""
    runtime_mod.Stream.reset()
    source = runtime_mod.Stream.source()
    sub = runtime_mod.Subscription(source, "error", capacity=2)
    # fill the buffer, then one more delivery overflows it
    for item in ("i0", "i1", "i2"):
        source.emit(item)
    # the buffered prefix drains first (no silent loss), then the terminal
    assert await sub.next() == "i0"
    assert await sub.next() == "i1"
    with pytest.raises(runtime_mod.StreamFaulted) as excinfo:
        await sub.next()
    assert "overflow" in str(excinfo.value)
    # the terminal closes the subscription (no silent loss, no dangling listener)
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0
