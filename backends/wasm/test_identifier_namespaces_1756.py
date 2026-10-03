"""No two source names share a WAT identifier on the wasm tier (issue #1756).

WHAT WAS WRONG. The emitter built identifiers by joining source names with
`_`, and `_` is also a character of a revl name. So two different things could
get the same identifier, and the module then did not validate:

* `$req_<key>_<op>`: `s.x_y` and `s_x.y` were both `$req_s_x_y`.
* `$route_<key>_<op>`, `$route_live_<key>`, `$route_disp_<key>_<op>`: two
  routed keys collided the same way, including `live.k` (a `$route_live_k`
  dispatch) against the probe of a routed key `k`.
* `$inst_<Component>_<key>_<op>`: spawn templates `W` (key `s_x`) and `W_s`
  (key `x`) collided.
* A user `fn` was `$<name>`, the namespace of the emitter's own helpers, so
  `fn int_add`, `fn alloc`, `fn str_concat` (or a `@wasm` extern `alloc`)
  redefined a helper, and `fn req_s_f` in a component redefined its import.
* `$g_<binding>` met the witness global `$g_wit_val_<n>`.
* A component method-body local was `$<name>`, so `let p_x` met the parameter
  `$p_x` and `var for_ptr_1` met the loop cursor `$for_ptr_1`.
* A `fn` in a component module was also exported under its name, which met the
  module's own `deactivate` / `abort` exports; and a `fn memory` met the
  functions module's `memory` export.

WHAT IS EMITTED NOW. An identifier built from source names is
`$<tag>.<part>.<part>`: `$req.s.x_y`, `$route.k.op`, `$inst.W.s_x.y`,
`$config.f`, `$spawn.T`, `$g.binding`, and `$fn.<name>` for a user `fn` or
extern (`emit._uid`). A revl name has no `.`, so the parts can always be read
back, and the emitter's own fixed names (`$alloc`, `$int_add`, ...) have no
`.`, so nothing built from source names can equal one. The fixed names keep
their spelling because hand-written `@wasm` bodies call them; parameters keep
`$p_<name>` for the same reason, and a method-body local is `$l_<name>`, the
spelling a v3 function already used. A `fn` in a component module is no longer
exported (nothing calls it by export). A `fn memory` cannot be renamed, since
its export is its API, so it is refused by name, on both the reference and the
self-host port.

Every case was measured first on main: each one failed validation. Each case
below is assembled AND validated with wasmtime, and executed on the live
cordis-wasm runtime where it has something to call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))
# tests/ is APPENDED, so its modules resolve only names nothing earlier on
# sys.path provides; `_load_by_path` is the one wanted here (see
# backends/java/test_reserved_word_idents_java.py).
if str(ROOT / "tests") not in sys.path:
    sys.path.append(str(ROOT / "tests"))

from revl import compile_source  # noqa: E402
from _load_by_path import load_by_path  # noqa: E402

emit = load_by_path("revl_wasm_emit_1756", BACKEND / "emit.py")

REQ = """
service A { fn x_y(k: Int) -> Int }
service B { fn y(k: Int) -> Int }
component PA provides s: A { provide s { fn x_y(k) = k + 1 } }
component PB provides s_x: B { provide s_x { fn y(k) = k + 2 } }
service Ops { fn one(k: Int) -> Int
              fn two(k: Int) -> Int }
component C requires s: A, s_x: B provides ops: Ops {
  provide ops { fn one(k) = s.x_y(k)
                fn two(k) = s_x.y(k) }
}
"""


def _routed(key_a: str, op_a: str, key_b: str, op_b: str) -> str:
    """Two routed keys over realms r1/r2; r1 serves first under round robin."""
    return f"""
service A {{ fn {op_a}(r: Int) -> Int }}
service B {{ fn {op_b}(r: Int) -> Int }}
component A1 provides {key_a}: A {{
  isolate {key_a} in realm("r1")
  provide {key_a} {{ fn {op_a}(r) = r + 1 }}
}}
component A2 provides {key_a}: A {{
  isolate {key_a} in realm("r2")
  provide {key_a} {{ fn {op_a}(r) = r + 2 }}
}}
component B1 provides {key_b}: B {{
  isolate {key_b} in realm("r1")
  provide {key_b} {{ fn {op_b}(r) = r + 10 }}
}}
component B2 provides {key_b}: B {{
  isolate {key_b} in realm("r2")
  provide {key_b} {{ fn {op_b}(r) = r + 20 }}
}}
component Router requires {key_a}: A, {key_b}: B provides {key_a}: A, {key_b}: B {{
  isolate {key_a} in realms("r1", "r2") strategy(round_robin)
  isolate {key_b} in realms("r1", "r2") strategy(round_robin)
}}
"""


INST = """
service A { fn y() -> Int }
service B { fn y() -> Int }
component W provides s_x: A { config { id: Int }
  provide s_x { fn y() = config.id } }
component W_s provides x: B { config { id: Int }
  provide x { fn y() = config.id + 100 } }
service Ops { fn one() -> Int }
component C provides ops: Ops {
  let a = effect spawn W with { id: 1 } undo a.dispose()
  let b = effect spawn W_s with { id: 2 } undo b.dispose()
  provide ops { fn one() = a.s_x.y() + b.x.y() }
}
"""

FN_LIKE_IMPORT = """
service S { fn f(k: Int) -> Int }
service Ops { fn go(k: Int) -> Int }
fn req_s_f(k: Int) -> Int { return k * 100 }
component P provides s: S { provide s { fn f(k) = k + 1 } }
component C requires s: S provides ops: Ops {
  provide ops { fn go(k) = s.f(k) + req_s_f(k) }
}
"""

PARAM_VS_LOCAL = """
service S { fn f(x: Int) -> Int }
component P provides s: S {
  provide s { fn f(x) { let p_x = x + 1
                        return p_x + x } }
}
"""

LOCAL_VS_CURSOR = """
service S { fn f(xs: List[Int]) -> Int }
component P provides s: S {
  provide s { fn f(xs) { var for_ptr_1 = 0
                         for (x of xs) { for_ptr_1 = for_ptr_1 + x }
                         return for_ptr_1 } }
}
"""

BINDING_VS_WITNESS = """
type Bracket = { fd: Int }
extern witnessed[t] fn mark(v: Int) -> Result[Int, Str]
    undo revert(result)
    = @wasm {
      (i32.store (i32.const 4096) (i32.const 0))
      (i64.store (i32.const 4104) (local.get $p_v))
      (i32.const 4096)
    }
extern pure fn revert(w: Int) -> Unit = @wasm { }
extern acquire fn tick() -> Bracket undo untick(0) = @wasm { (i32.const 0) }
extern pure fn untick(r: Int) -> Unit = @wasm { }
component C {
  effect mark(7)
  let wit_val_1 = effect tick() undo untick(0)
}
"""

FN_LIKE_EXPORT_IN_COMPONENT = """
service S { fn f(k: Int) -> Int }
fn deactivate(k: Int) -> Int { return k * 2 }
fn abort(k: Int) -> Int { return k * 3 }
component P provides s: S { provide s { fn f(k) = deactivate(k) + abort(k) } }
"""

FN_LIKE_HELPERS = """
fn int_add(a: Int, b: Int) -> Int { return a * b }
fn alloc(n: Int) -> Int { return int_add(n, 2) + 1 }
fn str_concat(s: Str) -> Str { return s.concat("!") }
pub fn probe(n: Int) -> Int { return alloc(n) + int_add(n, n) }
pub fn shout(s: Str) -> Str { return str_concat(s).concat("?") }
"""

EXTERN_LIKE_HELPER = """
extern pure fn alloc(n: Int) -> Int = @wasm { (local.get $p_n) }
pub fn greet(s: Str) -> Str { return s.concat("!") }
pub fn twice(n: Int) -> Int { return alloc(n) }
"""

CASES = {
    "req": REQ,
    "route": _routed("a", "x_y", "a_x", "y"),
    "route_live": _routed("live", "k", "k", "call"),
    "inst": INST,
    "fn_like_import": FN_LIKE_IMPORT,
    "param_vs_local": PARAM_VS_LOCAL,
    "local_vs_cursor": LOCAL_VS_CURSOR,
    "binding_vs_witness": BINDING_VS_WITNESS,
    "fn_like_export_in_component": FN_LIKE_EXPORT_IN_COMPONENT,
    "fn_like_helpers": FN_LIKE_HELPERS,
    "extern_like_helper": EXTERN_LIKE_HELPER,
}


def _wasmtime():
    return pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")


def _modules(source: str) -> tuple[dict, dict]:
    ir = compile_source(source)
    return ir, emit.emit(ir)


# ------------------------------------------------------------ the scheme

def test_an_identifier_built_from_names_reads_back_uniquely():
    """The pairs that collided under `_` are distinct under `.`, and nothing
    built from source names equals one of the emitter's fixed helpers."""
    assert emit._uid("req", "s", "x_y") != emit._uid("req", "s_x", "y")
    assert emit._uid("inst", "W", "s_x", "y") != emit._uid("inst", "W_s", "x", "y")
    assert emit._uid("route", "live", "k") != emit._uid("route_live", "k")
    assert emit._uid("fn", "int_add") == "$fn.int_add" != "$int_add"
    assert emit._uid("g", "wit_val_1") != "$g_wit_val_1"


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_module_validates(case):
    """Each of these failed wasmtime validation on main."""
    wasmtime = _wasmtime()
    _ir, modules = _modules(CASES[case])
    for name, wat in modules.items():
        if isinstance(wat, str) and "(module" in wat:
            wasmtime.Module(wasmtime.Engine(), wasmtime.wat2wasm(wat))  # raises on a duplicate


def test_the_names_are_spelled_by_the_scheme():
    _ir, modules = _modules(REQ)
    assert '(import "coeffect:s" "x_y" (func $req.s.x_y ' in modules["C"]
    assert '(import "coeffect:s_x" "y" (func $req.s_x.y ' in modules["C"]
    _ir, modules = _modules(PARAM_VS_LOCAL)
    assert "(param $p_x i64)" in modules["P"] and "(local $l_p_x i64)" in modules["P"]
    _ir, modules = _modules(FN_LIKE_HELPERS)
    functions = modules["functions"]
    assert '(func $fn.int_add (export "int_add")' in functions
    assert "(func $int_add (param $a i64)" in functions      # the helper keeps its name
    _ir, modules = _modules(FN_LIKE_EXPORT_IN_COMPONENT)
    assert "(func $fn.deactivate (param" in modules["P"]     # not exported from P
    assert '(export "deactivate")' in modules["P"]           # the module's own export


def test_a_fn_named_memory_is_refused_by_name():
    with pytest.raises(emit.EmitError, match="fn `memory` would export wasm name 'memory'"):
        emit.emit(compile_source("pub fn memory(n: Int) -> Int { return n + 1 }\n"))


# ---------------------------------------------------------- executed

def _cordis_runtime():
    _wasmtime()
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    path = Path(root) / "runtime.py"
    if not path.exists():
        pytest.skip(f"cordis-wasm runtime not found at {path} (set CORDIS_WASM)")
    module = load_by_path("cordis_wasm_runtime_1756", path)
    if not hasattr(module, "ROUTE_NS"):
        pytest.skip("cordis-wasm runtime predates the route:<key> primitive (item 173)")
    return module


def _plug(source: str):
    runtime = _cordis_runtime()
    ir, modules = _modules(source)
    rt = runtime.Runtime()
    for template in ir["manifest"].get("templates") or []:
        comp = next(c for c in ir["components"] if c["name"] == template)
        rt.register_template(template, modules[template],
                             [f["name"] for f in comp.get("config") or []])
    fibers = {c: rt.plug(c, modules[c]) for c in ir["manifest"]["loadOrder"]}
    return rt, fibers


@pytest.mark.parametrize("case, component, op, args, want", [
    ("req", "C", "provide:ops.one", (10,), 11),
    ("req", "C", "provide:ops.two", (10,), 12),
    ("route", "Router", "provide:a.x_y", (10,), 11),
    ("route", "Router", "provide:a_x.y", (10,), 20),
    ("route_live", "Router", "provide:live.k", (10,), 11),
    ("route_live", "Router", "provide:k.call", (10,), 20),
    ("inst", "C", "provide:ops.one", (), 103),
    ("fn_like_import", "C", "provide:ops.go", (1,), 102),
    ("param_vs_local", "P", "provide:s.f", (10,), 21),
    ("fn_like_export_in_component", "P", "provide:s.f", (10,), 50),
])
def test_each_component_case_runs(case, component, op, args, want):
    rt, fibers = _plug(CASES[case])
    assert rt.call(fibers[component], op, *args) == want


@pytest.mark.parametrize("case, export, arg, want", [
    # the user `int_add` multiplies: alloc(5) = 5 * 2 + 1, int_add(5, 5) = 25
    ("fn_like_helpers", "probe", 5, 36),
    ("extern_like_helper", "twice", 5, 5),
])
def test_each_functions_module_case_runs(case, export, arg, want):
    wasmtime = _wasmtime()
    _ir, modules = _modules(CASES[case])
    store = wasmtime.Store()
    module = wasmtime.Module(store.engine, modules["functions"])
    instance = wasmtime.Instance(store, module, [])
    assert instance.exports(store)[export](store, arg) == want
