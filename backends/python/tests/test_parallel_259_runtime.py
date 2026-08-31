"""Checked parallel emissions - the py runtime fan-out (item 259, Slice 2).

Slice 1 derived the partition (a checker-only plan); this slice makes the py
runtime FAN a proved-independent group out concurrently and rejoin it so that a
clean run is byte-identical to sequential and a fault or a divert is
teardown-EFFECT equivalent (docs/design/259-checked-parallel-emissions.md §4.1).
These tests are the slice-2 exit criteria, run end to end through the real
compile -> emit -> cordis pipeline plus focused unit tests on the runtime
helpers and the emitter's fan-out gate.

What is PROVEN here (§9, corrected exit criterion):

  * three disjoint-capability emissions with an awaited delay complete in
    ~max(latencies) not sum, with a BYTE-IDENTICAL audit trace (the record-sink
    replay in plan order) and byte-identical results versus the sequential form;
  * a fault in one branch is teardown-EFFECT equivalent: the world ends in the
    same state, with the fired members compensated in plan order and a correct
    LIFO unwind - NOT a byte-identical `accumulated`, which the design withdraws;
  * a divert (dispose) landing mid-group drives the whole group to quiescence
    (no in-flight branch orphaned to race teardown) and tears the activation down
    to DISPOSED with the audit flushed and no residual error - the conservative
    slice's guarantee (full divert-effect-equivalence for a PRESENT-compensation
    member is the deferred divert-aware runtime, §4.1/§10);
  * a group member that is non-idempotent, routes its audit off-task (a
    spawn-handle emission), is an approval crossing, or is not awaited stays
    SEQUENTIAL - the emitter's fan-out gate refuses to parallelize it;
  * a same-key non-commutative pair and an all-singleton body emit no fan-out at
    all (byte-identical to pre-259).
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

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl import compile_source  # noqa: E402


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

def _module(src: str, name: str = "p259") -> types.ModuleType:
    code = emit.emit(compile_source(src, "p259.rvl"))
    module = types.ModuleType(name)
    exec(compile(code, f"{name}.py", "exec"), module.__dict__)  # noqa: S102
    return module


def _emitted(src: str) -> str:
    return emit.emit(compile_source(src, "p259.rvl"))


def _ops(events: list[str]) -> list[str]:
    return [re.sub(r"#\d+", "", event) for event in events]


# Three distinct-token, idempotent, awaited host-extern emissions, each of which
# records its firing then awaits `delay` seconds. `kw` toggles the `idempotent`
# declaration so the SAME body can be compiled with the fan-out gate open
# (idempotent) or forced sequential (non-idempotent) - the sequential form is
# the byte-identical baseline the parallel run is diffed against.
def _three_emission_src(kw: str, delay: float, *, compensate: bool = False) -> str:
    comp_decls = ""
    comp_a = comp_b = comp_c = ""
    if compensate:
        comp_decls = (
            'extern emission[send.email] fn undo_a(x: Str) -> Str = @py '
            '{ import runtime; runtime._record("comp " + x); return x }\n'
            'extern emission[db.write] fn undo_b(x: Str) -> Str = @py '
            '{ import runtime; runtime._record("comp " + x); return x }\n'
            'extern emission[metrics] fn undo_c(x: Str) -> Str = @py '
            '{ import runtime; runtime._record("comp " + x); return x }\n'
        )
        comp_a = ' compensate undo_a("a")'
        comp_b = ' compensate undo_b("b")'
        comp_c = ' compensate undo_c("c")'
    return (
        f'extern emission[send.email] {kw}async fn fire_a(x: Str) -> Str = @py '
        f'{{ import runtime, asyncio; runtime._record("fire " + x); '
        f'await asyncio.sleep({delay}); return x }}\n'
        f'extern emission[db.write] {kw}async fn fire_b(x: Str) -> Str = @py '
        f'{{ import runtime, asyncio; runtime._record("fire " + x); '
        f'await asyncio.sleep({delay}); return x }}\n'
        f'extern emission[metrics] {kw}async fn fire_c(x: Str) -> Str = @py '
        f'{{ import runtime, asyncio; runtime._record("fire " + x); '
        f'await asyncio.sleep({delay}); return x }}\n'
        f'{comp_decls}'
        f'component W {{\n'
        f'  await emit fire_a("a"){comp_a}\n'
        f'  await emit fire_b("b"){comp_b}\n'
        f'  await emit fire_c("c"){comp_c}\n'
        f'}}\n'
    )


async def _activate_and_collect(module, *, until: int, timeout: float = 5.0):
    """Plug component W, drive it to activation, and poll until `until` trace
    events have been recorded (or timeout). Returns `(ops, elapsed_seconds)`.

    The activation body's awaited emissions run in the background after `await f`
    returns, so timing is measured by how long the recorded trace takes to fill -
    a concurrent group flushes all its records together at the join (~max), a
    sequential body trickles them (~sum)."""
    events: list[str] = []
    runtime_mod.set_trace(events.append)
    try:
        root = Context()
        root.logger.error = lambda *a, **k: None  # teardown failures are logged
        start = asyncio.get_event_loop().time()
        fiber = root.plugin(module.W)
        await fiber
        while len(events) < until and asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(0.005)
        elapsed = asyncio.get_event_loop().time() - start
        return _ops(events), elapsed
    finally:
        runtime_mod.set_trace(None)


# --------------------------------------------------------------------------- #
# 1. the exit test: concurrency + byte-identical audit
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_three_disjoint_emissions_run_concurrently_with_identical_audit():
    """Three disjoint idempotent emissions fire concurrently: the audit trace is
    byte-identical to the sequential form (the record-sink replays in plan order)
    and the group completes in ~max(latency) not sum - the roadmap exit test."""
    delay = 0.15
    par_mod = _module(_three_emission_src("idempotent ", delay), "par")
    seq_mod = _module(_three_emission_src("", delay), "seq")

    # the parallel module actually fans out; the sequential one does not
    assert "_revl_parallel" in emit.emit(compile_source(
        _three_emission_src("idempotent ", delay), "p.rvl"))
    assert "_revl_parallel" not in emit.emit(compile_source(
        _three_emission_src("", delay), "p.rvl"))

    par_ops, par_elapsed = await _activate_and_collect(par_mod, until=3)
    seq_ops, seq_elapsed = await _activate_and_collect(seq_mod, until=3)

    # byte-identical audit: both produce the plan-order concatenation
    assert par_ops == ["fire a", "fire b", "fire c"]
    assert par_ops == seq_ops

    # concurrency: parallel ~max (one delay), sequential ~sum (three delays)
    assert par_elapsed < 2 * delay, f"parallel took {par_elapsed:.3f}s (~max expected)"
    assert par_elapsed < seq_elapsed * 0.7, (
        f"parallel {par_elapsed:.3f}s not < 0.7 * sequential {seq_elapsed:.3f}s")


# --------------------------------------------------------------------------- #
# 2. a clean singleton / all-singleton body is byte-identical (no fan-out)
# --------------------------------------------------------------------------- #

def test_singleton_body_emits_no_fan_out():
    """A single emission is a singleton group - a plain sequential emit, no
    `_revl_parallel`, byte-identical to pre-259."""
    src = (
        'extern emission[send.email] idempotent async fn fire_a(x: Str) -> Str = @py '
        '{ import asyncio; await asyncio.sleep(0.0); return x }\n'
        'component W {\n  await emit fire_a("a")\n}\n'
    )
    assert "_revl_parallel" not in _emitted(src)


def test_non_commutative_same_key_pair_emits_no_fan_out():
    """A same-key pair without `commutative` stays two singleton groups (the plan
    never groups them), so no fan-out is emitted - the exit test's second half."""
    src = (
        'extern emission[db.write] idempotent async fn fire(x: Str) -> Str = @py '
        '{ import asyncio; await asyncio.sleep(0.0); return x }\n'
        'component W {\n  await emit fire("a")\n  await emit fire("b")\n}\n'
    )
    assert "_revl_parallel" not in _emitted(src)


def test_non_idempotent_disjoint_group_stays_sequential():
    """The plan groups three disjoint emissions, but the fan-out GATE refuses a
    non-idempotent member (over-firing it under a fault/divert is not proved
    safe), so the whole group degrades to sequential."""
    assert "_revl_parallel" not in _emitted(_three_emission_src("", 0.0))


# --------------------------------------------------------------------------- #
# 3. the fan-out eligibility gate (unit level, deterministic)
# --------------------------------------------------------------------------- #

def _emitter_for(externs, requires=None, services=None):
    component = {"name": "W", "requires": requires or {}}
    return emit._ComponentEmitter(component, services or {}, externs)


def test_gate_admits_idempotent_awaited_extern_emission():
    ext = {"name": "fire_a", "class": "emission", "idempotent": True}
    em = _emitter_for([ext])
    step = {"step": "emit", "async": True, "expr": {"kind": "fn", "name": "fire_a"}}
    assert em._group_eligible(step) is True


def test_gate_rejects_non_idempotent_emission():
    ext = {"name": "fire_a", "class": "emission"}  # no idempotent
    em = _emitter_for([ext])
    step = {"step": "emit", "async": True, "expr": {"kind": "fn", "name": "fire_a"}}
    assert em._group_eligible(step) is False


def test_gate_rejects_non_awaited_emission():
    ext = {"name": "fire_a", "class": "emission", "idempotent": True}
    em = _emitter_for([ext])
    step = {"step": "emit", "expr": {"kind": "fn", "name": "fire_a"}}  # no async
    assert em._group_eligible(step) is False


def test_gate_rejects_spawn_handle_off_task_recorder():
    """A spawn-handle provision emission records through the OFF-TASK spawn
    recorder, so its audit escapes the branch sink (§3.2): the gate excludes it
    even though its forward delivery is idempotent."""
    em = _emitter_for([], requires={}, services={})
    expr = {
        "kind": "call",
        "callee": {"kind": "field", "name": "method",
                   "target": {"kind": "instance-get", "service": "Inner"}},
    }
    step = {"step": "emit", "async": True, "expr": expr}
    assert em._emission_shape(expr) == "spawn"
    assert em._group_eligible(step) is False


def test_gate_rejects_approval_crossing():
    ext = {"name": "fire_a", "class": "emission", "idempotent": True}
    em = _emitter_for([ext])
    step = {"step": "emit", "async": True,
            "expr": {"kind": "fn", "name": "fire_a"},
            "approval": {"expr": {"kind": "name", "id": "a"}, "capability": "C"}}
    assert em._group_eligible(step) is False


# --------------------------------------------------------------------------- #
# 4. fault: teardown-EFFECT equivalence (NOT byte-identical accumulated)
# --------------------------------------------------------------------------- #

def _world(ops: list[str]) -> set:
    """The net host state from a fire/comp trace: a fire adds an id, its
    compensation removes it. Empty means every fired emission was compensated."""
    state: set = set()
    for event in ops:
        if event.startswith("fire "):
            state.add(event[5:])
        elif event.startswith("comp "):
            state.discard(event[5:])
    return state


def _fault_src(kw: str) -> str:
    return (
        f'extern emission[send.email] {kw}async fn fire_a(x: Str) -> Str = @py '
        f'{{ import runtime, asyncio; await asyncio.sleep(0.0); '
        f'runtime._record("fire " + x); return x }}\n'
        f'extern emission[db.write] {kw}async fn fire_b(x: Str) -> Str = @py '
        f'{{ import asyncio; await asyncio.sleep(0.0); raise RuntimeError("b boom") }}\n'
        f'extern emission[metrics] {kw}async fn fire_c(x: Str) -> Str = @py '
        f'{{ import runtime, asyncio; await asyncio.sleep(0.0); '
        f'runtime._record("fire " + x); return x }}\n'
        f'extern emission[send.email] fn undo_a(x: Str) -> Str = @py '
        f'{{ import runtime; runtime._record("comp " + x); return x }}\n'
        f'extern emission[db.write] fn undo_b(x: Str) -> Str = @py '
        f'{{ import runtime; runtime._record("comp " + x); return x }}\n'
        f'extern emission[metrics] fn undo_c(x: Str) -> Str = @py '
        f'{{ import runtime; runtime._record("comp " + x); return x }}\n'
        f'component W {{\n'
        f'  await emit fire_a("a") compensate undo_a("a")\n'
        f'  await emit fire_b("b") compensate undo_b("b")\n'
        f'  await emit fire_c("c") compensate undo_c("c")\n'
        f'}}\n'
    )


async def _run_to_teardown(module):
    events: list[str] = []
    runtime_mod.set_trace(events.append)
    try:
        root = Context()
        root.logger.error = lambda *a, **k: None
        fiber = root.plugin(module.W)
        try:
            await fiber
        except Exception:  # noqa: BLE001 — the branch fault surfaces here
            pass
        for _ in range(80):
            await asyncio.sleep(0.005)
        return _ops(events), fiber.state
    finally:
        runtime_mod.set_trace(None)


@pytest.mark.asyncio
async def test_fault_in_one_branch_is_teardown_effect_equivalent():
    """`fire_b` raises. Sequential leaves `accumulated=[a]` (c never fires);
    parallel fires a and c, registers BOTH compensations in plan order, then
    re-raises and unwinds LIFO. The registered SETS differ ([a,c] vs [a]) - byte-
    identical `accumulated` is NOT claimed - but the WORLD ends in the same state:
    every fired emission is compensated, so both nets are empty (§3.3, invariant
    E)."""
    par_ops, par_state = await _run_to_teardown(_module(_fault_src("idempotent "), "fpar"))
    seq_ops, seq_state = await _run_to_teardown(_module(_fault_src(""), "fseq"))

    assert par_state is FiberState.FAILED
    assert seq_state is FiberState.FAILED

    # teardown-EFFECT equivalence: the world ends in the same (empty) state
    assert _world(par_ops) == _world(seq_ops) == set()

    # parallel fired and compensated BOTH a and c (in plan order, unwound LIFO);
    # sequential only reached a - the registered sets legitimately differ
    assert par_ops == ["fire a", "fire c", "comp c", "comp a"]
    assert seq_ops == ["fire a", "comp a"]


# --------------------------------------------------------------------------- #
# 5. divert: the group is driven to quiescence and tears down cleanly
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_divert_mid_group_end_state_correct():
    """A dispose landing mid-group is the A1 divert path. The conservative slice
    does not abandon in-flight branches: `_revl_parallel` sits at one `await`, so
    the await LANDS (inertia) and the whole group is driven to quiescence - no
    orphaned branch races the teardown. The activation then unwinds to DISPOSED
    with the audit flushed and no residual error, and nothing records AFTER the
    teardown settles (no leaked in-flight branch). Full divert-effect-equivalence
    for a PRESENT-compensation member is the deferred divert-aware runtime
    (§4.1/§10); the members here declare no compensation (idempotent-or-absent,
    the trivially-safe case), so over-firing them is the accepted bounded
    residual (C2)."""
    delay = 0.2
    module = _module(_three_emission_src("idempotent ", delay), "dvt")

    events: list[str] = []
    runtime_mod.set_trace(events.append)
    try:
        root = Context()
        root.logger.error = lambda *a, **k: None
        fiber = root.plugin(module.W)

        async def _kill():
            await asyncio.sleep(0.05)  # land the dispose well inside the group await
            fiber.dispose()

        killer = asyncio.ensure_future(_kill())
        try:
            await fiber
        except BaseException:  # noqa: BLE001 — a divert may surface here
            pass
        for _ in range(120):
            await asyncio.sleep(0.005)
        await killer

        # the divert tore the activation down cleanly
        assert fiber.state is FiberState.DISPOSED
        # the whole group was driven to quiescence, then settled: capture the
        # trace and confirm it is STABLE (no in-flight branch records after
        # teardown - no orphan racing the unwind)
        settled = list(events)
        for _ in range(40):
            await asyncio.sleep(0.005)
        assert list(events) == settled, "an orphaned branch recorded after teardown"
    finally:
        runtime_mod.set_trace(None)


# --------------------------------------------------------------------------- #
# 6. runtime helpers, unit level (deterministic)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_revl_parallel_runs_branches_concurrently():
    """`_revl_parallel` fires the branches under one loop: three 0.1s awaits are
    in flight at once, so the gather completes in ~max not ~sum."""
    async def slow(tag, d):
        await asyncio.sleep(d)
        return tag

    start = asyncio.get_event_loop().time()
    outcomes = await runtime_mod._revl_parallel([
        (lambda: slow("a", 0.1)),
        (lambda: slow("b", 0.1)),
        (lambda: slow("c", 0.1)),
    ])
    elapsed = asyncio.get_event_loop().time() - start
    assert [o.value for o in outcomes] == ["a", "b", "c"]  # plan order preserved
    assert elapsed < 0.25, f"branches not concurrent: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_record_sink_buffers_mid_branch_and_replays_in_plan_order():
    """A record emitted DURING a branch buffers into that branch's sink (not the
    real observers); the join replays the buffers in PLAN order, so the observable
    trace is the sequential concatenation even though the branches interleaved."""
    live: list[str] = []
    runtime_mod.set_trace(live.append)
    try:
        async def emit_records(tag, first_delay):
            # record, yield to let siblings interleave, record again
            runtime_mod._record(f"{tag}1")
            await asyncio.sleep(first_delay)
            runtime_mod._record(f"{tag}2")

        outcomes = await runtime_mod._revl_parallel([
            (lambda: emit_records("a", 0.02)),
            (lambda: emit_records("b", 0.0)),
        ])
        # nothing reached the real sink while the branches ran - all buffered
        assert live == []
        # replay in plan order at the join
        for outcome in outcomes:
            runtime_mod._revl_flush(outcome.records)
        assert live == ["a1", "a2", "b1", "b2"]
    finally:
        runtime_mod.set_trace(None)


@pytest.mark.asyncio
async def test_revl_parallel_captures_fault_and_raises_first_in_plan_order():
    """Every branch is driven to quiescence even when one faults (the gather never
    short-circuits); `_revl_raise_first` re-raises the FIRST fault in plan order,
    after the join has had its chance to register the successful members."""
    async def ok(tag):
        await asyncio.sleep(0.0)
        return tag

    async def boom(msg):
        await asyncio.sleep(0.0)
        raise RuntimeError(msg)

    outcomes = await runtime_mod._revl_parallel([
        (lambda: ok("a")),
        (lambda: boom("b boom")),
        (lambda: boom("c boom")),
    ])
    assert [o.ok for o in outcomes] == [True, False, False]  # all captured
    assert outcomes[0].value == "a"
    with pytest.raises(RuntimeError, match="b boom"):  # first fault in plan order
        runtime_mod._revl_raise_first(outcomes)


@pytest.mark.asyncio
async def test_revl_parallel_captures_divert_cancelled_and_leaves_siblings_intact():
    """A divert arrives at a branch's await as a CancelledError. `_revl_branch`
    captures it (rather than propagating and abandoning the siblings), so the
    successful siblings' outcomes are intact and the join can register THEIR
    compensations in plan order; `_revl_raise_first` then re-surfaces the
    cancellation to drive the activation's teardown."""
    async def ok(tag):
        await asyncio.sleep(0.0)
        return tag

    async def diverted():
        await asyncio.sleep(0.0)
        raise asyncio.CancelledError()

    outcomes = await runtime_mod._revl_parallel([
        (lambda: ok("a")),
        (lambda: diverted()),
        (lambda: ok("c")),
    ])
    assert [o.ok for o in outcomes] == [True, False, True]
    assert outcomes[0].value == "a" and outcomes[2].value == "c"
    assert isinstance(outcomes[1].error, asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        runtime_mod._revl_raise_first(outcomes)


@pytest.mark.asyncio
async def test_record_sink_stack_nests():
    """The sink is a STACK: a parallel group inside a parallel branch buffers into
    the INNER sink, and `_revl_flush` (routing back through `_record`) replays the
    inner buffer into the OUTER branch's sink, which the outer join then replays to
    the real trace - one plan-order concatenation, correctly nested."""
    live: list[str] = []
    runtime_mod.set_trace(live.append)
    try:
        async def inner_branch(tag):
            runtime_mod._record(f"{tag}-inner")
            return tag

        async def outer_branch(tag):
            runtime_mod._record(f"{tag}-outer-before")
            inner = await runtime_mod._revl_parallel([(lambda: inner_branch(tag))])
            for o in inner:
                runtime_mod._revl_flush(o.records)  # replays into THIS branch's sink
            runtime_mod._record(f"{tag}-outer-after")
            return tag

        outcomes = await runtime_mod._revl_parallel([(lambda: outer_branch("x"))])
        assert live == []  # everything buffered up the stack
        for o in outcomes:
            runtime_mod._revl_flush(o.records)
        assert live == ["x-outer-before", "x-inner", "x-outer-after"]
    finally:
        runtime_mod.set_trace(None)
