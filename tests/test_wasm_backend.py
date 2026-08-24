"""Substrate-tier backend: WAT emission (pure) and the end-to-end demo
against the cordis-wasm runtime (skips when that project/venv is absent)."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402

CORDIS_WASM_PY = Path.home() / "Projects" / "cordis-wasm" / ".venv" / "bin" / "python"

needs_wasmtime = pytest.mark.skipif(
    shutil.which("wasmtime") is None, reason="wasmtime not installed")


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", ROOT / "backends" / "wasm" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_beacon_emits_goldens():
    ir = compile_files([str(ROOT / "examples" / "beacon.rvl")])
    modules = _emitter().emit(ir)
    for name in ("Beacon", "Auditor"):
        golden = (ROOT / "backends" / "wasm" / "golden" / f"{name}.wat").read_text()
        assert modules[name] == golden


def test_pulse_await_lowering():
    """A1 on the substrate: the await segment launches the async host op and
    the boundary yield structure survives in the golden."""
    ir = compile_files([str(ROOT / "examples" / "pulse.rvl")])
    pulse = _emitter().emit(ir)["Pulse"]
    golden = (ROOT / "backends" / "wasm" / "golden" / "Pulse.wat").read_text()
    assert pulse == golden
    assert '(import "host" "job_run"' in pulse
    # the job name is interned at compile time (the host op is i32-only), so
    # the first distinct name in the module is id 1
    assert "(call $host_job_run (i32.const 1))" in pulse
    # the effect after the await is a separate segment: divert can skip it
    assert pulse.index("job_run (i32.const 1)") < pulse.index("(i64.const 2) (i64.const 22)")


def test_import_section_is_the_coeffect_specification():
    ir = compile_files([str(ROOT / "examples" / "beacon.rvl")])
    beacon = _emitter().emit(ir)["Beacon"]
    assert '(import "coeffect:kv" "get"' in beacon
    assert '(import "coeffect:kv" "set"' in beacon
    assert '(export "provide:status.shared")' in beacon
    # the accumulator: inverses guarded by completed-step count, LIFO
    assert beacon.index("i64.const 8) (i64.const 0)") < beacon.index("i64.const 7) (i64.const 0)")


def test_v2_realms_lower_to_realm_namespaces():
    ir = compile_source(
        """
        service Kv {
          fn get(k: Int) -> Int
          fn set(k: Int, v: Int)
        }
        component StoreA provides kv: Kv {
          isolate kv in realm("tenant_a")
          provide kv {
            fn get(k) { return k }
            fn set(k, v) { }
          }
        }
        component AppA requires kv: Kv {
          isolate kv in realm("tenant_a")
          intercept kv with { quota: 5 }
          effect kv.set(1, 10) undo kv.set(1, 0)
        }
        """
    )
    modules = _emitter().emit(ir)
    assert '(import "coeffect:tenant_a/kv" "set"' in modules["AppA"]
    assert '(export "provide:tenant_a/kv.get")' in modules["StoreA"]
    assert '(@custom "revl:isolate" "{\\"kv\\": \\"tenant_a\\"}")' in modules["AppA"]
    assert '(@custom "revl:intercept" "{\\"kv\\": {\\"quota\\": 5}}")' in modules["AppA"]


def test_tier_restrictions_are_hard_errors():
    emitter = _emitter()
    # user_cache is string-shaped and configured: must be rejected (the
    # config check fires first), never silently degraded
    ir = compile_files([str(ROOT / "examples" / "user_cache.rvl")])
    with pytest.raises(emitter.EmitError, match="not lowerable"):
        emitter.emit(ir)
    # await steps: the runtime implements the sync base calculus
    ir = {
        "ir_version": 1,
        "services": {},
        "components": [{
            "name": "Waiter", "config": [], "requires": {}, "provides": {},
            "body": [{"step": "await", "expr": {"kind": "lit", "value": 1}}],
        }],
    }
    with pytest.raises(emitter.EmitError, match="await"):
        emitter.emit(ir)


# ---------------------------------------------------------------------------
# the component-path renderer: constructs that used to be "unknown expression
# kind X". Emitting them is not the claim — executing them is, so every case
# below is invoked on real wasmtime.
# ---------------------------------------------------------------------------

_SVC = "service S { fn f(x: Int) -> Int }\n"
_PROVIDE = "component C provides s: S {{ provide s {{ fn f(x) {} }} }}"


def _component(body: str) -> str:
    return _SVC + _PROVIDE.format(body)


#: (label, source, argument, expected result)
_COMPONENT_CASES = [
    # `if` (ternary) — i32 select/if is native here
    ("ternary-then", _component("= x > 0 ? 11 : 22"), 5, 11),
    ("ternary-else", _component("= x > 0 ? 11 : 22"), -5, 22),
    # `fn` call nodes: a component body calling a top-level `fn`
    ("fn-call", "fn double(n: Int) -> Int { return n * 2 }\n"
     + _component("= double(x)"), 21, 42),
    ("fn-recursion",
     "fn fib(n: Int) -> Int { if (n < 2) { return n }  return fib(n - 1) + fib(n - 2) }\n"
     + _component("= fib(x)"), 10, 55),
    ("fn-while",
     "fn count(n: Int) -> Int { var i = 0  while (i < n) { i += 1 }  return i }\n"
     + _component("= count(x)"), 7, 7),
    ("fn-arrow", "fn apply(n: Int) -> Int { let g = v => v + 1  return g(n) }\n"
     + _component("= apply(x)"), 41, 42),
    ("fn-verified", "verified fn inc(n: Int) -> Int { return n + 1 }\n"
     + _component("= inc(x)"), 41, 42),
    ("fn-for-of",
     "fn total(xs: List[Int]) -> Int { var t = 0  for (v of xs) { t += v }  return t }\n"
     + _component("{ let xs = [1, 2, 3]  return total(xs) }"), 0, 6),
    # list / record / index / stdlib methods, on the linear-memory model
    ("list-index", _component("{ let xs = [10, 20, 30]  return xs[1] }"), 0, 20),
    ("list-index-dynamic", _component("{ let xs = [10, 20, 30]  return xs[x] }"), 2, 30),
    ("list-length", _component("{ let xs = [1, 2, 3, 4]  return xs.length() }"), 0, 4),
    ("record-field", "type R = { a: Int, b: Int }\n"
     + _component("{ let r = { a: x, b: 5 }  return r.a + r.b }"), 37, 42),
    ("template-in-module", _component("{ let m = `n=${x}!`  return m.length() }"), 123, 6),
    # `match` in a method body (legal since ff0d76e)
    ("match-payload", "type O = Found(Int) | Missing\n"
     + _component("{ let o = Found(x)  return match o { Found(v) => v * 2, Missing => 0 } }"),
     21, 42),
    ("match-unit-case", "type O = Found(Int) | Missing\n"
     + _component("{ let o = Missing  return match o { Found(v) => v * 2, Missing => 99 } }"),
     21, 99),
    # `to_str` renders through the $int_to_str helper; the probe reads back
    # the digit count (a Str does not cross the canonical-ABI probe here)
    ("int-to-str-len", _component("{ let s = x.to_str()  return s.length() }"),
     123, 3),
    ("int-to-str-negative-len",
     _component("{ let s = (0 - x).to_str()  return s.length() }"), 45, 3),
    # `??` on an Opt that never leaves the module
    ("nullish-some", _component("{ let o = Some(x)  return o ?? 7 }"), 42, 42),
    ("nullish-none", _component("{ let o = None  return o ?? 7 }"), 42, 7),
]


def _invoke(tmp_path, label, wat, export, *args):
    path = tmp_path / f"{label}.wat"
    path.write_text(wat, encoding="utf-8")
    out = subprocess.run(
        ["wasmtime", "--invoke", export, str(path), *[str(a) for a in args]],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, f"{label}: {out.stderr}"
    return out.stdout.strip().splitlines()


@needs_wasmtime
@pytest.mark.parametrize("label,source,arg,expected", _COMPONENT_CASES,
                         ids=[case[0] for case in _COMPONENT_CASES])
def test_component_expressions_run_on_wasmtime(tmp_path, label, source, arg, expected):
    wat = _emitter().emit(compile_source(source))["C"]
    output = _invoke(tmp_path, label, wat, "provide:s.f", arg)
    assert int(output[-1]) == expected


@needs_wasmtime
def test_bare_return_lowers_a_void_operation(tmp_path):
    """`{"step": "return", "expr": null}` — the natural body of an operation
    the service declares with no return type."""
    wat = _emitter().emit(compile_source(
        "service V { fn f(x: Int) }\n"
        "component C provides s: V { provide s { fn f(x) { return } } }"
    ))["C"]
    assert "(return)" in wat
    assert _invoke(tmp_path, "bare-return", wat, "provide:s.f", 1) == []


@needs_wasmtime
def test_called_fn_is_emitted_into_the_component_module(tmp_path):
    """A component is one self-contained artifact: the `fn`s it calls are
    lowered into its own module so `call $name` resolves, transitively."""
    wat = _emitter().emit(compile_source(
        "fn inner(n: Int) -> Int { return n + 1 }\n"
        "fn outer(n: Int) -> Int { return inner(n) * 2 }\n"
        "fn unused(n: Int) -> Int { return n }\n"
        + _component("= outer(x)")
    ))["C"]
    assert "(func $outer" in wat and "(func $inner" in wat
    assert "(func $unused" not in wat        # only the closure, not the corpus
    assert int(_invoke(tmp_path, "closure", wat, "provide:s.f", 20)[-1]) == 42


@needs_wasmtime
def test_activation_steps_declare_their_own_scratch_locals(tmp_path):
    """A delegated expression inside a body segment belongs to `activate_step`,
    not to a method — its scratch slots have to be declared there or the module
    does not validate."""
    wat = _emitter().emit(compile_source(
        "service Kv { fn set(k: Int, v: Int) -> Int }\n" + _SVC
        + "component C requires kv: Kv provides s: S {\n"
          "  effect kv.set(1, [7, 8, 9].length()) undo kv.set(1, 0)\n"
          "  provide s { fn f(x) { let xs = [1, 2]  return xs.length() + x } }\n"
          "}"
    ))["C"]
    assert '(func (export "activate_step") (result i32) (local $__revl_tmp i32)' in wat
    assert '(func (export "deactivate") (local $__revl_tmp i32)' in wat
    path = tmp_path / "activation.wat"
    path.write_text(wat, encoding="utf-8")
    # it imports coeffects, so validate by compiling rather than invoking
    out = subprocess.run(["wasmtime", "compile", str(path), "-o", str(tmp_path / "o.cwasm")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_plain_i32_components_declare_no_memory():
    """Linear memory is pulled in only when a value needs it — an arithmetic
    component emits exactly the module it always did (the goldens depend on
    this)."""
    wat = _emitter().emit(compile_source(_component("= x + 1 * 2")))["C"]
    assert "(memory" not in wat and "$alloc" not in wat
    wat = _emitter().emit(compile_source(_component("{ let xs = [1]  return xs.length() }")))["C"]
    assert '(memory (export "memory") 1)' in wat


def test_boundary_refusals_say_why_not_unknown_kind():
    """The distinction that matters: an unhandled node is a bug, a value that
    cannot cross the scalar service boundary is this tier's design."""
    emitter = _emitter()
    for source, expected in [
        # a compound value returned from a service operation. A *typed*
        # compound (`let xs = [1, 2]  return xs`) is now refused by the
        # checker: the provide-method let sweep types the binding (roadmap
        # 75(b)), so `return xs` against an `Int` service return is a compile
        # error before any tier sees it. An anonymous record literal has no
        # recoverable type, so it still reaches this tier's boundary.
        (_component("{ return { a: 1 } }"), "cannot cross this tier's scalar service boundary"),
        # a compound value passed to a coeffect (same story: `b.g(xs)` with
        # `xs: List[Int]` is checker-refused; an anonymous record passes)
        ("service B { fn g(n: Int) -> Int }\n" + _SVC
         + "component C requires b: B provides s: S "
           "{ provide s { fn f(x) { return b.g({ a: 1 }) } } }",
         "cannot cross this tier's scalar coeffect boundary"),
        # an extern with no @wasm body is not a missing case either
        ("extern pure fn h(n: Int) -> Int = @py { return n } = @ts { return n }\n"
         + _component("= h(x)"), "has no @wasm body"),
    ]:
        with pytest.raises(emitter.EmitError, match=expected):
            emitter.emit(compile_source(source))
        # never the bug-shaped message
        try:
            emitter.emit(compile_source(source))
        except emitter.EmitError as error:
            assert "unknown expression kind" not in str(error)


@pytest.mark.skipif(not CORDIS_WASM_PY.exists(), reason="cordis-wasm venv not available")
def test_demo_runs_on_the_real_substrate():
    result = subprocess.run(
        [str(CORDIS_WASM_PY), str(ROOT / "backends" / "wasm" / "demo.py")],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
    assert "[FAIL]" not in result.stdout
