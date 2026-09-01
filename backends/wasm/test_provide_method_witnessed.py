"""Witnessed effects in a PROVIDE-METHOD body on the wasm tier — roadmap item
324, the wasm half of item 318's H1 gate.

Design: docs/design/243-witnessed-externs.md, docs/design/teardown-contract.md
(the wasm preemption row: "activation-registered-only"). The py reference tier
proved the per-tool-call closed loop in tests/test_provide_method_witnessed.py
against a live cordis-py composition; this is the same proof restated on this
tier's own machinery — a first-party wasmtime instance, no cordis-wasm needed
(the same pattern test_witnessed_teardown.py / test_spawn_exec.py use).

243/244/Slice-2b proved a witnessed effect in the component ACTIVATION body,
whose accumulator is fixed at compile time. The real agent use case is a fs
mutation that fires from a provide-METHOD, PER TOOL CALL, after activation. On
this tier that needs a RUNTIME accumulator (`$__mw_head`, a linked list in
linear memory) rather than the static one, because the number of tool calls is
only known at runtime. This suite proves the closed loop end to end:

  * a component provides a service whose method does a witnessed mutation;
  * the method is called PER REQUEST (the exported `provide:ops.touch`), each
    call registering a transactional inverse into the component's RUNTIME
    accumulator;
  * on a clean unload the mutations PERSIST (discharged — the deliverable);
  * on an ABORT (the `abort` export — the seam item 245's explicit session
    commit/abort UX will drive) every per-call mutation REVERTS, all-or-nothing;
  * the residue is ENUMERABLE: `mw_live` counts every outstanding crossing (the
    wasm analogue of the py WAL discharge descriptors), the count rides up as
    calls register and back to 0 as an abort drains them.

THE DISPOSAL-ORDERING HAZARD (item 318 found this on py; checked here for wasm):
draining a method entry at method return would observe `committed() == 0` while
the session is still live and WRONGLY revert the deliverable. The fix is the
same park-for-drain discipline: the entry is parked in the runtime list and
drained ONLY by `deactivate`/`deactivate_step`, gated on `$__committed`, where
the commit-vs-abort bit is already settled. Consistent with the contract's
`activation-registered-only` wasm row: `$__mw_head` is component-instance state
drained by the component's own teardown, never a per-call epoch.

The witnessed extern is a fixed-address `@wasm` mark/revert stand-in in the
style test_witnessed_teardown.py already uses (item 244's real fs bodies are the
py reference; a memory-cell mutation is enough to exercise the runtime path),
extended to take the target slot as a PARAMETER so each per-call invocation
mutates a DISTINCT cell — the shape of an agent calling one fs tool repeatedly.
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
    return _load("revl_wasm_emit_method_witnessed", "emit.py")


# A per-call witnessed mutation: `stash(v)` marks the slot at 4096 + v*8 as
# stashed (writes 1), returning `v` as its data witness; the declared inverse
# `unstash(w)` clears slot 4096 + w*8 back to pristine (0). The Ok Result is
# built at a transient fixed cell (4200 tag / 4208 payload), immediately
# consumed by the accumulator, so reusing it across calls is fine — each call's
# witness is copied into its own runtime cell. `touch(v)` is the provided
# service method that fires it, once per tool call.
_SOURCE = '''
extern witnessed[t] fn stash(v: Int) -> Result[Int, Str]
    undo unstash(result)
    = @wasm {
      (i32.store (i32.add (i32.const 4096) (i32.mul (i32.wrap_i64 (local.get $p_v)) (i32.const 8))) (i32.const 1))
      (i32.store (i32.const 4200) (i32.const 0))
      (i64.store (i32.const 4208) (local.get $p_v))
      (i32.const 4200)
    }
extern pure fn unstash(w: Int) -> Unit
    = @wasm {
      (i32.store (i32.add (i32.const 4096) (i32.mul (i32.wrap_i64 (local.get $p_w)) (i32.const 8))) (i32.const 0))
    }
service Ops { emission fn touch(v: Int) }
component Agent provides ops: Ops {
  provide ops {
    fn touch(v) {
      effect stash(v)
    }
  }
}
'''

_CALLS = 3  # simulated tool calls, one per distinct slot


def _agent_wat():
    return _emitter().emit(compile_source(_SOURCE, "provide_method_witnessed.rvl"))["Agent"]


# ---------------------------------------------------------------------------
# emission-level: no wasmtime needed
# ---------------------------------------------------------------------------

def test_method_witnessed_effect_is_lowerable_and_wires_the_runtime_accumulator():
    """item 324: a witnessed effect in a provide-method body is no longer the
    blanket `method-time effects are not lowerable` refusal — it compiles, and
    the module gains the runtime accumulator (`$__mw_head`), the abort seam
    (`abort`), and the enumeration surface (`mw_live`)."""
    wat = _agent_wat()
    assert '(func (export "provide:ops.touch")' in wat
    assert "(global $__mw_head" in wat
    assert "(global $__mw_count" in wat
    assert '(func (export "abort")' in wat
    assert '(func (export "mw_live")' in wat
    # the per-call registration allocates a cell and pushes it newest-first
    assert "(call $alloc (i32.const 24))" in wat
    assert "(global.set $__mw_head" in wat
    # the declared inverse (`unstash`) is planned and rendered into the drain
    assert "(call $unstash)" in wat


def test_a_program_without_a_method_witnessed_effect_is_byte_identical():
    """The tight byte-identity gate (a prior wasm Slice-2b attempt churned
    non-witnessed goldens): the whole runtime accumulator is gated strictly on a
    component actually having a method-body witnessed effect. A plain provider
    emits NONE of it — no `$__mw_head`, no `abort`, no `mw_live`."""
    src = '''
    service S { fn f(x: Int) -> Int }
    component C provides s: S { provide s { fn f(x) { return x } } }
    '''
    wat = _emitter().emit(compile_source(src, "t.rvl"))["C"]
    assert "__mw_head" not in wat
    assert "__mw_count" not in wat
    assert 'export "mw_live"' not in wat
    assert 'export "abort"' not in wat


def test_non_witnessed_method_effect_stays_refused():
    """Only the witnessed position item 318 opened is admitted. A plain
    (site-`undo`) effect in a method body is still a hard refusal — the wasm
    accumulator has no general method-time acquisition."""
    src = '''
    type Bracket = { fd: Int }
    extern acquire fn tick() -> Bracket undo untick(0) = @wasm { (i32.const 0) }
    extern pure fn untick(r: Int) -> Unit = @wasm { }
    service S { emission fn f(x: Int) }
    component C provides s: S {
      provide s { fn f(x) { effect tick() undo untick(0) } }
    }
    '''
    emitter = _emitter()
    with pytest.raises(emitter.EmitError, match="method-time effects are not lowerable"):
        emitter.emit(compile_source(src, "t.rvl"))


def test_method_time_compensation_stays_refused():
    """item 301's soundness bar, unchanged by this slice: a compensation
    attached inside a provide-method body is still a hard `EmitError`. This
    slice lifts the witnessed position only, never the compensation one."""
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
# execution-level: live wasmtime, first-party direct driving (no cordis-wasm)
# ---------------------------------------------------------------------------

def _instantiate(wasmtime, wat):
    engine = wasmtime.Engine()
    module = wasmtime.Module(engine, wat)
    store = wasmtime.Store(engine)
    exports = wasmtime.Instance(store, module, []).exports(store)
    return store, exports


def _slot(store, exports, v: int) -> int:
    """The per-call mutation cell for slot `v`: 1 == stashed (mutated),
    0 == pristine (as the world started, or reverted residue-free)."""
    addr = 4096 + v * 8
    return int.from_bytes(exports["memory"].read(store, addr, addr + 4), "little", signed=True)


def _activate(store, exports):
    for _ in range(16):
        if not exports["activate_step"](store):
            return
    raise AssertionError("activate_step did not terminate")  # pragma: no cover


def _mutated(store, exports, v: int) -> bool:
    return _slot(store, exports, v) == 1


def _pristine(store, exports, v: int) -> bool:
    return _slot(store, exports, v) == 0


# 1. per-tool-call witnessed mutation PERSISTS on a clean unload (commit) -------

def test_per_tool_call_mutations_persist_on_clean_unload():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    store, exports = _instantiate(wasmtime, _agent_wat())

    # activation did nothing but complete cleanly; the accumulator is empty
    _activate(store, exports)
    assert exports["committed"](store) == 1
    assert exports["mw_live"](store) == 0

    # each tool call runs the provide-method, registering ONE transactional
    # inverse into the component's runtime accumulator (per-tool-call H1)
    for v in range(_CALLS):
        exports["provide:ops.touch"](store, v)
        assert _mutated(store, exports, v), "the witnessed mutation did not apply on the call"
    assert exports["mw_live"](store) == _CALLS

    exports["deactivate"](store)  # clean unload == implicit commit

    # the deliverable persists on every slot; nothing was reverted
    for v in range(_CALLS):
        assert _mutated(store, exports, v), "clean unload wrongly reverted a per-call mutation"
    # the entries discharged (never replayed), so the count still names them
    assert exports["mw_live"](store) == _CALLS


# 2. per-tool-call witnessed mutation REVERTS on abort, residue-free -----------

def test_per_tool_call_mutations_revert_on_abort():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    store, exports = _instantiate(wasmtime, _agent_wat())
    _activate(store, exports)
    for v in range(_CALLS):
        exports["provide:ops.touch"](store, v)
        assert _mutated(store, exports, v)
    assert exports["mw_live"](store) == _CALLS

    # abort the session's work (item 245's reject drives this seam): the next
    # teardown reverts instead of committing
    exports["abort"](store)
    assert exports["committed"](store) == 0
    exports["deactivate"](store)

    # every per-call mutation reverted, and the drain left no residue
    for v in range(_CALLS):
        assert _pristine(store, exports, v), "abort did not revert a per-call mutation"
    assert exports["mw_live"](store) == 0, "the abort drain left an outstanding entry"


# 3. abort is all-or-nothing across independent per-call mutations ------------

def test_abort_reverts_every_call_not_just_the_last():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    store, exports = _instantiate(wasmtime, _agent_wat())
    _activate(store, exports)
    for v in range(_CALLS):
        exports["provide:ops.touch"](store, v)

    exports["abort"](store)
    exports["deactivate"](store)

    # all _CALLS, in one abort — the runtime accumulator is the shared, ordered
    # LIFO stack, so no slot is left stashed
    assert all(_pristine(store, exports, v) for v in range(_CALLS))
    assert exports["mw_live"](store) == 0


# 4. residue is ENUMERABLE: mw_live names every crossing; commit keeps the
#    count, an abort drains it to zero -----------------------------------------

def test_mw_live_enumerates_every_crossing_then_commit_keeps_it():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    store, exports = _instantiate(wasmtime, _agent_wat())
    _activate(store, exports)

    # every per-call crossing is enumerated the instant it registers, well
    # before the commit/abort decision
    for i, v in enumerate(range(_CALLS), start=1):
        exports["provide:ops.touch"](store, v)
        assert exports["mw_live"](store) == i

    exports["deactivate"](store)  # clean commit
    # a commit discharges (never reverts) the entries — the count still names
    # every committed deliverable, none dropped as residue
    assert exports["mw_live"](store) == _CALLS


def test_mw_live_drains_to_zero_on_abort():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    store, exports = _instantiate(wasmtime, _agent_wat())
    _activate(store, exports)
    for v in range(_CALLS):
        exports["provide:ops.touch"](store, v)
    assert exports["mw_live"](store) == _CALLS

    # the drain reports its progress one entry per `deactivate_step` call — the
    # same one-entry-per-call idiom the static chain uses — and the live count
    # falls monotonically to 0, so a host can watch the residue clear
    exports["abort"](store)
    seen = [exports["mw_live"](store)]
    for _ in range(_CALLS + 2):
        more = exports["deactivate_step"](store)
        seen.append(exports["mw_live"](store))
        if not more:
            break
    assert seen[0] == _CALLS
    assert seen[-1] == 0
    assert seen == sorted(seen, reverse=True), f"count did not fall monotonically: {seen}"
