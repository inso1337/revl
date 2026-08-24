"""Unit tests for the wasm lifecycle driver's revl-side classifier and the
executor's scalar expression evaluator (roadmap item 142).

These need neither wasmtime nor the cordis-wasm runtime — they pin the
*decision* the driver makes (which lifecycle tests the substrate can express)
and the scalar interpreter the executor evaluates asserts with. The live
boot -> call -> unload -> no-residue round-trip is pinned in
tests/test_lifecycle_cross_tier.py against the real runtime.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from revl import compile_source  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(f"wasm_{name}", _HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lifecycle = _load("lifecycle")
harness = _load("lifecycle_harness")


_SCALAR_MESH = """
service Kv { fn get(k: Int) -> Int  fn set(k: Int, v: Int) }
service Status { fn shared() -> Int }
component Store provides kv: Kv { provide kv { fn get(k) = 0  fn set(k, v) {} } }
component Beacon requires kv: Kv provides status: Status {
  effect kv.set(7, 100) undo kv.set(7, 0)
  provide status { fn shared() = kv.get(7) }
}
"""


def _classify(body_src, extra=""):
    ir = compile_source(_SCALAR_MESH + extra + body_src, "t.rvl")
    test = next(t for t in ir["tests"] if t.get("lifecycle"))
    return lifecycle.classify(ir, test)


def test_scalar_lifecycle_test_is_runnable():
    ok, reason = _classify("""
    lifecycle test "scalar" {
      load Store
      load Beacon
      let s = call status.shared()
      assert s == 0
      unload Beacon
      unload Store
      assert no_residue
    }
    """)
    assert ok, reason
    assert reason == ""


def test_configured_load_is_skipped_with_a_reason():
    # a component with a `config` block does not lower on wasm at all
    ok, reason = _classify(
        """
        lifecycle test "configured" {
          load Configured with { url: "x" }
          unload Configured
          assert no_residue
        }
        """,
        extra="""
        service Q { fn q() -> Int }
        component Configured provides q: Q {
          config { url: Str }
          provide q { fn q() = 1 }
        }
        """,
    )
    assert not ok
    assert "config" in reason


def test_non_scalar_return_binding_is_skipped():
    ok, reason = _classify(
        """
        lifecycle test "str boundary" {
          load Namer
          let n = call names.label()
          assert n == "hi"
          unload Namer
          assert no_residue
        }
        """,
        extra="""
        service Names { fn label() -> Str }
        component Namer provides names: Names { provide names { fn label() = "hi" } }
        """,
    )
    assert not ok
    assert "non-scalar" in reason


def test_build_spec_test_reduces_call_method_to_op():
    ir = compile_source(_SCALAR_MESH + """
    lifecycle test "scalar" {
      load Store
      load Beacon
      let s = call status.shared()
      assert s == 0
      unload Beacon
      unload Store
      assert no_residue
    }
    """, "t.rvl")
    test = next(t for t in ir["tests"] if t.get("lifecycle"))
    spec = lifecycle.build_spec_test(test)
    call = next(s for s in spec["steps"] if s["step"] == "call")
    assert call["key"] == "status" and call["op"] == "shared" and call["bind"] == "s"
    assert [s["step"] for s in spec["steps"]] == [
        "load", "load", "call", "assert", "unload", "unload", "assert_no_residue"]


@pytest.mark.parametrize("expr,env,expected", [
    ({"kind": "lit", "value": 42}, {}, 42),
    ({"kind": "var", "name": "x"}, {"x": 7}, 7),
    ({"kind": "bin", "op": "==", "left": {"kind": "var", "name": "x"},
      "right": {"kind": "lit", "value": 7}}, {"x": 7}, True),
    ({"kind": "bin", "op": "+", "left": {"kind": "lit", "value": 2},
      "right": {"kind": "lit", "value": 3}}, {}, 5),
    ({"kind": "unary", "op": "-", "operand": {"kind": "lit", "value": 4}}, {}, -4),
])
def test_executor_scalar_eval(expr, env, expected):
    assert harness._eval(expr, env) == expected


def test_executor_rejects_non_scalar_expr():
    with pytest.raises(harness._StepError):
        harness._eval({"kind": "call", "name": "Some"}, {})
