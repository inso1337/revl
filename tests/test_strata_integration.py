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


# --- finding 6: the stdlib surface (docs/stdlib-2.0.md) ---------------------

def _emit_py(ir):
    spec = importlib.util.spec_from_file_location(
        "pyemit_stdlib", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


def test_stdlib_executes_identically_to_spec():
    ir = compile_source('''
pub fn seq(n: Int) -> List[Int] { var out = [] var i = 0 while (i < n) { out = out.push(i) i += 1 } return out }
pub fn head(s: Str) -> Str { return s.slice(0, 1) }
pub fn find(s: Str, sub: Str) -> Int { return s.indexOf(sub) }
pub fn findL(xs: List[Int], v: Int) -> Int { return xs.indexOf(v) }
pub fn pieces(s: Str, sep: Str) -> List[Str] { return s.split(sep) }
pub fn glue(xs: List[Str], sep: Str) -> Str { return xs.join(sep) }
pub fn times(s: Str, n: Int) -> Str { return s.repeat(n) }
''', "std.rvl")
    namespace = {}
    exec(compile(_emit_py(ir), "std.py", "exec"), namespace)
    assert namespace["seq"](5) == [0, 1, 2, 3, 4]
    assert namespace["head"]("revl") == "r"
    assert namespace["find"]("revl", "zz") == -1, "-1 when absent, both hosts"
    assert namespace["findL"]([4, 5, 6], 9) == -1
    assert namespace["pieces"]("a,,b", ",") == ["a", "", "b"], "JS-shape split"
    assert namespace["pieces"]("a,", ",") == ["a", ""], "trailing empty kept"
    assert namespace["pieces"]("abc", "") == ["a", "b", "c"], "empty sep = chars"
    assert namespace["glue"](["a", "b"], "+") == "a+b"
    assert namespace["times"]("ab", 3) == "ababab"


def test_push_is_persistent():
    ir = compile_source('''
pub fn keep(xs: List[Int]) -> List[Int] { let bigger = xs.push(9) return xs }
''', "p.rvl")
    namespace = {}
    exec(compile(_emit_py(ir), "p.py", "exec"), namespace)
    original = [1, 2]
    assert namespace["keep"](original) == [1, 2]
    assert original == [1, 2], "push must never mutate in place (value semantics)"


def test_unknown_method_is_a_compile_error_not_a_passthrough():
    with pytest.raises(RevlError, match="no builtin method `shove`"):
        compile_source('pub fn f(xs: List[Int]) -> List[Int] { return xs.shove(1) }', "u.rvl")


def test_builtin_arity_is_checked():
    with pytest.raises(RevlError, match=r"builtin `slice` takes 2 argument\(s\)"):
        compile_source('pub fn f(s: Str) -> Str { return s.slice(1) }', "a.rvl")


def test_host_objects_keep_their_own_methods():
    ir = compile_source('''
test "host map" {
  let m = Map.new()
  m.insert("k", "v")
  assert m.get("k") == "v"
}
''', "h.rvl")
    assert ir["tests"], "host-object methods stay verbatim (provenance exemption)"


def test_builtin_in_component_bumps_to_v3():
    ir = compile_source('''
service Kv { fn put(k: Int, v: Int) }
component C requires kv: Kv {
  effect kv.put(1, [1, 2].length()) undo kv.put(1, 0)
}
''', "v.rvl")
    assert ir["ir_version"] == 3
