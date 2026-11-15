"""Issue #2949: a Map index compared with an Int literal gets its widen marker.

A `Map[Str, V]` subscript has carried the declared `value_type` since issue
#957, but the inference the equality marker reads (`infer_ast` at the
`fn`/`test` stratum, `infer_ir` in a component body) typed a map read as
unknown. `m["a"] == 3` with a `Float` (or `Int32`) value therefore reached
every tier unmarked, while a bare `Float` operand and a `List` element read
were marked correctly:

  - TypeScript compared `revlEq(revlMapIndex(m, "a"), 3n)`, a number against
    a bigint, and answered false;
  - rust did not build (E0308, mismatched types) and java failed the
    assertion;
  - python and go absorbed the mix behind a host rule.

A map read now answers `V`, so `_mark_equality_widen` sees the operand's
width exactly as it does for `List`: the `Int` literal widens to `Float`
against a `Float` value, and the `Int32` read widens to `Int`. A
`Map[Str, Int]` read carries no widen marker and the equality node is
unchanged; elsewhere the new type knowledge does reach `_operand_annotation`,
so arithmetic over an Int map read gains `"operands": "Int"`.

The executing five-tier probe lives in `tests/test_cross_tier_execution.py`
(so the conformance job runs it), and the self-hosted lowering is held to the
same IR by the `maps.rvl` corpus document. This file pins the IR on BOTH
strata the marker reads: the plain `fn` path (`infer_ast`) and a `provide`
method body (`infer_ir`), plus the wasm tier's unchanged refusal.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


SOURCE = """
fn float_value_against_an_int(m: Map[Str, Float]) -> Bool { return m["a"] == 3 }
fn int_against_a_float_value(m: Map[Str, Float]) -> Bool { return 3 == m["a"] }
fn int32_value_against_an_int(m: Map[Str, Int32]) -> Bool { return m["a"] == 3 }
fn int_against_an_int32_value(m: Map[Str, Int32]) -> Bool { return 3 == m["a"] }
fn int_value_against_an_int(m: Map[Str, Int]) -> Bool { return m["a"] == 3 }
fn int_value_against_a_float(m: Map[Str, Int]) -> Bool { return m["a"] == 3.0 }
"""

# A component body goes through the OTHER inference (`infer_ir` over lowered
# nodes), not the `fn` path above. Its index node carries neither `key_type`
# nor `value_type`; the map value type has to come from the target's declared
# type, so a revert of the `infer_ir` arm leaves this IR unmarked.
COMPONENT_SOURCE = """
service Reads {
  fn float_is_three(m: Map[Str, Float]) -> Bool
  fn int32_is_three(m: Map[Str, Int32]) -> Bool
}
component Reader provides reads: Reads {
  provide reads {
    fn float_is_three(m) = m["a"] == 3
    fn int32_is_three(m) = m["a"] == 3
  }
}
"""


def _expr(name: str) -> dict:
    ir = compile_source(SOURCE, "map_index_2949.rvl")
    fn = next(f for f in ir["functions"] if f["name"] == name)
    return fn["body"][-1]["expr"]


def _component_expr(name: str) -> dict:
    ir = compile_source(COMPONENT_SOURCE, "map_index_2949_component.rvl")
    body = ir["components"][0]["body"]
    provide = next(step for step in body if step["step"] == "provide")
    method = next(m for m in provide["methods"] if m["name"] == name)
    return method["body"][-1]["expr"]


def _component_map_read() -> dict:
    """A component-stratum map read: a `name` target and no map annotations."""
    return {"kind": "index",
            "target": {"kind": "name", "id": "m"},
            "index": {"kind": "lit", "value": "a"}}


def _map_read(value_type: str) -> dict:
    return {"kind": "index",
            "target": {"kind": "var", "name": "m"},
            "index": {"kind": "lit", "value": "a"},
            "key_type": "Str", "value_type": value_type}


def test_float_value_marks_the_int_literal():
    expr = _expr("float_value_against_an_int")
    assert expr == {"kind": "bin", "op": "==",
                    "left": _map_read("Float"),
                    "right": {"kind": "lit", "value": 3, "widen": "Float"}}


def test_float_value_marks_the_literal_in_either_operand_order():
    expr = _expr("int_against_a_float_value")
    assert expr == {"kind": "bin", "op": "==",
                    "left": {"kind": "lit", "value": 3, "widen": "Float"},
                    "right": _map_read("Float")}


def test_int32_value_marks_the_narrower_index():
    expr = _expr("int32_value_against_an_int")
    assert expr == {"kind": "bin", "op": "==",
                    "left": {**_map_read("Int32"), "widen": "Int"},
                    "right": {"kind": "lit", "value": 3}}


def test_int32_value_marks_the_index_in_either_operand_order():
    expr = _expr("int_against_an_int32_value")
    assert expr == {"kind": "bin", "op": "==",
                    "left": {"kind": "lit", "value": 3},
                    "right": {**_map_read("Int32"), "widen": "Int"}}


def test_component_stratum_marks_a_float_map_value():
    """A `provide` method body lowers through `infer_ir`, not `infer_ast`; the
    component index node has no `value_type`, so this only marks once the arm
    reads the target's declared map type."""
    expr = _component_expr("float_is_three")
    assert expr == {"kind": "bin", "op": "==",
                    "left": _component_map_read(),
                    "right": {"kind": "lit", "value": 3, "widen": "Float"}}


def test_component_stratum_marks_an_int32_map_value():
    expr = _component_expr("int32_is_three")
    assert expr == {"kind": "bin", "op": "==",
                    "left": {**_component_map_read(), "widen": "Int"},
                    "right": {"kind": "lit", "value": 3}}


def test_an_int_value_marks_the_read_against_a_float_literal():
    """The mirror image also comes from the new type knowledge: an `Int` map
    value compared with a `Float` literal widens the READ, so the marker must
    reach an `index` node, not only a literal."""
    expr = _expr("int_value_against_a_float")
    assert expr == {"kind": "bin", "op": "==",
                    "left": {**_map_read("Int"), "widen": "Float"},
                    "right": {"kind": "lit", "value": 3.0}}


def test_int_value_carries_no_marker_and_stays_byte_identical():
    """The acceptance's unchanged row: an `Int` value was already exact, so it
    gains neither a marker nor a conversion on any tier. Pinned against the
    pre-fix node, key for key."""
    expr = _expr("int_value_against_an_int")
    assert expr == {"kind": "bin", "op": "==",
                    "left": _map_read("Int"),
                    "right": {"kind": "lit", "value": 3}}
    assert "widen" not in expr["left"]
    assert "widen" not in expr["right"]


@pytest.mark.parametrize("value_type", ["Float", "Int32"])
def test_wasm_still_refuses_a_map_value_by_name(value_type):
    """The fix does not teach the wasm tier about Maps: it must keep refusing
    the same programs with its own type name, not a new failure."""
    source = (f'fn f(m: Map[Str, {value_type}]) -> Bool '
              '{ return m["a"] == 3 }\n')
    spec = importlib.util.spec_from_file_location(
        "emit_wasm_2949", ROOT / "backends" / "wasm" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(Exception) as excinfo:
        module.emit(compile_source(source, "map_index_2949_wasm.rvl"))
    assert f"type 'Map[Str, {value_type}]' is not lowerable" in str(excinfo.value)
