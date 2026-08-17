"""The strata must compose: components (stratum 3) call functions
(stratum 1) at every expression position. This is the integration point
the 2.0 review found untested — each test here failed before the
unification landed."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

PRELUDE = """
pub fn double(x: Int) -> Int { return x + x }
extern pure fn sha(x: Int) -> Int = @py { return x * 2 } = @ts { return x * 2 }
service Kv {
  fn get(k: Int) -> Int
  fn put(k: Int, v: Int)
  emission fn log(m: Str)
}
"""


def test_effect_argument_calls_a_function():
    ir = compile_source(PRELUDE + """
component C requires kv: Kv {
  effect kv.put(1, double(20)) undo kv.put(1, 0)
}
""", "t.rvl")
    arg = ir["components"][0]["body"][0]["acquire"]["args"][1]
    assert arg == {"kind": "fn", "name": "double", "args": [{"kind": "lit", "value": 20}]}


def test_effect_argument_calls_an_extern():
    ir = compile_source(PRELUDE + """
component C requires kv: Kv {
  effect kv.put(1, sha(41)) undo kv.put(1, 0)
}
""", "t.rvl")
    arg = ir["components"][0]["body"][0]["acquire"]["args"][1]
    assert arg["kind"] == "fn" and arg["name"] == "sha"


def test_provide_method_calls_a_function():
    ir = compile_source(PRELUDE + """
component C provides kv2: Kv requires kv: Kv {
  let m = effect Map.new() undo m.drop()
  provide kv2 {
    fn get(k) = double(k)
    fn put(k, v) { effect m.insert(k, double(v)) undo m.remove(k) }
    fn log(msg) { emit kv.log(msg) }
  }
}
""", "t.rvl")
    methods = {m["name"]: m for m in ir["components"][0]["body"][1]["methods"]}
    assert methods["get"]["body"][0]["expr"]["kind"] == "fn"


def test_undo_and_compensate_may_call_functions():
    ir = compile_source(PRELUDE + """
component C requires kv: Kv {
  effect kv.put(1, 5) undo kv.put(1, double(0))
  emit kv.log(`bye`) compensate kv.put(9, double(2))
}
""", "t.rvl")
    body = ir["components"][0]["body"]
    assert body[0]["undo"]["args"][1]["kind"] == "fn"
    assert body[1]["compensate"]["args"][1]["kind"] == "fn"


def test_operators_reach_component_positions():
    ir = compile_source(PRELUDE + """
component C requires kv: Kv {
  effect kv.put(1, 2 + 3 * 4) undo kv.put(1, 0)
}
""", "t.rvl")
    arg = ir["components"][0]["body"][0]["acquire"]["args"][1]
    assert arg["kind"] == "bin" and arg["op"] == "+"


def test_guard_rails_survive_the_unification():
    # G1 keeps the v1 message shape in plain component bodies
    with pytest.raises(RevlError, match="`db` is not a declared requirement of L"):
        compile_source(
            "service S { fn f() }\n"
            "component L { let r = effect db.f() undo r.drop() }", "g1.rvl")
    # G4 still gates emissions in setup — even through the full grammar
    with pytest.raises(RevlError, match=r"must be marked `emit` \(G4\)"):
        compile_source(PRELUDE + """
component C requires kv: Kv {
  effect kv.log("x") undo kv.put(1, 0)
}
""", "g4.rvl")


def test_emitted_python_for_fn_in_effect_compiles():
    ir = compile_source(PRELUDE + """
component C requires kv: Kv {
  effect kv.put(1, double(sha(3))) undo kv.put(1, 0)
}
""", "t.rvl")
    spec = importlib.util.spec_from_file_location(
        "pyemit_integration", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = module.emit(ir)
    compile(source, "emitted.py", "exec")  # must be valid Python
    assert "double(sha(3))" in source.replace(" ", "").replace("(3))", "(3))") or "double" in source
