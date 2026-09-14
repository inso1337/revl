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
    # registered before the exec because an emitted RECORD renders as a
    # `@dataclass`, and `dataclasses` resolves a string annotation by looking the
    # defining class's module up in `sys.modules`. A typed event always brings a
    # record with it (item 130 Slice 5), so this is the ordinary path now rather
    # than an exotic one; each test uses its own module name.
    sys.modules[name] = module
    try:
        exec(compile(code, f"{name}.py", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
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


@pytest.mark.asyncio
async def test_a_block_pause_does_not_spend_the_take_budget(trace):
    """A lossy/blocking POLICY and a `take(n)` link on the SAME subscription.

    The two mechanisms share one boolean — the acceptance `_deliver` answers —
    and `take(n)` counts only ACCEPTED items. So a `block` pause must not spend
    the budget: the item never landed, and a consumer that asked for two items
    has to receive two. If the pause spent the slot, `take(2)` would exhaust
    after ONE delivered item and push its `Closed` terminal early, and the
    second item would be gone with no overflow, no drop mark and no fault — the
    silent loss §4.4 exists to rule out.

    This is the case the blocking tiers mirror (go/rust/java), where the budget
    is RESERVED under the lock and refunded on refusal rather than decremented
    after the forward; the observable sequence has to be this one."""
    source = runtime_mod.Stream.source()
    sub = _chain(source, ("take", 2), policy="block", capacity=1)
    assert source.emit("i0") is True
    # the buffer is full; `block` refuses and pauses, and the refusal travels UP
    # through the take link, which must hand its slot back.
    assert source.emit("i1") is False
    assert sub.state == "paused"
    assert await sub.next() == "i0"        # drains -> resumes eagerly
    assert sub.state == "active"
    # the budget survived the pause, so the second item is still admissible
    assert source.emit("i1") is True
    assert await sub.next() == "i1"
    assert await sub.next() is runtime_mod.STREAM_CLOSED, (
        "take(2) ends the derived stream after the SECOND accepted item")
    ops = _ops(trace)
    assert "stream.emit i1 refused" in ops, "the refusal is traced, never silent"
    assert ops.count("stream.take exhausted") == 1
    # the pause did not exhaust the budget early: the terminal lands after the
    # second ACCEPTED item, not after the refused one.
    assert ops.index("stream.paused") < ops.index("stream.take exhausted")
    assert ops.index("stream.resume") < ops.index("stream.take exhausted")
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


# ---------------------------------------------------------------------------
# Slice 3 — `merge` on the reference tier (design §1)
#
# The fan-in is a DERIVED stream OWNED by the subscription, not a bracket of its
# own: `sub.close()` closes the merge, and closing the merge detaches it from
# both sources, which keep their own brackets. So multi-source teardown is the
# same single LIFO stack Slice 1 proved. These pin the two terminal rules that
# make a parked `next` on a fan-in always terminable.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_delivers_from_either_source_and_tears_down_as_one_stack():
    runtime_mod.Stream.reset()
    a = runtime_mod.Stream.source()
    b = runtime_mod.Stream.source()
    merged = runtime_mod.Stream.merge(a, b)
    sub = runtime_mod.Stream.subscribe(merged, "error")

    a.emit("from-a")
    b.emit("from-b")
    assert await sub.next() == "from-a"
    assert await sub.next() == "from-b"

    # ONE close unwinds the subscription and the fan-in it owns; the sources are
    # left to their own brackets.
    assert sub.close() is True
    assert merged.state == "closed"
    assert a.state == "open" and b.state == "open"
    a.close()
    b.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_merge_closed_only_after_every_source_is_done():
    """One source's close must not strand the consumer on the other; the LAST
    source's close must terminate it (never a parked-forever fan-in)."""
    runtime_mod.Stream.reset()
    a = runtime_mod.Stream.source()
    b = runtime_mod.Stream.source()
    sub = runtime_mod.Stream.subscribe(runtime_mod.Stream.merge(a, b), "error")

    a.close()
    b.emit("still-live")
    assert await sub.next() == "still-live"

    b.close()
    assert await sub.next() is runtime_mod.STREAM_CLOSED
    sub.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_merge_propagates_a_source_fault_immediately():
    """A fan-in source's abort reaches the consumer at once — no silent loss, no
    waiting on the sibling source that is still live (§4.3)."""
    runtime_mod.Stream.reset()
    a = runtime_mod.Stream.source()
    b = runtime_mod.Stream.source()
    sub = runtime_mod.Stream.subscribe(runtime_mod.Stream.merge(a, b), "error")

    a.fault("kafka gone")
    with pytest.raises(runtime_mod.StreamFaulted) as excinfo:
        await sub.next()
    assert "kafka gone" in str(excinfo.value)

    sub.close()
    a.close()
    b.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_merge_nests_and_one_close_unwinds_the_chain():
    runtime_mod.Stream.reset()
    a = runtime_mod.Stream.source()
    b = runtime_mod.Stream.source()
    c = runtime_mod.Stream.source()
    inner = runtime_mod.Stream.merge(a, b)
    outer = runtime_mod.Stream.merge(inner, c)
    sub = runtime_mod.Stream.subscribe(outer, "error")

    a.emit("deep")
    assert await sub.next() == "deep"

    sub.close()
    assert outer.state == "closed" and inner.state == "closed"
    a.close()
    b.close()
    c.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_merge_composes_with_the_slice_2_combinator_chain():
    """A fan-in IS a stream, so the Slice 2 chain applies to it unchanged:
    `subscribe merge(a, b).map(f).filter(p)`. The composition is where either
    slice could quietly break the other, so it is pinned here — items from
    either source traverse every link, ONE close unwinds stage -> stage -> merge
    while leaving both providers to their own brackets, and a source's fault
    reaches the consumer through the whole derived chain."""
    runtime_mod.Stream.reset()
    a = runtime_mod.Stream.source()
    b = runtime_mod.Stream.source()
    sub = runtime_mod.Stream.subscribe(
        runtime_mod.Stream.merge(a, b), "error",
        stages=[("map", lambda x: x.upper()), ("filter", lambda x: x != "SKIP")])

    a.emit("one")
    b.emit("skip")          # transformed, then filtered out
    b.emit("two")
    assert await sub.next() == "ONE"
    assert await sub.next() == "TWO"

    sub.close()
    # the derived chain unwound; the PROVIDERS are left to their own brackets
    assert a.state == "open" and b.state == "open"
    a.close()
    b.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_a_fan_in_fault_traverses_the_combinator_chain():
    runtime_mod.Stream.reset()
    a = runtime_mod.Stream.source()
    b = runtime_mod.Stream.source()
    sub = runtime_mod.Stream.subscribe(
        runtime_mod.Stream.merge(a, b), "error",
        stages=[("map", lambda x: x)])

    a.fault("boom")
    with pytest.raises(runtime_mod.StreamFaulted) as excinfo:
        await sub.next()
    assert "boom" in str(excinfo.value)

    sub.close()
    a.close()
    b.close()
    assert runtime_mod.Stream.pending() == 0


# ===========================================================================
# Slice 4 — `every <x> in <sub> { … }`, the async-iteration form (§1, §4.7)
# ===========================================================================
#
# The iteration form adds no runtime primitive: it is the Slice 1 protocol
# (`next` raced against the cancel token, `close` tripping it synchronously)
# driven in a loop. So these tests are not a re-proof of the protocol — they are
# the proof that the LOOP does not weaken it. Three things could, and each has a
# test: a loop that ran its body on a terminal would invent an item; a loop that
# swallowed a `Faulted` would turn a provider abort into a clean end of stream
# (and leave the subscription active through a handler failure, the exact
# obligation §6 lists); and a loop that did not resolve on withdrawal would park
# the owner forever behind a provider that never emits — the CRITICAL of §9.

_ITER = """
service Sink { emission fn write(v: Str) }
component C requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  every o in sub {
    emit sink.write(o)
  }
}
"""


class _Sink:
    """The consumer's boundary: records every item the iteration body emitted."""

    def __init__(self) -> None:
        self.written: list = []

    def write(self, v):
        self.written.append(v)
        return None


def _sink_root() -> tuple:
    root = Context()
    sink = _Sink()
    root.provide("sink", sink)
    return root, sink


@pytest.mark.asyncio
async def test_every_in_delivers_each_item_to_the_body_and_ends_on_the_terminal(trace):
    """Exit test §10.1 in the iteration form: N items reach the body in order,
    an orderly provider close ENDS the loop (the terminal is not an item), and
    the bracket inverse still closes the subscription before its source."""
    module = _module(_ITER, "stream_every")
    root, sink = _sink_root()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    src = runtime_mod.Stream.last_source()
    src.emit("a")
    src.emit("b")
    await _flush()
    assert sink.written == ["a", "b"], "each delivered item ran the body once"

    src.close()                     # orderly provider terminal
    await _flush()
    assert sink.written == ["a", "b"], "the `Closed` terminal is NOT an item"

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert runtime_mod.Stream.pending() == 0, "no dangling listener (no residue)"


@pytest.mark.asyncio
async def test_unloading_the_owner_ends_the_iteration_and_closes_the_stream(trace):
    """The core guarantee (§0) through the loop: withdraw the owner while the
    consumer is parked in the iteration's `next` on a provider that never emits.
    The park resolves as `Closed` (§9 Part A), the loop exits, `close` runs, and
    teardown does not deadlock behind the parked `next`."""
    module = _module(_ITER, "stream_every_unload")
    root, sink = _sink_root()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE
    assert runtime_mod.Stream.pending() == 2, "parked: source open + subscription live"

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED, "teardown completed (no deadlock)"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert ops.index("stream.close") < ops.index("stream.source close"), \
        "the subscription closes before its source (LIFO)"
    assert sink.written == [], "no item was invented for the terminal"
    assert runtime_mod.Stream.pending() == 0, "no residue"


@pytest.mark.asyncio
async def test_a_provider_fault_aborts_the_iteration_and_closes_the_subscription(trace):
    """§9 Part B through the loop, and the §6 events obligation: a `Faulted`
    terminal RAISES out of `next` rather than reading as an end of stream, so
    the activation fails, the prefix reverts LIFO, and the subscription is
    CLOSED — never left active behind a failure."""
    module = _module(_ITER, "stream_every_fault")
    root, sink = _sink_root()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    src = runtime_mod.Stream.last_source()
    src.emit("a")
    await _flush()
    assert sink.written == ["a"]

    src.fault("boom")
    await _flush()
    assert c.state is FiberState.FAILED, "the fault aborted the iteration (A8)"
    assert sink.written == ["a"], "the terminal never ran the body"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.close" in ops, "the bracket closed in the prefix reversal"
    assert runtime_mod.Stream.pending() == 0, "no residue after a provider terminal"


_ITER_FAIL = """
service Sink { emission fn write(v: Str) }
component C requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  every o in sub {
    emit sink.write(o)
    fail "handler refused the item"
  }
}
"""


@pytest.mark.asyncio
async def test_handler_failure_closes_the_subscription(trace):
    """Exit test §10.6 (the typed-events obligation, §6): an `every … in` body
    that `fail`s aborts the iteration, and because the subscription bracket is
    on the reverted prefix the failure CLOSES it. Delivered by A8 with no
    events-specific machinery."""
    module = _module(_ITER_FAIL, "stream_every_fail")
    root, sink = _sink_root()
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    src = runtime_mod.Stream.last_source()
    src.emit("a")
    await _flush()

    assert sink.written == ["a"], "the body ran on the item, then failed"
    assert c.state is FiberState.FAILED, "the handler failure aborted the activation"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.close" in ops, \
        "a failed handler does NOT leave the subscription active (§6)"
    assert runtime_mod.Stream.pending() == 0, "no residue"


_ITER_CHAIN = """
service Sink { emission fn write(v: Str) }
component C requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.filter(x => x != "skip").take(2) undo sub.close()
  every o in sub {
    emit sink.write(o)
  }
}
"""


@pytest.mark.asyncio
async def test_the_iteration_composes_with_the_combinator_chain(trace):
    """Slice 2 + Slice 4: the loop pulls a DERIVED stream, so a filtered item
    never reaches the body and a spent `take(n)` synthesises the `Closed`
    terminal the loop ends on — with the whole chain still riding the ONE
    bracket the `subscribe` registered."""
    module = _module(_ITER_CHAIN, "stream_every_chain")
    root, sink = _sink_root()
    c = root.plugin(module.C)
    await _flush()

    src = runtime_mod.Stream.last_source()
    src.emit("skip")
    src.emit("one")
    src.emit("two")
    src.emit("three")          # past take(2): never delivered
    await _flush()

    assert sink.written == ["one", "two"], \
        "filtered items never reach the body; `take` bounds it"
    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert runtime_mod.Stream.pending() == 0, "the whole chain unwound (no residue)"


# The SAME program the three blocking tiers now run against their own runtimes
# (backends/go/testdata/stream_130.rvl, backends/rust/scenarios/stream.rvl,
# backends/java/scenarios/stream_130.rvl, each as `component Chain`). It is here
# so the reference answer the four tiers are held to is asserted in one place: a
# chain that compiled on a tier and delivered a DIFFERENT list from this one
# would be far worse than the refusal the blocking tiers used to give.
_ITER_CHAIN_MAPPED = """
service Sink { emission fn write(v: Str) }
component Chain requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.filter(x => x != "skip").map(x => x + "!").take(2)
              undo sub.close()
  every o in sub {
    emit sink.write(o)
  }
}
"""


@pytest.mark.asyncio
async def test_the_chain_scenario_the_blocking_tiers_mirror(trace):
    """The reference answer for the cross-tier `Chain` scenario: a filtered item
    never reaches the body, a mapped one reaches it TRANSFORMED, and a spent
    `take(2)` synthesises the `Closed` terminal the loop ends on — with the whole
    chain still riding the ONE bracket the `subscribe` registered."""
    module = _module(_ITER_CHAIN_MAPPED, "stream_chain_mirror")
    root, sink = _sink_root()
    c = root.plugin(module.Chain)
    await _flush()

    src = runtime_mod.Stream.last_source()
    for item in ("skip", "one", "skip", "two", "three"):
        src.emit(item)
    await _flush()

    assert sink.written == ["one!", "two!"]
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.take exhausted" in ops, \
        "take(2) synthesises its own Closed terminal"
    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert runtime_mod.Stream.pending() == 0, "the whole chain unwound (no residue)"


# ===========================================================================
# Slice 5 — typed EVENTS: `on <Event> as <x> in <sub> { … }` (§6)
# ===========================================================================
#
# Events are a `Stream[T]` SPECIALIZATION, so these tests are not a re-proof of
# the protocol or of the loop. They are the proof that the CONTRACT does what §6
# assigns to it and costs the guarantee nothing:
#
#   * every delivered item is validated against the event's schema BEFORE the
#     body runs, and a violation takes the `Faulted` path that already closes
#     the subscription — "a failed handler does not leave a subscription active"
#     reached with no events-specific teardown;
#   * a redelivery inside the bounded window is collapsed, and the collapse is
#     traced rather than silent;
#   * the window is BOUNDED, which bounds what it claims: past it, a redelivery
#     runs the handler again. That is the honest limit, not a bug — a durable
#     exactly-once claim needs the §4.5 cursor, a later slice;
#   * the terminal is still not an item, and withdrawal while parked still ends
#     the handler and closes the subscription LIFO.

_EVENT = """
event OrderCreated(key: order_id, window: 2) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }
component Fulfiller requires ship: Ship {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  on OrderCreated as e in sub {
    emit ship.dispatch(e.order_id)
  }
}
"""


class _Ship:
    def __init__(self) -> None:
        self.dispatched: list = []

    def dispatch(self, id):
        self.dispatched.append(id)
        return None


def _ship_root() -> tuple:
    root = Context()
    ship = _Ship()
    root.provide("ship", ship)
    return root, ship


def _order(oid: str, qty: int = 1) -> dict:
    return {"order_id": oid, "quantity": qty}


@pytest.mark.asyncio
async def test_a_conforming_item_reaches_the_body_and_a_duplicate_is_collapsed(trace):
    """§6's two added rows at once: schema compatibility (the item validates and
    the handler reads its declared fields) and duplicate handling (a redelivery
    of a key already admitted does NOT run the body again)."""
    module = _module(_EVENT, "event_dedup")
    root, ship = _ship_root()
    c = root.plugin(module.Fulfiller)
    await _flush()
    assert c.state is FiberState.ACTIVE

    src = runtime_mod.Stream.last_source()
    src.emit(_order("o1"))
    src.emit(_order("o2"))
    src.emit(_order("o1"))          # a redelivery, inside the window
    await _flush()

    assert ship.dispatched == ["o1", "o2"], "the duplicate never ran the handler"
    assert c.state is FiberState.ACTIVE, "a duplicate is not a failure"
    ops = _ops(trace)
    assert ops.count("event.OrderCreated admit") == 2
    assert ops.count("event.OrderCreated duplicate") == 1, \
        "the collapse is traced, not silent"

    c.dispose()
    await _flush()
    assert runtime_mod.Stream.pending() == 0, "no residue"


@pytest.mark.asyncio
async def test_the_dedup_window_is_bounded_and_says_so(trace):
    """The window is a fixed-size LRU (`window: 2` here), which is what keeps the
    dedup memory constant in the length of the stream rather than one entry per
    delivered item — the shape §4.7 refuses. Bounded memory bounds the claim:
    once a key has aged out, its redelivery runs the handler again. This
    collapses redeliveries; it is not a durable exactly-once claim (§4.5)."""
    module = _module(_EVENT, "event_window")
    root, ship = _ship_root()
    c = root.plugin(module.Fulfiller)
    await _flush()

    src = runtime_mod.Stream.last_source()
    for oid in ("o1", "o2", "o3"):          # o1 ages out of a 2-key window
        src.emit(_order(oid))
    await _flush()
    src.emit(_order("o1"))
    await _flush()

    assert ship.dispatched == ["o1", "o2", "o3", "o1"]
    assert c.state is FiberState.ACTIVE

    c.dispose()
    await _flush()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_a_schema_violation_faults_the_handler_and_closes_the_subscription(trace):
    """§6, "a failed handler does not leave a subscription active", reached by
    the path Slice 4 already defines. A provider that delivers an item the event
    does not describe breaks the contract at the boundary; `admit` raises
    `StreamFaulted` — the SAME terminal a provider abort delivers — which the
    loop does not catch, so the activation fails and the accumulated prefix
    reverts LIFO with the subscription bracket on it."""
    module = _module(_EVENT, "event_schema")
    root, ship = _ship_root()
    c = root.plugin(module.Fulfiller)
    await _flush()
    assert c.state is FiberState.ACTIVE

    src = runtime_mod.Stream.last_source()
    src.emit(_order("o1"))
    await _flush()
    assert ship.dispatched == ["o1"]

    src.emit({"order_id": "o2"})            # `quantity` is required
    await _flush()

    assert c.state is FiberState.FAILED, "the contract breach aborted the handler"
    assert ship.dispatched == ["o1"], "the malformed item never reached the body"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert "stream.close" in ops, \
        "a failed handler does NOT leave the subscription active (§6)"
    assert runtime_mod.Stream.pending() == 0, "no residue"


@pytest.mark.asyncio
async def test_a_mistyped_field_is_a_contract_breach_not_a_body_crash(trace):
    """The schema is checked at the BOUNDARY, before the value binds — so a
    provider that sends the right field at the wrong type is caught as a stream
    fault with a path in its message, never as a mid-body host error the handler
    would have to defend against."""
    module = _module(_EVENT, "event_mistyped")
    root, ship = _ship_root()
    c = root.plugin(module.Fulfiller)
    await _flush()

    src = runtime_mod.Stream.last_source()
    src.emit({"order_id": 7, "quantity": 1})
    await _flush()

    assert c.state is FiberState.FAILED
    assert ship.dispatched == []
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_the_terminal_is_not_validated_as_an_item(trace):
    """The gate sits AFTER the terminal test: an orderly `Closed` ends the loop
    without being run through the schema, so the end of a stream is never
    reported as a contract breach."""
    module = _module(_EVENT, "event_terminal")
    root, ship = _ship_root()
    c = root.plugin(module.Fulfiller)
    await _flush()

    src = runtime_mod.Stream.last_source()
    src.emit(_order("o1"))
    await _flush()
    src.close()
    await _flush()

    assert c.state is FiberState.ACTIVE, "an orderly close is not a fault"
    assert ship.dispatched == ["o1"]

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_unloading_the_owner_ends_the_handler_and_closes_the_stream(trace):
    """The core guarantee (§0) through the handler, unchanged by the contract:
    withdraw the owner while the consumer is parked on a provider that never
    emits. The park resolves as `Closed` (§9 Part A), the handler ends, `close`
    runs before the source's inverse (LIFO), and teardown does not deadlock."""
    module = _module(_EVENT, "event_unload")
    root, ship = _ship_root()
    c = root.plugin(module.Fulfiller)
    await _flush()
    assert runtime_mod.Stream.pending() == 2, "parked: source open + subscription live"

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED, "teardown completed (no deadlock)"
    ops = [e for e in _ops(trace) if e.startswith("stream")]
    assert ops.index("stream.close") < ops.index("stream.source close"), \
        "the subscription closes before its source (LIFO)"
    assert ship.dispatched == [], "no item was invented for the terminal"
    assert runtime_mod.Stream.pending() == 0, "no residue"


@pytest.mark.asyncio
async def test_the_contract_is_built_once_not_per_item(trace):
    """The dedup memory is per HANDLER, not per delivered item: if the contract
    were rebuilt each turn its table would be empty every time and no duplicate
    would ever be recognised. This is the same test the emitted code's shape
    makes (`Stream.contract(...)` above `while True:`), observed at runtime."""
    module = _module(_EVENT, "event_once")
    root, ship = _ship_root()
    c = root.plugin(module.Fulfiller)
    await _flush()

    src = runtime_mod.Stream.last_source()
    for _ in range(4):
        src.emit(_order("same"))
        await _flush()

    assert ship.dispatched == ["same"], "one admit, three collapses"
    c.dispose()
    await _flush()
    assert runtime_mod.Stream.pending() == 0


# ===========================================================================
# §4.5 — provider-declared replay, and §4.9's durable cursor
# ===========================================================================
#
# Replay is the one stream property a consumer cannot have on its own: the
# PROVIDER holds the backlog, so the claim is the provider's to make and the
# frontend refuses a consumer that asks for one nobody declared. What follows is
# the runtime half of that — what the declaration actually buys, executed
# end to end where the surface allows it and against the reference protocol
# where it does not.
#
# The end-to-end shape needs a window: a consumer that subscribes at the same
# instant its source opens has nothing to replay. `await Job.run(..)` between
# the two is that window — the body parks for a few scheduler turns, the harness
# emits into the open provider, and the subscription that follows sees the
# backlog. That is exactly the case replay exists for, and the CONTROL below
# (the same program and the same timing with no `replay`) sees nothing at all.

_REPLAY_LATE = """
service Sink { emission fn write(v: Str) }
component C requires sink: Sink {
  let src = effect Stream.source() replay(3) undo src.close()
  await Job.run("warm")
  let sub = subscribe src replay(3) undo sub.close()
  every o in sub { emit sink.write(o) }
}
"""

_LIVE_ONLY = _REPLAY_LATE.replace(" replay(3)", "")


async def _park_until_source() -> object:
    """Turn the loop until the activation has opened its provider and parked in
    `Job.run` — the window in which a late subscriber's backlog accumulates."""
    for _ in range(8):
        await asyncio.sleep(0)
        if runtime_mod.Stream.last_source() is not None:
            return runtime_mod.Stream.last_source()
    raise AssertionError("the provider never opened")


@pytest.mark.asyncio
async def test_a_declared_backlog_reaches_a_consumer_that_subscribed_late():
    """§4.5 end to end: four items are emitted while the consumer is still
    parked before its `subscribe`. A `replay(3)` provider holds the last three,
    and the iteration body runs on exactly those before any live item."""
    module = _module(_REPLAY_LATE, "stream_replay_late")
    root, sink = _sink_root()
    c = root.plugin(module.C)
    src = await _park_until_source()

    for item in ("a", "b", "c", "d"):
        src.emit(item)
    await _flush()

    assert sink.written == ["b", "c", "d"], \
        "the last three held items replayed, in order, oldest first"

    src.emit("e")
    await _flush()
    assert sink.written == ["b", "c", "d", "e"], "live items follow the backlog"

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert runtime_mod.Stream.pending() == 0, "no residue: the bracket still closed"


@pytest.mark.asyncio
async def test_without_the_declaration_the_same_program_sees_no_backlog():
    """The non-vacuity control. Identical program and identical timing with the
    `replay` dropped: every item emitted before the `subscribe` is gone, which
    is §4.5's default (a consumer sees only what was emitted after it
    subscribed) and the thing the declaration changes."""
    module = _module(_LIVE_ONLY, "stream_live_only")
    root, sink = _sink_root()
    c = root.plugin(module.C)
    src = await _park_until_source()

    for item in ("a", "b", "c", "d"):
        src.emit(item)
    await _flush()

    assert sink.written == [], "no declaration, no backlog"

    c.dispose()
    await _flush()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_a_replayed_item_takes_the_declared_buffer_and_policy():
    """A replayed item is delivered through the provider's own forward path, so
    it takes the declared bounded buffer and overflow policy exactly as a live
    item does. There is no second delivery path for a backlog to diverge on:
    a backlog larger than the buffer is ordinary `error`-policy overflow."""
    runtime_mod.Stream.reset()
    src = runtime_mod.Stream.source(replay={"count": 6})
    for i in range(6):
        src.emit(i)

    sub = runtime_mod.Stream.subscribe(src, "error", None, capacity=2,
                                       replay={"count": 6})
    assert await sub.next() == 0
    assert await sub.next() == 1
    with pytest.raises(runtime_mod.StreamFaulted) as excinfo:
        await sub.next()
    assert "overflow" in str(excinfo.value)

    sub.close()
    src.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_a_durable_cursor_resumes_where_the_last_consumer_stopped():
    """§4.9's mechanism, observed: the cursor is a POSITION, advanced when the
    consumer takes an item and not when one is merely delivered. A second
    subscription on the same declared cursor picks up at the next item, never
    re-delivering what was already consumed."""
    runtime_mod.Stream.reset()
    src = runtime_mod.Stream.source(replay={"cursor": "orders"})
    for i in range(4):
        src.emit(f"e{i}")

    first = runtime_mod.Stream.subscribe(src, "error", None,
                                         replay={"cursor": "orders"})
    assert [await first.next(), await first.next()] == ["e0", "e1"]
    first.close()
    assert runtime_mod.Stream.cursor_at("orders") == 1

    second = runtime_mod.Stream.subscribe(src, "error", None,
                                          replay={"cursor": "orders"})
    assert [await second.next(), await second.next()] == ["e2", "e3"]
    assert runtime_mod.Stream.cursor_at("orders") == 3
    second.close()
    src.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_a_cursor_behind_the_held_backlog_is_a_faulted_terminal():
    """The bound is honest. A provider holds a BOUNDED backlog (§4.4: there are
    no unbounded buffers), so a cursor the provider has already trimmed past
    cannot be resumed. Resuming anyway would silently skip items, so it is a
    `Faulted` terminal like any other — the consumer is told."""
    runtime_mod.Stream.reset()
    src = runtime_mod.Stream.source(replay={"cursor": "c"})
    src.emit("a")
    src.emit("b")
    first = runtime_mod.Stream.subscribe(src, "error", None,
                                         replay={"cursor": "c"})
    assert await first.next() == "a"
    first.close()

    cap = runtime_mod.StreamSource.DEFAULT_REPLAY_CAPACITY
    for i in range(cap + 4):
        src.emit(i)

    second = runtime_mod.Stream.subscribe(src, "error", None,
                                          replay={"cursor": "c"})
    with pytest.raises(runtime_mod.StreamFaulted) as excinfo:
        await second.next()
    assert "replay gap" in str(excinfo.value)

    second.close()
    src.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_a_fresh_cursor_starts_at_the_oldest_held_item_not_a_gap():
    """A cursor with no recorded position is not a gap: it resumes at the oldest
    item the provider still holds. `from the beginning of time` is a claim a
    bounded buffer cannot back, and faulting a first subscribe would make the
    declaration useless."""
    runtime_mod.Stream.reset()
    src = runtime_mod.Stream.source(replay={"cursor": "fresh"})
    for i in range(3):
        src.emit(i)
    sub = runtime_mod.Stream.subscribe(src, "error", None,
                                       replay={"cursor": "fresh"})
    assert [await sub.next() for _ in range(3)] == [0, 1, 2]
    sub.close()
    src.close()
    assert runtime_mod.Stream.pending() == 0


@pytest.mark.asyncio
async def test_a_declared_provider_with_no_consumer_request_replays_nothing():
    """The declaration alone changes nothing a consumer sees: the provider holds
    the backlog, and only a `subscribe … replay(…)` reads it. So a source that
    declares replay and a subscription that does not ask is byte-for-byte the
    default behaviour."""
    runtime_mod.Stream.reset()
    src = runtime_mod.Stream.source(replay={"count": 4})
    src.emit("old")
    sub = runtime_mod.Stream.subscribe(src, "error", None)
    src.emit("new")
    assert await sub.next() == "new", "only what was emitted after subscribe"
    sub.close()
    src.close()
    assert runtime_mod.Stream.pending() == 0


# ---------------------------------------------------------------------------
# The required `Stream[T]` coeffect (§4.3, §6b) — run, not read
#
# The source is no longer acquired in the consumer's own body: it is DECLARED as
# a requirement and supplied by the wiring, so the stream outlives the
# activation that reads it. These pin that the three properties the guarantee
# rests on survive the new path — a `Closed` ends the loop without becoming an
# item, a `Faulted` is NOT caught so the activation fails and the prefix reverts
# with the subscription bracket on it, and a withdrawal reaches a parked `next`
# — plus the two the coeffect adds: an unmet requirement PENDS instead of
# subscribing to nothing, and a requirement satisfied by something that is not a
# stream is refused at the injection point.
# ---------------------------------------------------------------------------

_COEFFECT = """
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }
component Fulfiller requires feed: Stream[OrderCreated], ship: Ship {
  on OrderCreated as e {
    emit ship.dispatch(e.order_id)
  }
}
"""


def _wired(module_name: str, src: str = _COEFFECT):
    """A root context with the stream and the sink both supplied by the WIRING.

    `feed` is provided exactly the way `ship` is — one `provide` call on the
    root — which is the whole claim: a required stream is an ordinary
    requirement on the same graph, not a second kind of wiring."""
    module = _module(src, module_name)
    root = Context()
    source = runtime_mod.StreamSource()
    ship = _Ship()
    root.provide("feed", source)
    root.provide("ship", ship)
    return module, root, source, ship


@pytest.mark.asyncio
async def test_a_wired_stream_reaches_a_handler_that_names_no_subscription(trace):
    """The ergonomics claim, executed: `on OrderCreated as e { … }` with no `in
    <sub>` resolves the component's required stream, subscribes to it, and
    delivers each item to the body. The bracket is real — `stream.subscribe` is
    traced — and it closes on unload with no residue."""
    module, root, source, ship = _wired("coeffect_delivery")
    c = root.plugin(module.Fulfiller)
    await _flush()
    assert c.state is FiberState.ACTIVE

    source.emit(_order("o1"))
    source.emit(_order("o2"))
    await _flush()
    assert ship.dispatched == ["o1", "o2"]

    ops = _ops(trace)
    assert "stream.subscribe" in ops, "the handler registered a real bracket"

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert "stream.close" in _ops(trace), "the bracket inverse ran"
    # the source is the WIRING's, not this activation's, so it is still open —
    # and that is the point of the coeffect. The subscription is what had to go.
    assert all(s._closed for s in runtime_mod.Stream._subs), "no dangling listener"


@pytest.mark.asyncio
async def test_a_closed_terminal_ends_the_handler_and_is_not_an_item(trace):
    """The terminal is not a value: a provider close ENDS the loop, and the body
    never runs for it. Same rule as the explicit form, re-proved on the path that
    reaches the subscription through the wiring."""
    module, root, source, ship = _wired("coeffect_closed")
    c = root.plugin(module.Fulfiller)
    await _flush()
    source.emit(_order("o1"))
    await _flush()
    source.close()
    await _flush()

    assert ship.dispatched == ["o1"], "the terminal never became an item"
    assert c.state is FiberState.ACTIVE, "an orderly close is not a failure"
    c.dispose()
    await _flush()
    assert runtime_mod.Stream.pending() == 0, "no residue"


@pytest.mark.asyncio
async def test_a_faulted_terminal_is_not_caught_and_reverts_the_prefix(trace):
    """`Faulted` RAISES out of the handler rather than reading as an end of
    stream, so the activation fails and the accumulated prefix reverts LIFO with
    the subscription bracket on it (§4.3, A8). If the coeffect path had caught
    it, a dead provider would have looked like a clean end of stream."""
    module, root, source, ship = _wired("coeffect_faulted")
    c = root.plugin(module.Fulfiller)
    await _flush()
    source.emit(_order("o1"))
    await _flush()
    source.fault("provider aborted")
    await _flush()

    assert ship.dispatched == ["o1"]
    assert c.state is FiberState.FAILED, "the fault aborted the activation"
    assert "stream.close" in _ops(trace), \
        "the subscription bracket reverted with the prefix"
    assert all(s._closed for s in runtime_mod.Stream._subs)


@pytest.mark.asyncio
async def test_withdrawal_reaches_a_next_parked_on_a_wired_stream(trace):
    """§9 Part A through the coeffect: an owner withdrawn while parked on a
    provider that never emits still closes, with no deadlock behind the park."""
    module, root, source, ship = _wired("coeffect_parked")
    c = root.plugin(module.Fulfiller)
    await _flush()
    assert c.state is FiberState.ACTIVE

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert "stream.close" in _ops(trace)
    assert all(s._closed for s in runtime_mod.Stream._subs), "no dangling listener"


@pytest.mark.asyncio
async def test_an_unmet_stream_requirement_pends_instead_of_subscribing(trace):
    """The coeffect-layer half of §4.3: an unmet requirement leaves the component
    PENDING under R2. A required stream is on the same graph, so a consumer whose
    stream has no provider waits for one — it does not subscribe to nothing, and
    no bracket is registered."""
    module = _module(_COEFFECT, "coeffect_pending")
    root = Context()
    root.provide("ship", _Ship())
    c = root.plugin(module.Fulfiller)
    await _flush()

    assert c.state is FiberState.PENDING, "an unmet stream requirement pends"
    assert "stream.subscribe" not in _ops(trace), "nothing was subscribed"

    # ... and it activates once the wiring supplies the stream.
    source = runtime_mod.StreamSource()
    root.provide("feed", source)
    await _flush()
    assert c.state is FiberState.ACTIVE
    assert "stream.subscribe" in _ops(trace)
    c.dispose()
    await _flush()


@pytest.mark.asyncio
async def test_a_requirement_met_by_a_non_stream_is_refused_at_the_injection():
    """Rule 3.6 is the one admission rule the frontend cannot discharge for a
    required stream: there is no local acquisition to read an inverse off. So the
    shape is checked where the value arrives. A value that is not a stream has no
    terminal to deliver, which is exactly the silent-vanish state §9 Part B
    forbids — refused by name rather than parked on forever."""
    module = _module(_COEFFECT, "coeffect_bad_injection")
    root = Context()
    root.provide("feed", object())
    root.provide("ship", _Ship())
    c = root.plugin(module.Fulfiller)
    await _flush()

    assert c.state is FiberState.FAILED, \
        "the injection is refused, not parked on"
    assert runtime_mod.Stream.pending() == 0, "nothing was left half-subscribed"


@pytest.mark.asyncio
async def test_the_stream_outlives_the_activation_across_a_restart(trace):
    """The structural half of §4.9 the coeffect delivers.

    Before it, a source and its subscription were acquired in the SAME body, so
    the stream died with the activation and a consumer could not be re-activated
    against the stream it had been reading — the cross-process case could not be
    spelled at all. With the source declared as a requirement, the stream belongs
    to the wiring: activation #1 reads items and is torn down (the crash), and a
    SECOND activation of the same component, wired to the same stream, keeps
    reading it.

    What this does NOT claim is replay. A restarted consumer here resumes at the
    live edge; resuming from a durable cursor is `replay(from: …)` (§4.5), whose
    provider-declared surface is a separate change. This pins the half that was
    structurally impossible, and it is the half §4.9 was blocked on."""
    module = _module(_COEFFECT, "coeffect_restart")
    source = runtime_mod.StreamSource()

    root_a = Context()
    ship_a = _Ship()
    root_a.provide("feed", source)
    root_a.provide("ship", ship_a)
    first = root_a.plugin(module.Fulfiller)
    await _flush()
    source.emit(_order("o1"))
    await _flush()
    assert ship_a.dispatched == ["o1"]

    first.dispose()                      # the crash: the activation goes away
    await _flush()
    assert first.state is FiberState.DISPOSED
    assert source.state == "open", "the stream is the wiring's, not the fiber's"

    # the restart: a second activation, wired to the SAME stream
    root_b = Context()
    ship_b = _Ship()
    root_b.provide("feed", source)
    root_b.provide("ship", ship_b)
    second = root_b.plugin(module.Fulfiller)
    await _flush()
    assert second.state is FiberState.ACTIVE

    source.emit(_order("o2"))
    await _flush()
    assert ship_b.dispatched == ["o2"], \
        "the restarted consumer reads the stream the first one was reading"
    assert ship_a.dispatched == ["o1"], "the dead activation took no further item"

    second.dispose()
    await _flush()
    assert all(s._closed for s in runtime_mod.Stream._subs)


# ---------------------------------------------------------------------------
# §4.9 END TO END: a cross-restart replay, written as one revl program
#
# This is the case the item circled from both sides. §4.5's durable cursor gave
# a crashed subscription a re-issuable descriptor, but the source and its
# subscription were still acquired in the SAME body, so "a fresh process picks
# the subscription up where it stopped" could not be spelled: the fresh process
# would acquire a fresh provider, and resuming a cursor into a log nobody else
# held is not a resume. §6c's required `Stream[T]` moves the provider to the
# WIRING, and the requirement is where its durability claim is stated — so the
# two halves compose into one program, and this runs it.
# ---------------------------------------------------------------------------

_CROSS_RESTART = """
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }

component Fulfiller requires feed: Stream[OrderCreated] replay(from: "fulfiller-cursor"),
                             ship: Ship {
  let sub = subscribe feed replay(from: "fulfiller-cursor") undo sub.close()
  on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
}
"""


def _process(module, source):
    """One PROCESS: a fresh root context wired to the SAME stream.

    The stream is the wiring's, so it is passed in rather than built here — that
    is the whole point of the coeffect. `Stream._cursors` is the durable store
    the cursor's position lives in; it outlives a context exactly as a real one
    outlives a process."""
    root = Context()
    ship = _Ship()
    root.provide("feed", source)
    root.provide("ship", ship)
    return root.plugin(getattr(module, "Fulfiller")), ship


@pytest.mark.asyncio
async def test_a_crashed_consumer_resumes_from_its_durable_cursor_in_a_fresh_process(trace):
    """§4.9's exit case, end to end rather than at the WAL.

    Process A consumes two orders and is killed mid-stream. Four more are
    emitted while nothing is consuming. Process B — a fresh activation of the
    SAME program, wired to the SAME stream — resumes at the recorded cursor: it
    sees exactly what it missed, in order, and does NOT re-deliver what A had
    already handled. Both halves are load-bearing and the test would pass
    without either being right, so both are asserted: the missed items ARE
    delivered (the resume happened) and the handled ones are NOT (it resumed at
    the cursor rather than at the start of the backlog)."""
    module = _module(_CROSS_RESTART, "cross_restart")
    source = runtime_mod.StreamSource(replay={"cursor": "fulfiller-cursor"})

    first, ship_a = _process(module, source)
    await _flush()
    assert first.state is FiberState.ACTIVE
    source.emit(_order("o1"))
    source.emit(_order("o2"))
    await _flush()
    assert ship_a.dispatched == ["o1", "o2"]
    recorded = runtime_mod.Stream.cursor_at("fulfiller-cursor")
    assert recorded == 1, "the cursor advanced with the items A handled"

    first.dispose()                       # the crash
    await _flush()
    assert first.state is FiberState.DISPOSED
    assert source.state == "open", "the stream is the wiring's, not the fiber's"

    # the outage: emitted with nothing consuming, held by the provider's backlog
    for oid in ("o3", "o4", "o5", "o6"):
        source.emit(_order(oid))
    await _flush()
    assert ship_a.dispatched == ["o1", "o2"], "the dead activation took nothing"

    second, ship_b = _process(module, source)
    await _flush()
    assert second.state is FiberState.ACTIVE
    assert ship_b.dispatched == ["o3", "o4", "o5", "o6"], \
        "the fresh process resumed at the cursor and caught up on the outage"
    assert "o1" not in ship_b.dispatched and "o2" not in ship_b.dispatched, \
        "it resumed AT the cursor, not at the start of the provider's backlog"

    # ... and it keeps reading live items from there
    source.emit(_order("o7"))
    await _flush()
    assert ship_b.dispatched == ["o3", "o4", "o5", "o6", "o7"]
    assert runtime_mod.Stream.cursor_at("fulfiller-cursor") == 6

    second.dispose()
    await _flush()
    assert all(s._closed for s in runtime_mod.Stream._subs), \
        "both subscriptions closed on their own brackets"


@pytest.mark.asyncio
async def test_the_same_program_without_the_cursor_loses_the_outage(trace):
    """The non-vacuity control for the test above, and the measure of what the
    cursor buys. Identical program and identical timing with `replay(from: …)`
    dropped from BOTH ends: process B subscribes at the live edge, so everything
    emitted during the outage is gone. That is §4.5's default and correct; it is
    also exactly what a resume is not."""
    live_only = _CROSS_RESTART \
        .replace(' replay(from: "fulfiller-cursor"),', ',') \
        .replace(' replay(from: "fulfiller-cursor") undo', ' undo')
    module = _module(live_only, "cross_restart_live_only")
    source = runtime_mod.StreamSource()

    first, ship_a = _process(module, source)
    await _flush()
    source.emit(_order("o1"))
    await _flush()
    assert ship_a.dispatched == ["o1"]
    first.dispose()
    await _flush()

    for oid in ("o2", "o3"):
        source.emit(_order(oid))
    await _flush()

    second, ship_b = _process(module, source)
    await _flush()
    assert ship_b.dispatched == [], "no cursor, no resume: the outage is lost"
    source.emit(_order("o4"))
    await _flush()
    assert ship_b.dispatched == ["o4"], "it reads only from where it subscribed"
    second.dispose()
    await _flush()


@pytest.mark.asyncio
async def test_a_wiring_that_supplies_a_stream_holding_no_such_cursor_is_refused():
    """The claim is checked where it is satisfied. The frontend admitted the
    request against the REQUIREMENT's declaration, and nothing there can know
    what the wiring actually injected — so a provider holding no such cursor is
    refused at the injection point. Unchecked it would answer an empty backlog,
    which reads as an uneventful outage: the vacuous durability claim §4.5
    exists to keep off the wire."""
    module = _module(_CROSS_RESTART, "cross_restart_bad_wiring")
    source = runtime_mod.StreamSource()        # declares no backlog at all

    root = Context()
    root.provide("feed", source)
    root.provide("ship", _Ship())
    c = root.plugin(module.Fulfiller)
    await _flush()

    assert c.state is FiberState.FAILED, \
        "a resume against a provider that holds no such cursor is refused"
    assert runtime_mod.Stream.pending() == 1, \
        "only the wiring's own source is live: nothing was half-subscribed"
