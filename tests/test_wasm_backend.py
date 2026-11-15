"""Substrate-tier backend: WAT emission (pure) and the end-to-end demo
against the cordis-wasm runtime (skips when that project/venv is absent)."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
# A red golden is a REVIEW prompt, not a wall: the goldens are snapshot tests
# (docs/conformance.md, "Golden policy: snapshot, not freeze"), so regenerating
# and reviewing the diff is always an acceptable resolution. Every golden
# assertion says which command regenerates it.
_TAIL = ("If the change is intended: python3 tools/regen_goldens.py {t}, then review "
         "the diff. Goldens are snapshots, not a freeze (docs/conformance.md).")

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
        golden = (ROOT / "backends" / "wasm" / "golden" / f"{name}.wat").read_text(encoding="utf-8")
        assert modules[name] == golden, (
            f"backends/wasm/golden/{name}.wat drifted from the emitter. "
            + _TAIL.format(t="wasm"))


def test_pulse_await_lowering():
    """A1 on the substrate: the await segment launches the async host op and
    the boundary yield structure survives in the golden."""
    ir = compile_files([str(ROOT / "examples" / "pulse.rvl")])
    pulse = _emitter().emit(ir)["Pulse"]
    golden = (ROOT / "backends" / "wasm" / "golden" / "Pulse.wat").read_text(encoding="utf-8")
    assert pulse == golden, (
        "backends/wasm/golden/Pulse.wat drifted from the emitter. "
        + _TAIL.format(t="wasm"))
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
    # `match` in a method body (legal since 46173d0)
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
    # `startsWith`/`endsWith` (FR-6) run through the byte-prefix helpers
    ("str-prefix-true", _component("{ let s = x.to_str()  return s.startsWith(\"12\") ? 1 : 0 }"),
     123, 1),
    ("str-prefix-false", _component("{ let s = x.to_str()  return s.startsWith(\"9\") ? 1 : 0 }"),
     123, 0),
    ("str-prefix-empty", _component("{ let s = x.to_str()  return s.startsWith(\"\") ? 1 : 0 }"),
     123, 1),
    ("str-suffix-true", _component("{ let s = x.to_str()  return s.endsWith(\"23\") ? 1 : 0 }"),
     123, 1),
    ("str-suffix-false", _component("{ let s = x.to_str()  return s.endsWith(\"9\") ? 1 : 0 }"),
     123, 0),
    # `Str.to_int` (FR-9) parses through $str_to_int; the Opt stays inside
    # the module and unwraps with `??` (the canonical-ABI probe is Int-only)
    ("str-to-int-some", _component("{ let s = x.to_str()  return s.to_int() ?? -1 }"),
     42, 42),
    ("str-to-int-negative",
     _component("{ let s = (0 - x).to_str()  return s.to_int() ?? -1 }"), 7, -7),
    ("str-to-int-empty", _component("{ let o = \"\".to_int()  return o ?? -1 }"),
     0, -1),
    ("str-to-int-garbage", _component("{ let o = \"12a\".to_int()  return o ?? -1 }"),
     0, -1),
    ("str-to-int-overflow", _component("{ let o = \"9223372036854775808\".to_int()  return o ?? -1 }"),
     0, -1),
    ("str-to-int-min", _component("{ let s = x.to_str()  return s.to_int() ?? 0 }"),
     -9223372036854775808, -9223372036854775808),
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
    assert "(func $fn.outer" in wat and "(func $fn.inner" in wat
    assert "(func $fn.unused" not in wat        # only the closure, not the corpus
    assert int(_invoke(tmp_path, "closure", wat, "provide:s.f", 20)[-1]) == 42


@needs_wasmtime
def test_activation_steps_declare_their_own_scratch_locals(tmp_path):
    """A delegated expression inside a body segment belongs to `activate_step`,
    not to a method — its scratch slots have to be declared there or the module
    does not validate. This fixture has no `witnessed`/`compensate` entry (a
    plain bracket only), so `deactivate` renders the LEGACY single-pass shape
    (item 243 Slice 2b's scaffold is additive — see
    test_witnessed_teardown.py for the `deactivate_step` shape a witnessed/
    compensating component gets instead) and its inverse's own delegated
    expression's scratch slot belongs there, not to a method."""
    wat = _emitter().emit(compile_source(
        "service Kv { fn set(k: Int, v: Int) -> Int }\n" + _SVC
        + "component C requires kv: Kv provides s: S {\n"
          "  effect kv.set(1, [7, 8, 9].length()) undo kv.set(1, 0)\n"
          "  provide s { fn f(x) { let xs = [1, 2]  return xs.length() + x } }\n"
          "}"
    ))["C"]
    assert '(func (export "activate_step") (result i32) (local $__revl_tmp i32)' in wat
    assert '(func (export "deactivate") (local $__revl_tmp i32)' in wat
    assert 'deactivate_step' not in wat
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
        # a compound value returned from a service operation. Every value the
        # checker can TYPE is now refused before any tier sees it: a typed
        # compound (`let xs = [1, 2]  return xs`, roadmap 75(b)), and, since
        # `compatible` decides structural-vs-nominal instead of waving it
        # through (F3), a bare record literal too. What still reaches this
        # tier is what the checker is DOCUMENTED to wave through: the gradual
        # frontier. `Any` launders in both directions by design, so a compound
        # bound through an `Any` annotation is admitted by the frontend and
        # meets its width mismatch here, which is exactly the boundary this
        # test is about.
        (_component("{ let r: Any = { a: 1 } return r }"),
         "cannot cross this tier's scalar service boundary"),
        # a compound value passed to a coeffect, reached the same way
        ("service B { fn g(n: Int) -> Int }\n" + _SVC
         + "component C requires b: B provides s: S "
           "{ provide s { fn f(x) { let r: Any = { a: 1 } return b.g(r) } } }",
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


# ---------------------------------------------------------------------------
# Float is a native f64 value on the substrate: locals, record fields, list
# elements, ADT payloads, arithmetic, comparison, equality, `%` (C fmod) and
# the service boundary. These are the positions that used to be refused by
# name; they are EXECUTED on wasmtime here, not merely emitted, because the
# original bug was a module that failed validation at load.
# ---------------------------------------------------------------------------

def _invoke_wat(wat_path: Path, fn: str, *args):
    result = subprocess.run(
        ["wasmtime", "--invoke", fn, str(wat_path), *[str(a) for a in args]],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


@needs_wasmtime
def test_float_value_positions_lower_and_run(tmp_path):
    emitter = _emitter()
    source = (
        "type P = { x: Float, y: Float }\n"
        "pub fn addf(a: Float, b: Float) -> Float = a + b\n"
        "pub fn local(x: Float) -> Float { let y: Float = x + 0.5 return y }\n"
        "pub fn divi(n: Int) -> Float = n / 2\n"
        "pub fn divf(x: Float) -> Float = x / 4.0\n"
        "pub fn modf(a: Float, b: Float) -> Float = a % b\n"
        "pub fn cmpf(x: Float) -> Bool = x < 1.5\n"
        "pub fn eqf(x: Float) -> Bool = x == 1.0\n"
        "pub fn negf(x: Float) -> Float = -x\n"
        "pub fn widen(n: Int) -> Float = n\n"
        "pub fn listf() -> Float = [1.5, 2.5][1]\n"
        "pub fn recf() -> Float = ({ x: 1.5, y: 2.5 }).y\n"
        "pub fn somef(x: Float) -> Float = match Some(x) { Some(v) => v + 0.25, None => 0.0 }\n"
        'test "t" { assert recf() == 2.5 }\n'
    )
    wat_path = tmp_path / "float.wat"
    wat_path.write_text(emitter.emit(compile_source(source))["functions"], encoding="utf-8")

    assert float(_invoke_wat(wat_path, "addf", 1.5, 2.25)) == 3.75
    assert float(_invoke_wat(wat_path, "local", 2.0)) == 2.5
    # `/` is true division even over two Ints (docs/arithmetic.md).
    assert float(_invoke_wat(wat_path, "divi", 7)) == 3.5
    assert float(_invoke_wat(wat_path, "divf", 9.0)) == 2.25
    # `%` is C fmod: a large dividend must not round to zero, and the sign is
    # the dividend's (including -0.0's sign bit).
    assert float(_invoke_wat(wat_path, "modf", 1e300, 7.0)) == 1.0
    assert float(_invoke_wat(wat_path, "modf", -5.5, 2.0)) == -1.5
    assert _invoke_wat(wat_path, "cmpf", 1.0) == "1"
    assert _invoke_wat(wat_path, "cmpf", 2.0) == "0"
    assert _invoke_wat(wat_path, "eqf", 1.0) == "1"
    assert _invoke_wat(wat_path, "eqf", 2.0) == "0"
    assert float(_invoke_wat(wat_path, "negf", 1.5)) == -1.5
    assert float(_invoke_wat(wat_path, "widen", 3)) == 3.0
    assert float(_invoke_wat(wat_path, "listf")) == 2.5
    assert float(_invoke_wat(wat_path, "recf")) == 2.5
    assert float(_invoke_wat(wat_path, "somef", 1.0)) == 1.25


def test_component_float_remainder_includes_the_f64_mod_helper():
    """A provide-method body takes the component renderer, whose helper tail is
    demand-driven: `%` on Float must pull `$f64_mod` in there too, or the module
    calls an undefined function."""
    emitter = _emitter()
    source = (
        "service Math { fn wrap(a: Float) -> Float }\n"
        "component C provides math: Math {\n"
        "  provide math { fn wrap(a: Float) -> Float = a % 3.5 }\n"
        "}\n"
    )
    wat = emitter.emit(compile_source(source))["C"]
    assert "(call $f64_mod)" in wat, wat
    assert "(func $f64_mod" in wat, wat


@needs_wasmtime
def test_float_fuzz_regressions_lower_and_run(tmp_path):
    emitter = _emitter()
    for example, expected in (("fuzz_wasm_ad4e66e8", "3"), ("fuzz_wasm_af371f9d", "0")):
        ir = compile_files([str(ROOT / "examples" / "regressions" / f"{example}.rvl")])
        wat_path = tmp_path / f"{example}.wat"
        wat_path.write_text(emitter.emit(ir)["functions"], encoding="utf-8")
        assert _invoke_wat(wat_path, "probe") == expected


@needs_wasmtime
def test_float_adt_variant_payload_with_match_runs(tmp_path):
    # The roadmap-301 shape verbatim: `type Adt0 = C0_0(Float)` matched over.
    # The payload is a Float slot; the result is f64, so the module validates
    # and runs instead of being refused.
    emitter = _emitter()
    source = (
        "type Adt0 = C0_0(Float)\n"
        "pub fn probe() -> Float { return match C0_0(1.0) { C0_0(v) => v + 0.5 } }\n"
        'test "t" { assert probe() == 1.5 }\n'
    )
    wat_path = tmp_path / "adt.wat"
    wat_path.write_text(emitter.emit(compile_source(source))["functions"], encoding="utf-8")
    assert float(_invoke_wat(wat_path, "probe")) == 1.5


@pytest.mark.skipif(not CORDIS_WASM_PY.exists(), reason="cordis-wasm venv not available")
def test_float_crosses_the_service_boundary_on_the_real_substrate(tmp_path):
    # f64 is a scalar boundary value: a provider's export and a consumer's
    # coeffect import carry it by value, with no pointer into linear memory.
    script = tmp_path / "float_boundary.py"
    script.write_text(
        "import sys\n"
        f"sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT / 'backends' / 'wasm')!r}, "
        f"{str(Path.home() / 'Projects' / 'cordis-wasm')!r}]\n"
        "from revl import compile_source\n"
        "import emit\n"
        "from runtime import Runtime, State\n"
        "mods = emit.emit(compile_source('''\n"
        "service Math { fn scale(a: Float) -> Float }\n"
        "service Out { fn get() -> Float }\n"
        "component C provides m: Math { provide m { fn scale(a: Float) -> Float = a * 2.0 } }\n"
        "component D requires m: Math provides out: Out {\n"
        "  provide out { fn get() -> Float = m.scale(1.5) }\n"
        "}\n"
        "'''))\n"
        "rt = Runtime()\n"
        "c = rt.plug('C', mods['C'])\n"
        "d = rt.plug('D', mods['D'])\n"
        "assert c.state is State.ACTIVE, c.state\n"
        "assert d.state is State.ACTIVE, d.state\n"
        "assert rt.call(d, 'provide:out.get') == 3.0\n"
        "print('float boundary ok')\n",
        encoding="utf-8",
    )
    result = subprocess.run([str(CORDIS_WASM_PY), str(script)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "float boundary ok" in result.stdout


def test_supported_value_shapes_still_emit_no_over_refusal():
    # The refusal must be exact: int/list/record/variant/str programs that carry
    # NO Float still lower and never grow a stray f64 in the module.
    emitter = _emitter()
    source = (
        "type Box = Box(Int)\n"
        "pub fn ints() -> Int { return [1, 2, 3].length() }\n"
        "pub fn recs() -> Int { return [{ a: 1, b: 2 }, { a: 3, b: 4 }].length() }\n"
        "pub fn tagged() -> Int { return match Box(7) { Box(v) => v } }\n"
        "pub fn strs() -> Int { return [\"x\", \"y\"].length() }\n"
        'test "t" { assert ints() == 3 }\n'
    )
    wat = "\n".join(emitter.emit(compile_source(source)).values())
    assert "f64" not in wat


@pytest.mark.skipif(not CORDIS_WASM_PY.exists(), reason="cordis-wasm venv not available")
def test_demo_runs_on_the_real_substrate():
    result = subprocess.run(
        [str(CORDIS_WASM_PY), str(ROOT / "backends" / "wasm" / "demo.py")],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
    assert "[FAIL]" not in result.stdout
