"""An extern-DECLARED `compensate` on the wasm tier — issue #1979.

Design: docs/design/243-witnessed-externs.md, docs/design/teardown-contract.md.
#1511 and #1592 fixed the same drop on ts, go, rust and java: an
`extern emission fn put(v: Int) -> Unit compensate restore()` registered its
compensation NOWHERE on the emitted tier, so an abort never ran it — the program
emitted, with no refusal and no diagnostic. The wasm emitter was never covered:
it had no `compensated` registry at all. Only a SITE-spelled
`emit .. compensate ..` registered (activation body) or was refused (provide
method, "method-time compensation is not lowerable").

This suite pins both halves of the fix (#1979's Done order):

* the declared form REGISTERS — in the activation body through the compile-time
  entry list (`Phase 2`, the same entry kind a site-spelled clause produces), and
  in a provide METHOD through the RUNTIME `$__mc_*` accumulator, which is
  component-instance state drained by `deactivate` (the item-324 `$__mw_*`
  discipline, restated for compensations: a method runs N times, so a
  compile-time entry would be unsound);
* what still cannot register is REFUSED BY NAME, never dropped: a site-spelled
  method-time `compensate` (item 301's soundness bar), and a provide method the
  frontend marks as a UI transaction unit (`revl.ui_transaction`, item 522) —
  that unit's boundary is not representable on this tier, and its fail-closed
  frontend dependency is checked rather than assumed.

The execution-level tests (gated by `pytest.importorskip("wasmtime", ...)`, the
pattern this directory uses) run the module on a live wasmtime instance and
observe the closed loop: the compensation's effect reverts on abort and is
DISCHARGED on commit, and `mc_live` enumerates every outstanding registration.
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
    return _load("revl_wasm_emit_declared_compensate_1979", "emit.py")


def _lifecycle():
    return _load("revl_wasm_lifecycle_declared_compensate_1979", "lifecycle.py")


# `put` is an EXTERN emission with a declared compensation `restore` — the
# issue's own shape. Both bodies are real (not `(nop)`) so the exec-level tests
# can observe the compensation actually running: `put` bumps 4200, `restore`
# bumps 4096.
_EXTERNS = '''
extern pure fn restore() -> Unit
    = @wasm { (i32.store (i32.const 4096) (i32.add (i32.load (i32.const 4096)) (i32.const 1))) }
extern emission fn put(v: Int) -> Unit compensate restore()
    = @wasm { (i32.store (i32.const 4200) (i32.add (i32.load (i32.const 4200)) (i32.const 1))) }
'''

# the issue's FIRST reproducer: `emit put(v)` inside a provide method
_METHOD_SRC = _EXTERNS + '''
service Ops { emission fn run(v: Int) -> Int }
component Agent provides ops: Ops {
  provide ops {
    fn run(v) {
      emit put(v)
      return 1
    }
  }
}
'''

# the issue's SECOND reproducer: `emit put(1)` in the activation body. `boom`
# traps, which is the abort trigger the item-243 tests use.
_ACTIVATION_SRC = '''
type Bracket = { fd: Int }
extern acquire fn boom() -> Bracket undo noop(0) = @wasm { unreachable }
extern pure fn noop(r: Int) -> Unit = @wasm { }
''' + _EXTERNS + '''
component Agent {
  emit put(1)
  effect boom() undo noop(0)
}
'''

_CALLS = 3


def _method_wat():
    return _emitter().emit(compile_source(_METHOD_SRC, "decl.rvl"))["Agent"]


def _activation_wat():
    return _emitter().emit(compile_source(_ACTIVATION_SRC, "decl_act.rvl"))["Agent"]


# ---------------------------------------------------------------------------
# the bug: the declared compensation is REGISTERED, in both positions
# ---------------------------------------------------------------------------

def test_declared_compensate_in_a_provide_method_is_registered():
    """#1979's first reproducer. At the base revision `restore` was absent from
    every entry of the emitted module and nothing refused it. It is now planned
    and the runtime accumulator that a method-time registration needs is wired:
    the cell list head (`$__mc_head`), its count (`$__mc_count`, the emptiness
    guard), the `abort` seam and the `mc_live` enumeration export."""
    wat = _method_wat()
    assert "(call $fn.restore)" in wat, "the declared compensation is rendered nowhere"
    assert "(global $__mc_head" in wat
    assert "(global $__mc_count" in wat
    assert '(func (export "abort")' in wat
    assert '(func (export "mc_live")' in wat
    # one cell per registration, newest-first — the item-324 discipline
    assert "(global.set $__mc_head" in wat


def test_declared_compensate_in_the_activation_body_is_registered():
    """#1979's second reproducer, the compile-time half: an activation-registered
    entry needs no runtime cell list, and lands in the SAME `compensation` entry
    kind a site-spelled clause produces — Phase 2 only (item 243)."""
    wat = _activation_wat()
    assert "(call $fn.restore)" in wat
    assert "__mc_head" not in wat, "an activation-body entry must not need a runtime cell list"
    descriptor = _lifecycle().parse_teardown_descriptor(wat)
    assert descriptor is not None
    comp = next(e for e in descriptor["entries"] if e["entry"] == "compensation")
    assert comp["dispatch"] >= descriptor["phase1Count"]


def test_a_declared_compensate_the_tier_registers_is_not_refused():
    """The pair to the refusals below: the declared form must EMIT, not refuse.
    (A refusal here would be a different bug — the point of #1979 is that the
    compensation reaches the teardown accumulator.)"""
    emitter = _emitter()
    for src in (_METHOD_SRC, _ACTIVATION_SRC):
        assert emitter.emit(compile_source(src, "d.rvl"))["Agent"]


def test_a_program_without_a_compensated_extern_is_byte_identical():
    """The byte-identity discipline every wasm slice is held to: the whole
    runtime accumulator is gated on a component actually having a declared
    compensation reached from a method body. A plain provider emits NONE of it."""
    src = '''
    extern emission fn plain() -> Unit = @wasm { }
    service S { emission fn f(x: Int) -> Int }
    component C provides s: S { provide s { fn f(x) { emit plain() return x } } }
    '''
    wat = _emitter().emit(compile_source(src, "t.rvl"))["C"]
    assert "__mc_head" not in wat
    assert "__mc_count" not in wat
    assert 'export "mc_live"' not in wat


# ---------------------------------------------------------------------------
# ... and everything the tier still cannot register is REFUSED BY NAME
# ---------------------------------------------------------------------------

def test_site_spelled_method_time_compensation_is_still_refused():
    """item 301's soundness bar, unchanged and deliberately re-stated here: a
    compensation SITE-spelled inside a method body names a value that only exists
    at that call site, so it is still a hard `EmitError`. #1979 lifts only the
    DECLARED position (a compensation with no variables in scope, which is
    registerable as component-instance state). The refusal keeps its matched
    prefix and now names the path that does work."""
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


def test_site_spelled_compensate_replaces_the_declared_one():
    """item 247/#1902 precedence, restated at the registration site: a site
    spelling REPLACES the extern's declared default, and the crossing registers
    EXACTLY ONCE (a second registration would double-run the teardown). In the
    activation body both spellings are registrable, so this is where the
    replacement is observable — in a method body the site-spelled form is the
    refusal above, never a second registration."""
    src = _EXTERNS + '''
    extern pure fn other() -> Unit
        = @wasm { (i32.store (i32.const 4300) (i32.add (i32.load (i32.const 4300)) (i32.const 1))) }
    component Agent {
      emit put(1) compensate other()
    }
    '''
    wat = _emitter().emit(compile_source(src, "t.rvl"))["Agent"]
    assert "(call $fn.other)" in wat
    assert "(call $fn.restore)" not in wat, "the declared compensation must not ALSO register"
    descriptor = _lifecycle().parse_teardown_descriptor(wat)
    assert [e for e in descriptor["entries"] if e["entry"] == "compensation"] != []
    assert len([e for e in descriptor["entries"] if e["entry"] == "compensation"]) == 1


def test_a_ui_transaction_unit_method_is_refused_by_name():
    """#1979's Done list, second sentence: "So is a provide method that is a UI
    transaction unit." The unit is a PER-TRANSACTION scope; this tier's
    method-time registration is drained by `deactivate` at SESSION granularity,
    so the unit is not representable and a unit's `compensate` would be settled
    at a boundary `revl.ui_transaction` did not compute. Refused by name, citing
    the tier limit and the issue — never emitted with no unit.

    The fixture is the py reference tier's computer-use document
    (`tests/fixtures/emit_py_corpus/ui_unit.rvl`), whose `type_amount(..)
    compensate clear_amount()` is the shape the issue reports."""
    fixture = ROOT / "tests/fixtures/emit_py_corpus/ui_unit.rvl"
    src = fixture.read_text()
    assert "compensate" in src or "UiTarget" in src
    emitter = _emitter()
    with pytest.raises(emitter.EmitError, match="#1979"):
        emitter.emit(compile_source(src, "ui_unit.rvl"))


def test_a_ui_transaction_unit_refusal_names_the_unit():
    """The refusal is a NAME, not a blanket ban on computer-use: the message has
    to say which method is a unit, so a reader can act on it. (Matching on the
    unit name alone would also match this tier's older `has no @wasm body`
    refusal, which names the same method for an unrelated reason — so the match
    pins this gate's own wording too.)"""
    fixture = ROOT / "tests/fixtures/emit_py_corpus/ui_unit.rvl"
    emitter = _emitter()
    with pytest.raises(emitter.EmitError, match=r"desk\.act.*UI transaction unit.*#1979"):
        emitter.emit(compile_source(fixture.read_text(), "ui_unit.rvl"))


def test_a_document_without_a_computer_use_capability_is_untouched_by_that_gate():
    """The UI gate is keyed on a declared `ui.*`/`screen.*` capability, so an
    ordinary document never pays for it — including one whose method crosses an
    emission with a declared compensation (#1979's own case)."""
    wat = _method_wat()
    assert "(func (export \"mc_live\")" in wat


# ---------------------------------------------------------------------------
# execution-level: live wasmtime, first-party direct driving
# ---------------------------------------------------------------------------

def _instantiate(wasmtime, wat):
    engine = wasmtime.Engine()
    module = wasmtime.Module(engine, wat)
    store = wasmtime.Store(engine)
    exports = wasmtime.Instance(store, module, []).exports(store)
    return store, exports


def _mem_i32(store, exports, addr):
    return int.from_bytes(exports["memory"].read(store, addr, addr + 4), "little", signed=True)


def _run_to_completion_or_trap(wasmtime, store, exports):
    for _ in range(16):
        try:
            if not exports["activate_step"](store):
                return False
        except wasmtime.Trap:
            return True
    raise AssertionError("activate_step did not terminate")  # pragma: no cover


def test_method_registrations_revert_on_abort_and_enumerate_as_they_register():
    """The closed loop for the runtime accumulator: every per-call registration
    is enumerated by `mc_live` the instant it happens, and one `abort` runs the
    declared compensation once per outstanding registration — all-or-nothing,
    residue-free."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    store, exports = _instantiate(wasmtime, _method_wat())
    assert _run_to_completion_or_trap(wasmtime, store, exports) is False
    assert exports["committed"](store) == 1
    assert exports["mc_live"](store) == 0

    for i, v in enumerate(range(_CALLS), start=1):
        exports["provide:ops.run"](store, v)
        assert exports["mc_live"](store) == i, "a registration was not enumerated"

    exports["abort"](store)
    assert exports["committed"](store) == 0
    exports["deactivate"](store)
    assert _mem_i32(store, exports, 4096) == _CALLS, "abort did not replay the declared compensation"
    assert exports["mc_live"](store) == 0, "the abort drain left an outstanding registration"


def test_method_registrations_are_discharged_on_a_clean_unload():
    """Persist-on-commit: a clean unload discharges the entries instead of
    replaying them, so the compensation never runs and the deliverable stands."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    store, exports = _instantiate(wasmtime, _method_wat())
    _run_to_completion_or_trap(wasmtime, store, exports)
    for v in range(_CALLS):
        exports["provide:ops.run"](store, v)
    assert _mem_i32(store, exports, 4200) == _CALLS

    exports["deactivate"](store)
    assert _mem_i32(store, exports, 4096) == 0, "a clean unload wrongly replayed the compensation"
    assert _mem_i32(store, exports, 4200) == _CALLS, "the emission is the deliverable: it must persist"
    assert exports["mc_live"](store) == _CALLS


def test_activation_body_registration_replays_on_abort_only():
    """The compile-time half, executed: the entry is in the abort chain, so a
    trapped activation replays the declared compensation and a clean one does
    not."""
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")

    store, exports = _instantiate(wasmtime, _activation_wat())
    assert _run_to_completion_or_trap(wasmtime, store, exports) is True
    assert exports["committed"](store) == 0
    exports["deactivate"](store)
    assert _mem_i32(store, exports, 4096) == 1

    clean = _emitter().emit(compile_source(_EXTERNS + "component Agent { emit put(1) }\n",
                                           "clean.rvl"))["Agent"]
    store, exports = _instantiate(wasmtime, clean)
    assert _run_to_completion_or_trap(wasmtime, store, exports) is False
    assert exports["committed"](store) == 1
    exports["deactivate"](store)
    assert _mem_i32(store, exports, 4096) == 0
