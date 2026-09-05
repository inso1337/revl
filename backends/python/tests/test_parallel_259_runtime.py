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
  * a divert (a deadline) landing at an emission's in-flight await is FULLY
    teardown-EFFECT equivalent for the restricted set - every fired member is
    compensated in plan order, the world ends in the same state - because the
    tightened gate admits only members whose compensation is idempotent-or-absent;
  * an external dispose landing at the group's own await drives the whole group
    to quiescence (no in-flight branch orphaned) and tears the activation down to
    DISPOSED with no residual error; the tightened gate guarantees no member
    carries a real non-idempotent compensation to orphan, so the only residual is
    a bounded idempotent-FORWARD over-delivery (the deferred divert-aware runtime,
    §4.1/§10);
  * the CRITICAL restriction: a member is admitted to a group only if its
    COMPENSATION is idempotent-or-absent (NOT merely idempotent forward delivery);
    a member with a present, non-idempotent compensation stays SEQUENTIAL, as do
    non-idempotent-forward, off-task (spawn-handle), approval, and non-awaited
    emissions;
  * a same-key non-commutative pair and an all-singleton body emit no fan-out at
    all (byte-identical to pre-259).
"""

from __future__ import annotations

import asyncio
import itertools
import pathlib
import re
import sys
import tempfile
import types

import pytest

from cordis import Context
from cordis.fiber import FiberState

import emit
import runtime as runtime_mod

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl.compiler import compile_files  # noqa: E402


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
#
# The recording emissions below observe the runtime's record-sink (item 259
# slice 2 buffers a mid-branch `_record` and replays it at the join in plan
# order). Reaching `runtime._record` from a user `@py` body by inlining
# `import runtime` is exactly what the backend-import confinement refuses
# (#302): a user composition must not turn the backend search path into an
# ambient host dependency. The sanctioned route is `@py ref` — the fixtures
# name a small TRUSTED host module (`_host_module` below) that imports the
# runtime, and the compiler jails the ref to the program's own root tree. Each
# compile gets a UNIQUE host-module name so a per-compile `DELAY` never collides
# in `sys.modules` across the many programs this file builds in one process.

_HOST_PLACEHOLDER = "__REC_HOST__"
_host_counter = itertools.count()


def _host_module(delay: float) -> str:
    """A trusted host module the fixtures reference with `@py ref`.

    `fire` records its firing then awaits `delay`; `boom` models a faulting
    member; `unfire` records a compensation. All three call `runtime._record`,
    which is legitimate here because this file is TRUSTED host code declared
    through a ref, not an inline user `@py` body (#302)."""
    return (
        '"""Trusted host recording helpers for the item-259 fixtures."""\n'
        "import asyncio\n"
        "import runtime\n"
        "\n"
        f"DELAY = {delay!r}\n"
        "\n"
        "\n"
        "async def fire(x):\n"
        '    runtime._record("fire " + x)\n'
        "    await asyncio.sleep(DELAY)\n"
        "    return x\n"
        "\n"
        "\n"
        "async def boom(x):\n"
        "    await asyncio.sleep(0.0)\n"
        '    raise RuntimeError(x + " boom")\n'
        "\n"
        "\n"
        "def unfire(x):\n"
        '    runtime._record("comp " + x)\n'
        "    return x\n"
    )


def _write_program(src: str, delay: float) -> str:
    """Write a program (and its trusted host module) to a fresh directory and
    return the path to `main.rvl`. The host module gets a unique name so its
    `DELAY` is isolated per compile even though every program imports it."""
    host_name = f"rec_host_{next(_host_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl259_"))
    (directory / f"{host_name}.py").write_text(_host_module(delay), encoding="utf-8")
    resolved = src.replace(_HOST_PLACEHOLDER, host_name)
    main = directory / "main.rvl"
    main.write_text(resolved, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return str(main)


def _module(src: str, name: str = "p259", delay: float = 0.0) -> types.ModuleType:
    code = emit.emit(compile_files([_write_program(src, delay)]))
    module = types.ModuleType(name)
    exec(compile(code, f"{name}.py", "exec"), module.__dict__)  # noqa: S102
    return module


def _emitted(src: str, delay: float = 0.0) -> str:
    return emit.emit(compile_files([_write_program(src, delay)]))


def _ops(events: list[str]) -> list[str]:
    return [re.sub(r"#\d+", "", event) for event in events]


# Three distinct-token, idempotent, awaited host-extern emissions, each of which
# records its firing then awaits `delay` seconds. `kw` toggles the `idempotent`
# declaration so the SAME body can be compiled with the fan-out gate open
# (idempotent) or forced sequential (non-idempotent) - the sequential form is
# the byte-identical baseline the parallel run is diffed against.
def _three_emission_src(kw: str, *, compensate: bool = False) -> str:
    # The recording body lives in the trusted host module (`fire`/`unfire`),
    # declared with `@py ref` so the fixture reaches `runtime._record` without
    # an inline backend import (#302). The firing latency is the host module's
    # `DELAY`, baked in per compile by `_write_program` (passed to `_module`).
    comp_decls = ""
    comp_a = comp_b = comp_c = ""
    if compensate:
        comp_decls = (
            f'extern emission[send.email] fn undo_a(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
            f'extern emission[db.write] fn undo_b(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
            f'extern emission[metrics] fn undo_c(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
        )
        comp_a = ' compensate undo_a("a")'
        comp_b = ' compensate undo_b("b")'
        comp_c = ' compensate undo_c("c")'
    return (
        f'extern emission[send.email] {kw}async fn fire_a(x: Str) -> Str = @py ref fire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[db.write] {kw}async fn fire_b(x: Str) -> Str = @py ref fire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[metrics] {kw}async fn fire_c(x: Str) -> Str = @py ref fire from "{_HOST_PLACEHOLDER}.py"\n'
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
    par_mod = _module(_three_emission_src("idempotent "), "par", delay)
    seq_mod = _module(_three_emission_src(""), "seq", delay)

    # the parallel module actually fans out; the sequential one does not
    assert "_revl_parallel" in _emitted(_three_emission_src("idempotent "))
    assert "_revl_parallel" not in _emitted(_three_emission_src(""))

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
    assert "_revl_parallel" not in _emitted(_three_emission_src(""))


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


def test_gate_rejects_present_non_idempotent_compensation():
    """The CRITICAL restriction (§3.3/§5/§8): a member with idempotent FORWARD
    delivery but a PRESENT, non-idempotent COMPENSATION must stay sequential -
    NOT merely idempotent forward delivery. This is the exact gap that would let a
    divert leave a fired member with a real (non-idempotent) compensation it can
    never run at the interrupted join."""
    fire = {"name": "fire_a", "class": "emission", "idempotent": True}
    undo = {"name": "undo_a", "class": "emission"}  # present but NOT idempotent
    em = _emitter_for([fire, undo])
    step = {"step": "emit", "async": True,
            "expr": {"kind": "fn", "name": "fire_a"},
            "compensate": {"kind": "fn", "name": "undo_a"}}
    assert em._compensation_idempotent_or_absent(step) is False
    assert em._group_eligible(step) is False


def test_gate_admits_present_idempotent_compensation():
    """A present compensation is admissible when its operation is DECLARED
    idempotent (item 44/309) - over-firing the member and then running (or, at a
    divert boundary, skipping) an idempotent compensation is world-state-safe."""
    fire = {"name": "fire_a", "class": "emission", "idempotent": True}
    undo = {"name": "undo_a", "class": "emission", "idempotent": True}
    em = _emitter_for([fire, undo])
    step = {"step": "emit", "async": True,
            "expr": {"kind": "fn", "name": "fire_a"},
            "compensate": {"kind": "fn", "name": "undo_a"}}
    assert em._compensation_idempotent_or_absent(step) is True
    assert em._group_eligible(step) is True


def test_gate_admits_absent_compensation():
    """No `compensate` clause is the trivially-safe idempotent-or-absent case."""
    fire = {"name": "fire_a", "class": "emission", "idempotent": True}
    em = _emitter_for([fire])
    step = {"step": "emit", "async": True, "expr": {"kind": "fn", "name": "fire_a"}}
    assert em._compensation_idempotent_or_absent(step) is True
    assert em._group_eligible(step) is True


def test_present_non_idempotent_compensation_stays_sequential_end_to_end():
    """End to end: three disjoint idempotent-forward emissions each carrying a
    PRESENT non-idempotent compensation emit NO fan-out (the whole group degrades
    to sequential), so a divert can never orphan one of their real compensations.
    Declaring the SAME compensations `idempotent` restores the fan-out."""
    def src(comp_kw: str) -> str:
        return (
            'extern emission[send.email] idempotent async fn fire_a(x: Str) -> Str '
            '= @py { import asyncio; await asyncio.sleep(0.0); return x }\n'
            'extern emission[db.write] idempotent async fn fire_b(x: Str) -> Str '
            '= @py { import asyncio; await asyncio.sleep(0.0); return x }\n'
            'extern emission[metrics] idempotent async fn fire_c(x: Str) -> Str '
            '= @py { import asyncio; await asyncio.sleep(0.0); return x }\n'
            f'extern emission[send.email] {comp_kw}fn undo_a(x: Str) -> Str = @py {{ return x }}\n'
            f'extern emission[db.write] {comp_kw}fn undo_b(x: Str) -> Str = @py {{ return x }}\n'
            f'extern emission[metrics] {comp_kw}fn undo_c(x: Str) -> Str = @py {{ return x }}\n'
            'component W {\n'
            '  await emit fire_a("a") compensate undo_a("a")\n'
            '  await emit fire_b("b") compensate undo_b("b")\n'
            '  await emit fire_c("c") compensate undo_c("c")\n'
            '}\n'
        )
    assert "_revl_parallel" not in _emitted(src(""))            # non-idempotent comp
    assert "_revl_parallel" in _emitted(src("idempotent "))     # idempotent comp


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
    # `fire_b` faults (the trusted host `boom`); `fire_a`/`fire_c` record and
    # return (`fire`), and the compensations record through `undo`. All bodies
    # are `@py ref`s into the trusted host module, never inline backend imports.
    return (
        f'extern emission[send.email] {kw}async fn fire_a(x: Str) -> Str = @py ref fire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[db.write] {kw}async fn fire_b(x: Str) -> Str = @py ref boom from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[metrics] {kw}async fn fire_c(x: Str) -> Str = @py ref fire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[send.email] idempotent fn undo_a(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[db.write] idempotent fn undo_b(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[metrics] idempotent fn undo_c(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
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
        except Exception:  # noqa: BLE001 - the branch fault surfaces here
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
# 5. divert: teardown-EFFECT equivalence for the restricted set
# --------------------------------------------------------------------------- #

def _divert_src(kw: str) -> str:
    """A DEADLINE landing at one emission's in-flight await (a named A1 divert
    trigger, §3.3): `fire_b` awaits, then the deadline fires and its await raises.
    Sequential SKIPS the remaining emission (c never fires); parallel fires a and
    c and, because the join completes, compensates BOTH in plan order. All three
    compensations are declared `idempotent`, so the members satisfy the tightened
    gate (compensation idempotent-or-absent)."""
    # Same shape as `_fault_src`: the deadline is modelled by the trusted host
    # `boom` raising at `fire_b`'s await. Bodies are `@py ref`s (#302).
    return (
        f'extern emission[send.email] {kw}async fn fire_a(x: Str) -> Str = @py ref fire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[db.write] {kw}async fn fire_b(x: Str) -> Str = @py ref boom from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[metrics] {kw}async fn fire_c(x: Str) -> Str = @py ref fire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[send.email] idempotent fn undo_a(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[db.write] idempotent fn undo_b(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
        f'extern emission[metrics] idempotent fn undo_c(x: Str) -> Str = @py ref unfire from "{_HOST_PLACEHOLDER}.py"\n'
        f'component W {{\n'
        f'  await emit fire_a("a") compensate undo_a("a")\n'
        f'  await emit fire_b("b") compensate undo_b("b")\n'
        f'  await emit fire_c("c") compensate undo_c("c")\n'
        f'}}\n'
    )


@pytest.mark.asyncio
async def test_divert_at_member_await_is_teardown_effect_equivalent():
    """A divert (a deadline) landing at an emission's in-flight await drives the
    activation's teardown; because the tightened gate admits only members whose
    compensation is idempotent-or-absent, every fired member is compensated in
    plan order and the WORLD ends in the same state a sequential (also-diverted)
    run would leave it in - FULLY teardown-EFFECT equivalent for the restricted
    set. The registered sets legitimately differ ([a,c] vs [a]); byte-identical
    `accumulated` is not claimed (§3.3, invariant E)."""
    par_ops, par_state = await _run_to_teardown(_module(_divert_src("idempotent "), "dpar"))
    seq_ops, seq_state = await _run_to_teardown(_module(_divert_src(""), "dseq"))

    assert par_state is FiberState.FAILED
    assert seq_state is FiberState.FAILED

    # full teardown-EFFECT equivalence: the world ends empty in BOTH
    assert _world(par_ops) == _world(seq_ops) == set()
    # parallel compensated both fired members in plan order, unwound LIFO
    assert par_ops == ["fire a", "fire c", "comp c", "comp a"]
    assert seq_ops == ["fire a", "comp a"]


@pytest.mark.asyncio
async def test_external_dispose_mid_group_tears_down_cleanly():
    """An external dispose landing at the group's own `await` is the other A1
    divert shape: cordis's inertia lands the await (the whole group fires to
    quiescence - no in-flight branch orphaned to race teardown), then the close
    interrupts the plan-order join. The conservative slice guarantees: (1) the
    activation unwinds to DISPOSED with no residual error, (2) nothing records
    AFTER teardown settles (no orphan), and (3) the tightened gate means NO member
    of the group carries a real non-idempotent compensation, so a divert can never
    leave one un-run. The residual - an idempotent-FORWARD over-delivery of the
    members past the divert boundary - is bounded (within the declared count, G4)
    and is the design's accepted residual / deferred divert-aware runtime
    (§4.1/§10). The members here declare no compensation (the trivially-safe
    idempotent-or-absent case)."""
    delay = 0.2
    module = _module(_three_emission_src("idempotent "), "xdvt", delay)

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
        except BaseException:  # noqa: BLE001 - a divert may surface here
            pass
        for _ in range(120):
            await asyncio.sleep(0.005)
        await killer

        assert fiber.state is FiberState.DISPOSED
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
