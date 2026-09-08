"""The wasm tier's STATIC E-Stop report — roadmap item 443, issue #122.

Design of record: docs/design/443-estop-tier-contract.md, the wasm row. A wasm
instance has no process, no clock and no file access, so it cannot honor the
operator latch at runtime (no crossing seam, no watcher). Halting it is the
embedder DROPPING the instance without running its teardown. What makes this
better than the SIGKILL/UNKNOWN population every other seamless tier falls in:
the instance's inventory is knowable from OUTSIDE, at compile time, from the
`revl:teardown` custom section (item 243 Slice 2b, `_teardown_section` in
`backends/wasm/emit.py`), which enumerates every activation-registered
`transactional`/`compensation` descriptor.

This suite pins the projection end to end: compile a component with a witnessed
(`transactional`) entry and a `compensation` entry, read its emitted section
back with `lifecycle.static_estop_inventory`, and assert every entry surfaces
as an `estop-stranded (static)` record — a third population label, distinct
from the honoring tiers and from UNKNOWN. Emission-level only, so it needs no
wasmtime toolchain, the same as the emission-level half of
`test_witnessed_teardown.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    return _load("revl_wasm_emit_estop_static", "emit.py")


def _lifecycle():
    return _load("revl_wasm_lifecycle_estop_static", "lifecycle.py")


# A witnessed effect (`mark`/`revert`) and an activation-registered
# compensation (`sendmsg`/`undosend`); a bracket (`tick`/`untick`) for contrast
# — brackets carry no WAL row, so they never appear in the descriptor's
# `entries` and so never as static residue.
_SRC = '''
type Bracket = { fd: Int }
extern acquire fn tick() -> Bracket
    undo untick(0)
    = @wasm { (i32.const 0) }
extern pure fn untick(r: Int) -> Unit = @wasm { }

extern witnessed[t] fn mark(v: Int) -> Result[Int, Str]
    undo revert(result)
    = @wasm {
      (i32.store (i32.const 4096) (i32.const 0))
      (i64.store (i32.const 4104) (local.get $p_v))
      (i32.const 4096)
    }
extern pure fn revert(w: Int) -> Unit = @wasm { }

extern emission fn sendmsg(x: Int) -> Int compensate undosend(0) = @wasm { (local.get $p_x) }
extern pure fn undosend(x: Int) -> Unit = @wasm { }

component Probe {
  effect mark(7)
  effect tick() undo untick(0)
  emit sendmsg(1) compensate undosend(1)
}
'''


def test_static_inventory_strands_the_teardown_sections_entries():
    """The headline: the `static` inventory names the component's witnessed and
    compensation entries as `estop-stranded (static)`, read straight from the
    emitted `revl:teardown` section — no runtime, no latch seam."""
    wat = _emitter().emit(compile_source(_SRC, "t.rvl"))["Probe"]
    lifecycle = _lifecycle()

    # the section is really there and this is really reading it
    descriptor = lifecycle.parse_teardown_descriptor(wat)
    assert descriptor is not None
    kinds = sorted(e["entry"] for e in descriptor["entries"])
    assert kinds == ["compensation", "transactional"]

    inv = lifecycle.static_estop_inventory(
        wat, name="Probe", reason="runaway loop", operator="ops@example")

    assert inv["population"] == "static"
    assert inv["verdict"] == "halted"
    assert inv["resumable"] is False
    # ambiguity is the host's to name (E4); the static projection invents none
    assert inv["inFlight"] == []
    assert sorted(r["entry"] for r in inv["stranded"]) == ["compensation", "transactional"]
    for record in inv["stranded"]:
        assert record["kind"] == "estop-stranded"
        assert record["population"] == "static"
        assert record["attemptedFlag"] is False
        assert record["outcome"] == "not-attempted"
        assert record["component"] == "Probe"
    # the books partition the registered stack
    assert sum(a["stranded"] for a in inv["activations"]) == len(inv["stranded"])


def test_static_halt_line_is_the_mergeable_e5_inventory_line():
    """The conductor/embedder prints one `[<name>] HALTED <json>` line on the
    instance's behalf, byte-shaped like a honoring tier's E5 line so it merges
    into the halt report by name."""
    import json

    wat = _emitter().emit(compile_source(_SRC, "t.rvl"))["Probe"]
    line = _lifecycle().static_estop_line(wat, name="Probe", reason="runaway")

    assert line.startswith("[Probe] HALTED ")
    parsed = json.loads(line[len("[Probe] HALTED "):])
    assert parsed["population"] == "static"
    assert parsed["process"] == "Probe"
    assert len(parsed["stranded"]) == 2


def test_a_seamless_component_reports_no_static_residue():
    """A component that registers no transactional/compensation entry emits no
    `revl:teardown` section, so its static inventory is empty-but-well-formed —
    never a fabricated residue claim."""
    src = '''
type Bracket = { fd: Int }
extern acquire fn tick() -> Bracket
    undo untick(0)
    = @wasm { (i32.const 0) }
extern pure fn untick(r: Int) -> Unit = @wasm { }

component Bare { effect tick() undo untick(0) }
'''
    wat = _emitter().emit(compile_source(src, "t.rvl"))["Bare"]
    lifecycle = _lifecycle()
    assert lifecycle.parse_teardown_descriptor(wat) is None
    inv = lifecycle.static_estop_inventory(wat, name="Bare")
    assert inv["population"] == "static"
    assert inv["stranded"] == []
    assert inv["activations"] == []


if __name__ == "__main__":  # pragma: no cover
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
