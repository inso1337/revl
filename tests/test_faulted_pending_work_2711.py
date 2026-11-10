"""Where a faulted component's pending work goes (issue #2711).

docs/fault-tests.md section 12 lists every kind of work that can be pending for
an activation when its body faults, where that work is held, and what happens
to it at FAILED. Each row is pinned here on the cordis-py reference tier; the
rows already covered by the fault and stream suites point at those tests
instead of duplicating them.

The inventory is limited to a MID-ACTIVATION fault (A8): a component that lands
FAILED never served a call, only the work its activation had already queued
when the failure struck. A component that has activated cleanly does not fault;
it is disposed, which is the teardown contract's subject, not this one.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl._paths import python_backend_emitter, python_backend_first  # noqa: E402

# The py tier's own directory first, so `runtime` is the reference runtime and
# never a bare-name collision. The emitter itself is loaded through the same
# helper: a bare `import emit` would answer with whichever backend is first.
python_backend_first()
import runtime as rt  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a fault needs a real activation; the inventory is proven against "
           "the cordis-py reference tier (sh backends/python/setup.sh)",
)


def _cordis():
    from cordis import Context
    from cordis.fiber import FiberState
    return Context, FiberState


def _module(source: str, name: str) -> types.ModuleType:
    """Compile a revl composition and exec its py-tier emission."""
    code = python_backend_emitter().emit(compile_source(source, f"{name}.rvl"))
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(code, f"{name}.py", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


async def _flush(turns: int = 80) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def _maps(events: list, kind: str) -> list:
    """The `map#N.<kind>` trace events, without assuming a serial."""
    return [e for e in events
            if e.startswith("map#") and e.endswith("." + kind)]


@pytest.fixture(autouse=True)
def _reset_runtime():
    rt.Stream.reset()
    rt.Job.reset()
    rt.Clock.reset()
    # the live-instance registry is process-global and has no public reset
    rt._LIVE_INSTANCES.pop(_SPAWN_CHILD, None)
    yield
    rt._LIVE_INSTANCES.pop(_SPAWN_CHILD, None)
    rt.Stream.reset()
    rt.Job.reset()
    rt.Clock.reset()


# ---------------------------------------------------------------------------
# Row: a deferred emission the activation queued (session deferral queue)
# ---------------------------------------------------------------------------

_DEFERRED = """
extern emission deferred fn deliver(sink: Str, msg: Str) = @py {
    with open(sink, 'a') as _f:
        _f.write('deliver:' + msg + '\\n')
    return
}
service Ops { fn ping() -> Str }
component Agent provides ops: Ops {
  emit deliver("{sink}", "q0")
  let pool = effect Pool.open("boom://nope", 1) undo pool.close()
  provide ops { fn ping() = "pong" }
}
"""


@needs_cordis
def test_a_deferred_emission_queued_before_the_fault_fires_at_the_session_commit(
        tmp_path):
    """A deferred emission is not the activation's to lose: it is a descriptor
    on the SESSION's deferral queue. The fault does not drop it. It stays
    enumerated for the commit, and the commit flushes it once, in program order.

    The constraint the issue states still holds: firing the descriptor is a
    DELIVERY, not an undo, so nothing the fault's compensation already reverted
    runs a second time."""
    from revl.mcp.session import Session

    sink = str(tmp_path / "sink.log")
    ir = compile_source(_DEFERRED.replace("{sink}", sink), "deferred_fault.rvl")
    session = Session()
    session.load(ir)

    components = session.state()["components"]
    assert [row["state"] for row in components] == ["FAILED"], components
    assert session.state()["providedKeys"] == []
    owner = session._owner
    assert [d.descriptor()["args"] for d in owner._queue] == [[sink, "q0"]], \
        "the deferred emission is still on the session's queue, not dropped"

    manifest = session.commit()
    assert manifest["summary"] == [{"group": "deliver.deliver", "count": 1}]
    result = session.commit_confirm(manifest["hash"])
    assert result["committed"] is True
    assert (tmp_path / "sink.log").read_text(encoding="utf-8").splitlines() == \
        ["deliver:q0"], "the commit flushed the faulted activation's intent"


# ---------------------------------------------------------------------------
# Row: a request addressed to the faulted component
# ---------------------------------------------------------------------------

_REFUSED = """
service Ops { fn ping() -> Str }
component Agent provides ops: Ops {
  let pool = effect Pool.open("boom://nope", 1) undo pool.close()
  provide ops { fn ping() = "pong" }
}
"""


@needs_cordis
def test_a_request_to_the_failed_component_is_refused_by_name():
    """Nothing queues a call for a component that is not serving: the key has
    no live provider, and the refusal names the provider and the activation
    error instead of guessing 'inactive' (issue #1895)."""
    from revl.mcp.session import Session, SessionError

    session = Session()
    session.load(compile_source(_REFUSED, "refused.rvl"))
    with pytest.raises(SessionError) as excinfo:
        session.call("ops", "ping", [])
    message = str(excinfo.value)
    assert "key 'ops' has no provider" in message
    assert "its provider `Agent` failed to activate" in message
    assert "refused to open boom://nope" in message


# ---------------------------------------------------------------------------
# Row: stream items buffered for the component's subscription
# ---------------------------------------------------------------------------

_BUFFERED = """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  await sub.next()
  let pool = effect Pool.open("boom://nope", 1) undo pool.close()
}
"""


@needs_cordis
@pytest.mark.asyncio
async def test_buffered_stream_items_are_discarded_at_the_fault_and_the_drop_is_recorded():
    """The subscription is a bracket on the activation, so the fault unwind
    closes it. The item the body consumed was delivered; the two the provider
    emitted and the buffer ACCEPTED are discarded with the dead consumer. That
    is recorded as a count, so the loss is visible rather than silent, and a
    later provider emit is refused (the provider is told)."""
    Context, FiberState = _cordis()
    events: list = []
    rt.set_trace(events.append)
    try:
        module = _module(_BUFFERED, "buffered_fault")
        root = Context()
        c = root.plugin(module.C)
        await _flush()
        assert c.state is FiberState.ACTIVE
        source = rt.Stream.last_source()
        assert (source.emit("a"), source.emit("b"), source.emit("c")) == \
            (True, True, True)
        subscription = source._subs[0]
        assert len(subscription._buffer) == 3, \
            "the parked consumer has not drained any of the three accepted items"

        await _flush()
        assert c.state is FiberState.FAILED
        assert getattr(c, "_error", None) is not None
        assert rt.Stream.pending() == 0, "the bracket inverse closed the subscription"
        # a consumed + two buffered: the two the consumer never read are named
        assert "stream.discard 2" in events, events
        assert "stream.close" in events
        assert subscription._closed is True
        assert subscription._buffer == [], "the discarded items were released"
        assert source.emit("d") is False, "the provider sees the refusal"
        await c.dispose()
        await _flush()
    finally:
        rt.set_trace(None)


# ---------------------------------------------------------------------------
# Row: a parked next owned by the component
# ---------------------------------------------------------------------------

_PARKED = """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  await sub.next()
}
"""


@needs_cordis
@pytest.mark.asyncio
async def test_a_parked_next_is_resolved_by_a_terminal_event_never_left_pending():
    """A next is always terminated by an owner teardown or a provider terminal
    (design 130 section 4.3). A provider abort resolves the park, the exception
    faults the activation, and the bracket closes with no residue."""
    Context, FiberState = _cordis()
    events: list = []
    rt.set_trace(events.append)
    try:
        module = _module(_PARKED, "parked_fault")
        root = Context()
        c = root.plugin(module.C)
        await _flush()
        assert c.state is FiberState.ACTIVE
        assert rt.Stream.pending() == 2, "the source and its subscription are live"
        source = rt.Stream.last_source()
        source.fault("boom")
        await _flush()
        assert c.state is FiberState.FAILED
        assert "boom" in str(getattr(c, "_error", None))
        assert "stream.source fault boom" in events
        assert "stream.close" in events
        assert rt.Stream.pending() == 0
        await c.dispose()
        await _flush()
    finally:
        rt.set_trace(None)


# ---------------------------------------------------------------------------
# Row: an armed timer
# ---------------------------------------------------------------------------

_TIMER = """
service Log { emission fn write(line: Str) }
component Sink provides log: Log {
  provide log { fn write(line) { } }
}
component Heartbeat requires log: Log {
  after 5m { emit log.write("tick") }
  let pool = effect Pool.open("boom://nope", 1) undo pool.close()
}
"""


@needs_cordis
@pytest.mark.asyncio
async def test_an_armed_timer_is_cancelled_by_the_fault_unwind():
    """A schedule is a revertible acquisition whose inverse is cancellation, so
    a fault in the same activation cancels it. No timer outlives the failed
    activation, and no firing happens afterwards."""
    Context, FiberState = _cordis()
    events: list = []
    rt.set_trace(events.append)
    try:
        module = _module(_TIMER, "timer_fault")
        root = Context()
        sink = root.plugin(module.Sink)
        await _flush()
        heartbeat = root.plugin(module.Heartbeat)
        await _flush()
        assert sink.state is FiberState.ACTIVE
        assert heartbeat.state is FiberState.FAILED
        assert "timer#1.schedule after 300000ms" in events
        assert "timer#1.cancel" in events
        assert rt.Clock.pending() == 0, "the schedule did not outlive the fault"
        assert rt.Clock.advance(1_000_000) == 0, "no orphaned firing"
        await heartbeat.dispose()
        await sink.dispose()
        await _flush()
    finally:
        rt.set_trace(None)


# ---------------------------------------------------------------------------
# Row: a dependent waiting on the faulted component's provision
# ---------------------------------------------------------------------------

_DEPENDENT = """
service Beat { fn tick() -> Int }
component Broken provides beat: Beat {
  let pool = effect Pool.open("boom://nope", 1) undo pool.close()
  provide beat { fn tick() = 1 }
}
component Dep requires beat: Beat {
}
"""


@needs_cordis
@pytest.mark.asyncio
async def test_a_dependent_of_the_failed_component_stays_pending():
    """A dependent is not handed the fault: its requirement is simply unmet, so
    it stays PENDING and waits for a provider to return (R2), rather than
    failing or being fed a dead provider."""
    Context, FiberState = _cordis()
    module = _module(_DEPENDENT, "dependent_fault")
    root = Context()
    broken = root.plugin(module.Broken)
    await _flush()
    assert broken.state is FiberState.FAILED
    dependent = root.plugin(module.Dep)
    await _flush()
    assert dependent.state is FiberState.PENDING
    await dependent.dispose()
    await broken.dispose()
    await _flush()


# ---------------------------------------------------------------------------
# Row: a spawned child instance
# ---------------------------------------------------------------------------

_SPAWN_CHILD = "FaultSpawnChild"
_SPAWN = """
component FaultSpawnChild {
  let m = effect Map.new() undo m.drop()
}
component FaultSpawnParent {
  let w = effect spawn FaultSpawnChild with { } undo w.dispose()
  await Job.run("boot")
  let pool = effect Pool.open("boom://nope", 1) undo pool.close()
}
"""


@needs_cordis
@pytest.mark.asyncio
async def test_a_spawned_child_is_disposed_by_the_fault_unwind():
    """A spawn handle is a bracket on the parent. The child is live while the
    parent's activation awaits, and the parent's fault runs the handle's
    dispose: no child outlives the failed parent."""
    Context, FiberState = _cordis()
    events: list = []
    rt.set_trace(events.append)
    try:
        assert rt.live_instances(_SPAWN_CHILD) == [], "test pollution"
        module = _module(_SPAWN, "spawn_fault")
        root = Context()
        parent = root.plugin(module.FaultSpawnParent)
        child_seen = False
        for _ in range(20):
            await asyncio.sleep(0)
            if rt.live_instances(_SPAWN_CHILD):
                child_seen = True
            if parent.state is FiberState.FAILED:
                break
        assert child_seen, "the child was never live during the parent's activation"
        assert parent.state is FiberState.FAILED
        # the handle's dispose is the emitted inverse; drain the loop so the
        # child's own teardown completes before the assertions
        for _ in range(80):
            if not rt.live_instances(_SPAWN_CHILD) and _maps(events, "drop"):
                break
            await asyncio.sleep(0)
        assert rt.live_instances(_SPAWN_CHILD) == [], \
            "the spawned child outlived the faulted parent"
        # Map serials are process-global (there is no Map.reset), so match the
        # event shape rather than a particular serial
        assert _maps(events, "new"), "the child acquired its resource"
        assert _maps(events, "drop"), "the child's own inverse ran at disposal"
        await parent.dispose()
        await _flush()
    finally:
        rt.set_trace(None)
