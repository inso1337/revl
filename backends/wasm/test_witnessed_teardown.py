"""Witnessed-effects teardown on the wasm tier — roadmap item 243 Slice 2b.

Design: docs/design/teardown-contract.md (the merged bracket/witnessed/
compensation loop) and docs/design/243-witnessed-externs.md. The py reference
tier proved the `transactional` entry kind (backends/python/runtime.py,
`Frame`/`_Transactional`/`drain`); this is the wasm tier's own loop, built
against the same contract: three entry kinds over ACTIVATION-REGISTERED
entries (method-time compensation stays refused — the wasm accumulator is
fixed at activation, docs/wasm-capabilities.md), a two-phase abort, and a
commit path that discharges `transactional`/`compensation` entries while a
`bracket` still releases.

Two layers, mirroring the split `backends/wasm/emit.py` and
`backends/wasm/lifecycle.py` already draw:

* emission-level tests need no wasmtime — they assert on the compiled WAT
  text and on `EmitError` refusals (Float-in-witness, method-time
  compensation), the same "never silently degrade" contract every other
  wasm-tier refusal test in this directory checks;
* execution-level tests (gated by `pytest.importorskip("wasmtime", ...)`,
  the same pattern `test_spawn_exec.py`/`test_accessor_exec.py` use) run the
  compiled module on a live wasmtime instance and observe the three-way
  proof the task asks for: persist-on-commit, revert-on-abort, distinct from
  an ordinary acquire bracket — plus the two-phase compensation path and its
  first-party epoch-deadline bound (`lifecycle.drive_teardown`).

Every fixture below hand-writes its own `@wasm` extern bodies (raw WAT) at
FIXED, disjoint linear-memory addresses instead of going through `$alloc` —
these are test-only scratch cells, not language-level values, and picking
fixed addresses sidesteps needing a scratch `local` inside a `@wasm` body,
which `_emit_extern_func`'s header has no slot for.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BACKEND / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emitter():
    return _load("revl_wasm_emit_witnessed", "emit.py")


def _lifecycle():
    return _load("revl_wasm_lifecycle_witnessed", "lifecycle.py")


# ---------------------------------------------------------------------------
# fixtures: a witnessed extern (`mark`/`revert`), a bracket for contrast
# (`tick`/`untick`), an emission with an activation-registered compensation
# (`sendmsg`/`undosend`), and a bracket that always traps (`boom`/`noop`) —
# the abort trigger, standing in for a real mid-activation fault.
# ---------------------------------------------------------------------------

_EXTERNS = '''
type Bracket = { fd: Int }
extern acquire fn tick() -> Bracket
    undo untick(0)
    = @wasm { (i32.const 0) }
extern pure fn untick(r: Int) -> Unit
    = @wasm { (i32.store (i32.const 5000) (i32.add (i32.load (i32.const 5000)) (i32.const 1))) }

extern witnessed[t] fn mark(v: Int) -> Result[Int, Str]
    undo revert(result)
    = @wasm {
      (i32.store (i32.const 4096) (i32.const 0))
      (i64.store (i32.const 4104) (local.get $p_v))
      (i32.const 4096)
    }
extern pure fn revert(w: Int) -> Unit
    = @wasm {
      (i32.store (i32.const 5004) (i32.add (i32.load (i32.const 5004)) (i32.const 1)))
      (i64.store (i32.const 5008) (local.get $p_w))
    }

extern emission fn sendmsg(x: Int) -> Int compensate undosend(0) = @wasm { (local.get $p_x) }
extern pure fn undosend(x: Int) -> Unit
    = @wasm { (i32.store (i32.const 5020) (i32.add (i32.load (i32.const 5020)) (i32.const 1))) }

extern acquire fn boom() -> Bracket undo noop(0) = @wasm { unreachable }
extern pure fn noop(r: Int) -> Unit = @wasm { }
'''

_COMMIT_SRC = _EXTERNS + '''
component ProbeCommit {
  effect mark(7)
  effect tick() undo untick(0)
}
'''

_ABORT_SRC = _EXTERNS + '''
component ProbeAbort {
  effect mark(7)
  effect tick() undo untick(0)
  emit sendmsg(1) compensate undosend(1)
  effect boom() undo noop(0)
}
'''


def _mem_i32(store, exports, addr):
    return int.from_bytes(exports["memory"].read(store, addr, addr + 4), "little", signed=True)


def _mem_i64(store, exports, addr):
    return int.from_bytes(exports["memory"].read(store, addr, addr + 8), "little", signed=True)


def _run_to_completion_or_trap(wasmtime, store, exports):
    """Drive `activate_step` until it reports done, or a trap fires. Returns
    True iff a trap interrupted activation (the abort trigger, `boom`)."""
    for _ in range(16):
        try:
            if not exports["activate_step"](store):
                return False
        except wasmtime.Trap:
            return True
    raise AssertionError("activate_step did not terminate")  # pragma: no cover


# ---------------------------------------------------------------------------
# emission-level: no wasmtime needed
# ---------------------------------------------------------------------------

def test_witnessed_entry_compiles_to_a_transactional_dispatch_slot():
    """A witnessed acquisition's undo lands in `deactivate_step`'s ABORT
    chain only — never the commit chain — and the emitted teardown descriptor
    names it `transactional` (item 243's one entry-kind distinction, restated
    at the wasm tier: `docs/design/243-witnessed-externs.md`)."""
    wat = _emitter().emit(compile_source(_COMMIT_SRC, "t.rvl"))["ProbeCommit"]
    assert '(func (export "committed")' in wat
    assert '(func $deactivate_step (export "deactivate_step")' in wat
    descriptor = _lifecycle().parse_teardown_descriptor(wat)
    assert descriptor is not None
    assert descriptor["entries"] == [{"seq": 1, "entry": "transactional", "dispatch": 1}]
    # the bracket (`tick`) occupies the OTHER Phase-1 dispatch slot, but
    # carries no WAL row of its own (G5 infallible) — phase1Count counts it.
    assert descriptor["phase1Count"] == 2
    assert descriptor["phase2Count"] == 0


def test_compensation_entry_is_phase_two_only():
    wat = _emitter().emit(compile_source(_ABORT_SRC, "t.rvl"))["ProbeAbort"]
    descriptor = _lifecycle().parse_teardown_descriptor(wat)
    kinds = {e["entry"] for e in descriptor["entries"]}
    assert kinds == {"transactional", "compensation"}
    comp = next(e for e in descriptor["entries"] if e["entry"] == "compensation")
    # the compensation's dispatch position is >= phase1Count: strictly
    # Phase 2, after every bracket/transactional entry.
    assert comp["dispatch"] >= descriptor["phase1Count"]


def test_float_in_witness_is_refused_not_mis_emitted():
    """A witnessed extern's witness type goes through the SAME `_check_type`
    gate every other value on this tier does (item 301: the refusal has to
    recurse into every value position, not just the obvious ones) — a
    `Result[Float, ...]` witness is refused by name, never silently narrowed
    into an i32 slot that does not fit an f64."""
    src = '''
    extern witnessed[t] fn markf(v: Float) -> Result[Float, Str]
        undo revertf(result)
        = @wasm { (i32.const 0) }
    extern pure fn revertf(w: Float) -> Unit = @wasm { }
    component ProbeFloat {
      effect markf(1.5)
    }
    '''
    emitter = _emitter()
    with pytest.raises(emitter.EmitError, match="Float"):
        emitter.emit(compile_source(src, "t.rvl"))


def test_method_time_compensation_stays_refused():
    """item 301's soundness bar, restated for Slice 2b: the two-phase abort
    this slice adds is over ACTIVATION-REGISTERED entries only (docs/design/
    teardown-contract.md, the wasm row) — a compensation attached inside a
    provide-method body (method-time) must still be a hard `EmitError`, not
    silently admitted now that compensation entries are real. The wasm
    accumulator is fixed at activation; nothing in this slice lifts that."""
    src = '''
    service Bus { emission fn send(x: Int) -> Int }
    service S { emission fn f(x: Int) -> Int }
    component C requires bus: Bus provides s: S {
      provide s {
        fn f(x) {
          emit bus.send(x) compensate bus.send(0)
          return x
        }
      }
    }
    '''
    emitter = _emitter()
    with pytest.raises(emitter.EmitError, match="method-time compensation is not lowerable"):
        emitter.emit(compile_source(src, "t.rvl"))


# ---------------------------------------------------------------------------
# execution-level: live wasmtime, no cordis-wasm needed (first-party direct
# wasmtime driving — the same pattern test_spawn_exec.py/test_accessor_exec.py
# use for exec-level proofs)
# ---------------------------------------------------------------------------

def _instantiate(wasmtime, wat, *, epoch_interruption=False):
    cfg = wasmtime.Config()
    if epoch_interruption:
        cfg.epoch_interruption = True
    engine = wasmtime.Engine(cfg)
    module = wasmtime.Module(engine, wat)
    store = wasmtime.Store(engine)
    if epoch_interruption:
        # a huge deadline: activation itself never wants to be interrupted,
        # only the Phase-2 compensation calls `drive_teardown` arms per call.
        store.set_epoch_deadline(1_000_000)
    instance = wasmtime.Instance(store, module, [])
    return engine, store, instance.exports(store)


def test_commit_discharges_transactional_and_still_releases_bracket():
    """Persist-on-commit + distinct-from-acquire, in one scenario: a clean
    unload runs the bracket's release (as always) but DISCHARGES the
    witnessed entry — its undo never runs, and the mutation persists as the
    deliverable (docs/design/243-witnessed-externs.md's own contrast row)."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    wat = _emitter().emit(compile_source(_COMMIT_SRC, "t.rvl"))["ProbeCommit"]
    _engine, store, exports = _instantiate(wasmtime, wat)

    aborted = _run_to_completion_or_trap(wasmtime, store, exports)
    assert not aborted
    assert exports["committed"](store) == 1

    exports["deactivate"](store)
    assert _mem_i32(store, exports, 5000) == 1, "bracket must still release on a clean unload"
    assert _mem_i32(store, exports, 5004) == 0, "transactional must NOT replay on commit (discharge)"


def test_abort_replays_transactional_with_the_correct_witness():
    """Revert-on-abort: a mid-activation trap (after the mutation ran) leaves
    `committed() == 0`, and Phase 1 replays BOTH the bracket's release and the
    witnessed entry's declared undo — bound to the exact Ok witness the
    mutation returned, not a site-spelled placeholder."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    wat = _emitter().emit(compile_source(_ABORT_SRC, "t.rvl"))["ProbeAbort"]
    _engine, store, exports = _instantiate(wasmtime, wat)

    aborted = _run_to_completion_or_trap(wasmtime, store, exports)
    assert aborted, "the `boom` bracket's @wasm body is `unreachable` by design"
    assert exports["committed"](store) == 0

    exports["deactivate"](store)
    assert _mem_i32(store, exports, 5000) == 1, "bracket replays on abort too"
    assert _mem_i32(store, exports, 5004) == 1, "transactional replays on abort"
    assert _mem_i64(store, exports, 5008) == 7, "the witness threaded to undo is the Ok payload, unaltered"


def test_two_phase_compensation_runs_after_transactional_and_bracket():
    """a5b, the wasm tier's own instance: on abort, every Phase-1 entry
    (bracket + transactional) completes before the first Phase-2 compensation
    starts. `lifecycle.drive_teardown` (first-party, no cordis-wasm) drives
    the split and reports a clean residue when nothing faults."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    lifecycle = _lifecycle()
    wat = _emitter().emit(compile_source(_ABORT_SRC, "t.rvl"))["ProbeAbort"]
    engine, store, exports = _instantiate(wasmtime, wat, epoch_interruption=True)

    aborted = _run_to_completion_or_trap(wasmtime, store, exports)
    assert aborted

    result = lifecycle.drive_teardown(engine, store, exports, wat,
                                      phase2_per_call_ms=500, phase2_budget_ms=5000)
    assert result == {"clean": True, "outstanding": []}
    assert _mem_i32(store, exports, 5000) == 1  # bracket (Phase 1)
    assert _mem_i32(store, exports, 5004) == 1  # transactional (Phase 1)
    assert _mem_i32(store, exports, 5020) == 1  # compensation (Phase 2)


def test_epoch_deadline_bounds_a_hung_compensation():
    """The first-party epoch wiring: a Phase-2 compensation that spins past
    its per-call budget is cut off in flight, mid-guest-execution, and
    reported `compensation-residue` with `outcome: unknown` (the abandoned-
    in-flight shape the contract specifies) — never left to hang the abort."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    lifecycle = _lifecycle()
    src = '''
    type Bracket = { fd: Int }
    extern acquire fn boom() -> Bracket undo noop(0) = @wasm { unreachable }
    extern pure fn noop(r: Int) -> Unit = @wasm { }
    extern emission fn spinner(x: Int) -> Int compensate undospin(0) = @wasm { (local.get $p_x) }
    extern pure fn undospin(x: Int) -> Unit
        = @wasm {
          (local $i i32)
          (local.set $i (i32.const 0))
          (block $done
            (loop $l
              (br_if $done (i32.ge_u (local.get $i) (i32.const 2000000000)))
              (local.set $i (i32.add (local.get $i) (i32.const 1)))
              (br $l)))
        }
    component ProbeSlowCompensate {
      emit spinner(1) compensate undospin(0)
      effect boom() undo noop(0)
    }
    '''
    wat = _emitter().emit(compile_source(src, "t.rvl"))["ProbeSlowCompensate"]
    engine, store, exports = _instantiate(wasmtime, wat, epoch_interruption=True)
    aborted = _run_to_completion_or_trap(wasmtime, store, exports)
    assert aborted

    result = lifecycle.drive_teardown(engine, store, exports, wat, phase2_per_call_ms=100)
    assert not result["clean"]
    (record,) = result["outstanding"]
    assert record["kind"] == "compensation-residue"
    assert record["outcome"] == "unknown"
    assert record["error"]["type"] == "TrapCode.INTERRUPT"


def test_phase_two_budget_records_every_skip_none_silently_dropped():
    """The between-compensation check (normative on every tier, docs/design/
    teardown-contract.md): a budget that is already exhausted before Phase 2
    starts records the compensation as skipped — `attemptedFlag: False`,
    `error.type: "deadline-expired"` — rather than silently omitting it."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    lifecycle = _lifecycle()
    wat = _emitter().emit(compile_source(_ABORT_SRC, "t.rvl"))["ProbeAbort"]
    engine, store, exports = _instantiate(wasmtime, wat)
    aborted = _run_to_completion_or_trap(wasmtime, store, exports)
    assert aborted

    result = lifecycle.drive_teardown(engine, store, exports, wat, phase2_budget_ms=0)
    (record,) = result["outstanding"]
    assert record["kind"] == "compensation-residue"
    assert record["attemptedFlag"] is False
    assert record["error"] == {"type": "deadline-expired", "message": "phase-2 budget exhausted"}
    # the compensation itself must NOT have run — the budget check happens
    # strictly before the call, so no side effect landed.
    assert _mem_i32(store, exports, 5020) == 0
