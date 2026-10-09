"""Issue #1979: an extern-declared `compensate` on the wasm tier.

An extern may declare its own compensation (item 254):

    extern emission fn put(v: Int) -> Unit compensate restore() = @wasm { ... }

Every crossing of it owes that compensation. #1511 and #1592 made ts, go,
rust and java register it at every site; on this tier it registered nowhere,
so an abort never ran it, and the program still emitted.

This tier's accumulator is the activation state machine, fixed at compile
time. It registers a compensation from an activation-body `emit` statement,
and now that statement's own call registers the extern's declared one too
(the site-spelled clause still replaces it, issue #1902). Everywhere else (a
provide method, a nested position) the crossing is refused by name rather
than emitted with the compensation in no entry. A provide method that is a UI
transaction unit (item 522, issue #1369) is refused for the same reason: a
failed call cannot be settled without a per-call accumulator.

On the base every test here fails except the two site-spelled controls:
the method and unit programs emitted, the activation entry did not exist, and
the abort never ran `restore`.
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
    return _load("revl_wasm_emit_declared_1979", "emit.py")


def _lifecycle():
    return _load("revl_wasm_lifecycle_declared_1979", "lifecycle.py")


# `put` declares `restore`, which counts its runs at 5020; `undo_site` counts
# at 5024; `boom` traps, the abort trigger.
_EXTERNS = '''
type Bracket = { fd: Int }
extern pure fn restore() -> Unit
    = @wasm { (i32.store (i32.const 5020) (i32.add (i32.load (i32.const 5020)) (i32.const 1))) }
extern pure fn undo_site() -> Unit
    = @wasm { (i32.store (i32.const 5024) (i32.add (i32.load (i32.const 5024)) (i32.const 1))) }
extern emission fn put(v: Int) -> Unit compensate restore() = @wasm { (nop) }
extern acquire fn boom() -> Bracket undo noop(0) = @wasm { unreachable }
extern pure fn noop(r: Int) -> Unit = @wasm { }
'''

_ACTIVATION_ABORT = _EXTERNS + '''
component Agent {
  emit put(1)
  effect boom() undo noop(0)
}
'''

_ACTIVATION_COMMIT = _EXTERNS + '''
component Agent {
  emit put(1)
}
'''

_SITE_ABORT = _EXTERNS + '''
component Agent {
  emit put(1) compensate undo_site()
  effect boom() undo noop(0)
}
'''

_METHOD = _EXTERNS + '''
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

_UI_UNIT = '''
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}
extern emission[ui.find] fn locate(name: Str) -> UiTarget = @wasm { (unreachable) }
extern emission[ui.click] fn actuate(t: UiTarget) -> Unit = @wasm { (nop) }
service Ops { emission fn run(v: Int) -> Int }
component Agent provides ops: Ops {
  provide ops {
    fn run(v) {
      let t = emit locate("Approve")
      emit actuate(t)
      return 1
    }
  }
}
'''


def _wat(source: str) -> str:
    return _emitter().emit(compile_source(source, "declared_1979.rvl"))["Agent"]


# ---------------------------------------------------------------------------
# emission-level: no wasmtime needed
# ---------------------------------------------------------------------------

def test_a_method_crossing_of_a_declared_compensate_is_refused_by_name():
    emitter = _emitter()
    with pytest.raises(emitter.EmitError) as caught:
        emitter.emit(compile_source(_METHOD, "declared_1979.rvl"))
    message = str(caught.value)
    assert "Agent.ops.run: extern `put` declares `compensate`" in message
    assert "issue #1979" in message


def test_a_ui_transaction_unit_is_refused_by_name():
    emitter = _emitter()
    with pytest.raises(emitter.EmitError) as caught:
        emitter.emit(compile_source(_UI_UNIT, "declared_1979.rvl"))
    message = str(caught.value)
    assert "Agent.ops.run: this provide method crosses a computer-use verb" in message
    assert "UI transaction unit" in message
    assert "issue #1979" in message and "#1369" in message


def test_an_activation_emit_registers_the_declared_compensation():
    wat = _wat(_ACTIVATION_COMMIT)
    assert "call $fn.restore" in wat


def test_a_site_spelled_compensation_replaces_the_declared_one():
    wat = _wat(_SITE_ABORT)
    assert "call $fn.undo_site" in wat
    assert "call $fn.restore" not in wat


# ---------------------------------------------------------------------------
# execution-level: live wasmtime
# ---------------------------------------------------------------------------

def _instantiate(wasmtime, wat):
    cfg = wasmtime.Config()
    cfg.epoch_interruption = True
    engine = wasmtime.Engine(cfg)
    module = wasmtime.Module(engine, wat)
    store = wasmtime.Store(engine)
    store.set_epoch_deadline(1_000_000)
    instance = wasmtime.Instance(store, module, [])
    return engine, store, instance.exports(store)


def _mem_i32(store, exports, addr):
    return int.from_bytes(exports["memory"].read(store, addr, addr + 4), "little", signed=True)


def _activate(wasmtime, store, exports) -> bool:
    """Drive `activate_step` to the end; True iff a trap aborted it."""
    for _ in range(16):
        try:
            if not exports["activate_step"](store):
                return False
        except wasmtime.Trap:
            return True
    raise AssertionError("activate_step did not terminate")  # pragma: no cover


def test_an_abort_runs_the_declared_compensation_in_phase_two():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    wat = _wat(_ACTIVATION_ABORT)
    engine, store, exports = _instantiate(wasmtime, wat)
    assert _activate(wasmtime, store, exports), "`boom` traps by design"
    result = _lifecycle().drive_teardown(engine, store, exports, wat,
                                         phase2_per_call_ms=500, phase2_budget_ms=5000)
    assert result == {"clean": True, "outstanding": []}
    assert _mem_i32(store, exports, 5020) == 1, "the declared compensation ran once"


def test_a_clean_commit_discharges_the_declared_compensation():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    wat = _wat(_ACTIVATION_COMMIT)
    _engine, store, exports = _instantiate(wasmtime, wat)
    assert not _activate(wasmtime, store, exports)
    assert exports["committed"](store) == 1
    exports["deactivate"](store)
    assert _mem_i32(store, exports, 5020) == 0, "a commit never runs the compensation"


def test_the_site_spelled_control_runs_only_the_site_one():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    wat = _wat(_SITE_ABORT)
    engine, store, exports = _instantiate(wasmtime, wat)
    assert _activate(wasmtime, store, exports)
    _lifecycle().drive_teardown(engine, store, exports, wat,
                                phase2_per_call_ms=500, phase2_budget_ms=5000)
    assert _mem_i32(store, exports, 5024) == 1
    assert _mem_i32(store, exports, 5020) == 0
