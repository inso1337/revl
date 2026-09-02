"""Item 130 runtime proof (py): `Stream[T]` subscribe / next / close, plus the
Slice 2 combinators, backpressure policies and clock-driven drain window.

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

Slice 2 re-proves both THROUGH a `map`/`filter`/`take` chain, because a chain is
exactly where they could regress: a link that swallowed a terminal, or one that
held a park of its own, would reintroduce the parked-forever path the core
guarantee forbids. It also pins each declared backpressure policy (no silent
loss on any of them) and the `block` drain window firing on `Clock.advance`.
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
    runtime_mod.Clock.reset()
    yield
    runtime_mod.Stream.reset()
    runtime_mod.Clock.reset()


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


# ===========================================================================
# Slice 2 — pure combinators, the declared policies, the deterministic clock
# ===========================================================================
#
# The two Slice 1 review-critical fixes are re-proven THROUGH a combinator
# chain here, because a chain is exactly where they could regress: a link that
# swallowed a terminal, or one that held its own park, would reintroduce the
# parked-forever path the core guarantee forbids.


def _chain(source, *stages, policy="error", capacity=8, drain_ms=None, ctx=None):
    return runtime_mod.Stream.subscribe(
        source, policy, ctx, stages=list(stages), capacity=capacity,
        drain_ms=drain_ms)


# ---------------------------------------------------------------------------
# The combinators themselves (§1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_map_filter_take_transform_the_stream_in_order():
    source = runtime_mod.Stream.source()
    sub = _chain(source,
                 ("map", lambda x: x * 2),
                 ("filter", lambda x: x > 2),
                 ("take", 2))
    for item in (1, 2, 3, 4):        # -> 2, 4, 6, 8 -> 4, 6, 8 -> take 4, 6
        source.emit(item)
    assert await sub.next() == 4
    assert await sub.next() == 6
    # `take(2)` is spent: the derived stream ends with a `Closed` TERMINAL, so
    # the consumer's next `next` resolves rather than parking forever.
    assert await sub.next() is runtime_mod.STREAM_CLOSED
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_take_detaches_from_the_provider_when_it_is_spent():
    """A spent `take` stops consuming from the provider: the link detaches, so
    a later emit reaches nothing, and the consumer sees the `Closed` terminal
    the link pushed — not a park and not a silently buffered extra item."""
    source = runtime_mod.Stream.source()
    sub = _chain(source, ("take", 1))
    stage = runtime_mod.Stream.stages()[0]
    assert stage in source._subs
    assert source.emit("a") is True
    assert stage not in source._subs, "the spent link detached from the provider"
    source.emit("b")
    assert await sub.next() == "a"
    assert await sub.next() is runtime_mod.STREAM_CLOSED, \
        "the terminal the spent link pushed, not the extra item"
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0


# ---------------------------------------------------------------------------
# The core guarantee THROUGH a chain (§0, exit test §10.2)
# ---------------------------------------------------------------------------

_CHAIN_LIFO = """
component C {
  let a = effect Pool.open("A", 1) undo a.close()
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.map(x => x * 2).filter(x => x > 100) undo sub.close()
  await sub.next()
}
"""


@pytest.mark.asyncio
async def test_unload_closes_the_whole_chain_lifo(trace):
    """The core guarantee survives derivation: unloading the owner runs the ONE
    bracket the `subscribe` registered, and that inverse unwinds every link of
    the chain before the provider's own inverse and before A's (LIFO). No
    orphaned derived stream — `Stream.pending()` counts live links."""
    module = _module(_CHAIN_LIFO, "stream_chain_lifo")
    root = Context()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE
    # source + subscription + 2 chain links are all live
    assert runtime_mod.Stream.pending() == 4

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED

    order = [e for e in _ops(trace)
             if e.startswith("stream.close") or e.startswith("stream.stage close")
             or e == "stream.source close" or e == "pool.close A"]
    assert order == ["stream.close",
                     "stream.stage close filter",
                     "stream.stage close map",
                     "stream.source close",
                     "pool.close A"], \
        "one bracket inverse unwinds the chain downstream-first, then the " \
        "provider, then A (LIFO)"
    assert runtime_mod.Stream.pending() == 0, "no residue anywhere in the chain"


# ---------------------------------------------------------------------------
# CRITICAL Part A through a chain: cancel reaches a `next` parked BEHIND a filter
# ---------------------------------------------------------------------------

_CHAIN_PARKED = """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.map(x => x * 2).filter(x => x > 100) undo sub.close()
  await sub.next()
}
"""


@pytest.mark.asyncio
async def test_cancel_reaches_a_next_parked_behind_a_filter(trace):
    """The nastiest Slice 2 shape for §9 Part A: the provider IS emitting, but a
    `filter` rejects every item, so the consumer is parked exactly as if the
    provider were dead. Withdrawing the owner must still resolve the park as
    `Closed` and reach the bracket inverse — a combinator may not reintroduce a
    parked-forever path."""
    module = _module(_CHAIN_PARKED, "stream_chain_parked")
    root = Context()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    source = runtime_mod.Stream.last_source()
    for item in (1, 2, 3):
        assert source.emit(item) is True, "the provider is not blocked"
    await _flush()
    assert c.state is FiberState.ACTIVE, "every item was filtered out — still parked"

    c.dispose()
    await _flush()   # bounded: a deadlocked teardown would leave it UNLOADING
    assert c.state is FiberState.DISPOSED, "teardown completed (no deadlock)"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.close" in ops and "stream.stage close filter" in ops
    assert runtime_mod.Stream.pending() == 0, "no parked next, no dangling link"


# ---------------------------------------------------------------------------
# CRITICAL Part B through a chain: a provider terminal traverses every link
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_fault_traverses_the_chain_to_the_consumer(trace):
    """A provider abort with an outstanding `next` behind two links: the
    `Faulted` terminal must reach the consumer THROUGH the chain (§9 Part B).
    A link that swallowed it would leave the `next` silently pending."""
    module = _module(_CHAIN_PARKED, "stream_chain_faulted")
    root = Context()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    runtime_mod.Stream.last_source().fault("boom")
    await _flush()

    assert c.state is FiberState.FAILED, "the terminal reached the parked `next`"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.close" in ops and "stream.stage close map" in ops, \
        "the failing activation's prefix reversal closed the whole chain"
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_an_orderly_provider_close_traverses_the_chain():
    source = runtime_mod.Stream.source()
    sub = _chain(source, ("map", lambda x: x), ("filter", lambda x: True))
    source.close()
    assert await sub.next() is runtime_mod.STREAM_CLOSED
    sub.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_closing_the_subscription_cascades_up_the_chain():
    """`close` on the derived subscription closes its upstream links — but NOT
    the provider, which is owned by its own bracket (that separation is what
    keeps the Slice 1 close-order proof true)."""
    source = runtime_mod.Stream.source()
    sub = _chain(source, ("map", lambda x: x), ("take", 9))
    stages = runtime_mod.Stream.stages()
    assert [s.state for s in stages] == ["open", "open"]
    sub.close()
    assert [s.state for s in stages] == ["closed", "closed"]
    assert source.state == "open", "the provider outlives the derived chain"
    source.close()
    assert runtime_mod.Stream.pending() == 0


# ---------------------------------------------------------------------------
# Backpressure: every declared policy (§4.4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_newest_discards_the_incoming_item_and_records_it(trace):
    source = runtime_mod.Stream.source()
    sub = runtime_mod.Subscription(source, "drop_newest", capacity=2)
    for item in ("i0", "i1", "i2", "i3"):
        assert source.emit(item) is True, "a drop policy never blocks the provider"
    assert await sub.next() == "i0"
    assert await sub.next() == "i1"
    # the buffered prefix survives; the overflow is DISCARDED and recorded, so
    # the loss is explicit rather than silent.
    assert [e for e in _ops(trace) if e.startswith("stream.drop_newest")] == \
        ["stream.drop_newest i2", "stream.drop_newest i3"]
    assert sub.state == "active", "a drop policy never pauses"
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_drop_oldest_evicts_the_buffer_head_and_records_it(trace):
    source = runtime_mod.Stream.source()
    sub = runtime_mod.Subscription(source, "drop_oldest", capacity=2)
    for item in ("i0", "i1", "i2"):
        assert source.emit(item) is True
    # latest-wins: the head was evicted, the newest item is buffered
    assert await sub.next() == "i1"
    assert await sub.next() == "i2"
    assert [e for e in _ops(trace) if e.startswith("stream.drop_oldest")] == \
        ["stream.drop_oldest i0"]
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_block_pauses_the_provider_until_the_consumer_drains(trace):
    """`block` (§4.4): a full buffer REFUSES the delivery and puts the
    subscription in the reserved `Paused` state. No implicit retry — `emit`
    returns False, so the provider knows it is suspended; draining resumes it."""
    source = runtime_mod.Stream.source()
    sub = runtime_mod.Subscription(source, "block", capacity=2)
    assert source.emit("i0") is True
    assert source.emit("i1") is True
    assert sub.state == "active"
    assert source.emit("i2") is False, "the provider is told it is blocked"
    assert sub.state == "paused", "the reserved `Paused` state index"
    assert "stream.paused" in _ops(trace)

    assert await sub.next() == "i0"          # drains -> resumes eagerly
    assert sub.state == "active"
    assert "stream.resume" in _ops(trace)
    assert source.emit("i2") is True, "the provider may emit again"
    assert await sub.next() == "i1"
    assert await sub.next() == "i2", "no silent loss: nothing was dropped"
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_block_through_a_chain_reaches_the_provider():
    """Backpressure must traverse the chain: a link that swallowed the refusal
    would let the provider run ahead of a blocked consumer."""
    source = runtime_mod.Stream.source()
    sub = _chain(source, ("map", lambda x: x * 10), policy="block", capacity=1)
    assert source.emit(1) is True
    assert source.emit(2) is False, "the consumer's pause reached the provider"
    assert sub.state == "paused"
    assert await sub.next() == 10
    assert sub.state == "active"
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0


# ---------------------------------------------------------------------------
# The deterministic test clock: the `block` drain window (§8)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_block_drain_window_fires_on_clock_advance(trace):
    """The item-130 time-windowed behavior, made deterministic by the existing
    test clock (§8): a `block` subscription with a declared drain window does
    NOT resume the instant the consumer drains — it resumes when
    `Clock.advance` steps the timeline past the window. No wall-clock sleeps."""
    source = runtime_mod.Stream.source()
    sub = runtime_mod.Subscription(source, "block", capacity=2, drain_ms=10)
    for item in ("i0", "i1"):
        assert source.emit(item) is True
    assert source.emit("i2") is False
    assert sub.state == "paused"

    assert await sub.next() == "i0"
    assert sub.state == "paused", \
        "a windowed subscription stays Paused until the window fires"
    assert source.emit("i2") is False

    assert runtime_mod.Clock.advance(5) == 0
    assert sub.state == "paused", "the window has not elapsed yet"
    assert runtime_mod.Clock.advance(5) == 1, "the drain window fired"
    assert sub.state == "active"
    assert "stream.resume" in _ops(trace)
    assert source.emit("i2") is True

    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0
    assert runtime_mod.Clock.pending() == 0


@pytest.mark.asyncio
async def test_the_drain_window_re_arms_while_the_buffer_is_still_full(trace):
    source = runtime_mod.Stream.source()
    sub = runtime_mod.Subscription(source, "block", capacity=1, drain_ms=10)
    assert source.emit("i0") is True
    assert source.emit("i1") is False
    assert sub.state == "paused"
    # nothing drained: the window fires, finds the buffer still full, re-arms
    assert runtime_mod.Clock.advance(10) == 1
    assert sub.state == "paused"
    assert runtime_mod.Clock.pending() == 1, "the window re-armed"
    assert await sub.next() == "i0"
    assert runtime_mod.Clock.advance(10) == 1
    assert sub.state == "active"
    sub.close()
    source.close()
    assert runtime_mod.Stream.pending() == 0
    assert runtime_mod.Clock.pending() == 0, "close cancelled the drain window"


@pytest.mark.asyncio
async def test_closing_a_paused_subscription_leaves_no_armed_window():
    """The drain window is a revertible schedule like any timer: the bracket
    inverse cancels it, so a `block` subscription torn down while Paused leaves
    no orphaned interval (the R1/R4 residue proof)."""
    source = runtime_mod.Stream.source()
    sub = runtime_mod.Subscription(source, "block", capacity=1, drain_ms=10)
    source.emit("i0")
    assert source.emit("i1") is False
    assert runtime_mod.Clock.pending() == 1
    sub.close()
    source.close()
    assert runtime_mod.Clock.pending() == 0
    assert runtime_mod.Stream.pending() == 0
