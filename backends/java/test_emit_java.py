"""Java backend tests: IR v1/v2/v3 -> cordis4j.

String-level assertions run everywhere; the javac gate compiles emitted
sources against the stubbed cordis4j API in ./stubs (and runs emitted test
blocks) when a working JDK is present.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# Load this backend's emitter under a unique module name — a bare
# `import emit` collides with the other backends' emitters when the
# suites run in one pytest invocation.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_java_emit", Path(__file__).resolve().parent / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)
from revl import compile_files, compile_source  # noqa: E402

HERE = Path(__file__).resolve().parent
STUB_SOURCES = sorted((HERE / "stubs").rglob("*.java"))


def _tool(name: str) -> str | None:
    """A toolchain binary that actually works (macOS ships a `javac` shim
    that errors when no JDK is installed)."""
    exe = shutil.which(name)
    if exe is None:
        return None
    try:
        probe = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exe if probe.returncode == 0 else None


JAVAC = _tool("javac")
JAVA = _tool("java")


def _javac_compile(tmp_path: Path, source: str) -> Path:
    """Compile emitted Components.java against the cordis4j stubs; returns
    the classes dir."""
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(source, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    result = subprocess.run(
        [JAVAC, "--release", "21", "-d", str(out)]
        + [str(s) for s in STUB_SOURCES]
        + [str(pkg / "Components.java")],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr
    return out


def _ir(name: str = "user_cache") -> dict:
    return json.loads((ROOT / "examples" / f"{name}.ir.json").read_text())


def test_user_cache_emits_java_structure():
    src = emit.emit(_ir("user_cache"))
    assert "package revl;" in src
    assert "import io.cordis4j.core.ServiceKey;" in src
    assert "public interface Database" in src
    assert "public interface Cache" in src
    assert "implements Plugin" in src
    assert "ctx.get(Database.class)" in src
    assert "ctx.provide(ServiceKey.of(Cache.class)" in src
    assert "Disposables.composite(" in src
    assert "Disposables.of(() -> store.drop())" in src
    # effectful provide-method bodies are lowered to the component effect scope
    assert "Context.EffectScope fx = ctx.effect();" in src
    assert "fx.track(ctx.provide(ServiceKey.of(Cache.class)" in src
    assert "this.db.execute(" in src
    assert 'UnsupportedOperationException("effectful method body")' not in src


def test_format_emits_string_format():
    ir = {
        "ir_version": 1,
        "services": {"Bus": {"methods": {"send": {
            "params": [{"name": "msg", "type": "Str"}], "returns": None, "emission": True}}}},
        "components": [{
            "name": "Notifier", "requires": {"bus": "Bus"}, "provides": {}, "config": [],
            "body": [{"step": "emit", "expr": {
                "kind": "call", "target": {"kind": "req", "name": "bus"}, "method": "send",
                "args": [{"kind": "format", "template": "hi $0", "args": [{"kind": "name", "id": "x"}]}]}}],
        }],
    }
    src = emit.emit(ir)
    assert 'String.format("hi %s", x)' in src


def test_rejects_unknown_ir_version():
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit({"ir_version": 4, "components": [{"name": "X", "body": []}]})


def test_version_gate_accepts_ir_1_2_3():
    v1 = {
        "ir_version": 1,
        "services": {},
        "components": [{"name": "X", "requires": {}, "provides": {}, "config": [], "body": []}],
    }
    assert "ir_version 1" in emit.emit(v1)
    assert "ir_version 2" in emit.emit({**v1, "ir_version": 2})
    v3 = {
        "ir_version": 3,
        "functions": [{
            "name": "answer",
            "params": [],
            "returns": "Int",
            "body": [{"step": "return", "expr": {"kind": "lit", "value": 42}}],
        }],
    }
    assert "ir_version 3" in emit.emit(v3)
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit({**v1, "ir_version": 4})


def test_user_cache_golden_byte_equality():
    src = emit.emit(_ir("user_cache"))
    golden = (Path(__file__).resolve().parent / "golden" / "user_cache.java").read_text()
    assert src == golden


def test_host_objects_are_real_java_runtime_classes():
    src = emit.emit(_ir("user_cache"))
    assert "UnsupportedOperationException" not in src
    assert "private final java.util.HashMap<String, String> values = new java.util.HashMap<>();" in src
    # `new` is a Java reserved word — the constructor must be renamed.
    assert "public static Map create()" in src
    assert "static Map new()" not in src
    assert "Map.new()" not in src.replace("`Map.new()`", "")
    assert "public void insert(String key, String value)" in src
    assert "public java.util.Optional<String> get(String key)" in src
    assert "public static Pool open(String url, long poolSize)" in src
    assert "return java.util.List.of();" in src
    assert "return 0L;" in src


def test_config_defaults_emit_no_arg_constructor():
    src = emit.emit(_ir("user_cache"))
    assert "public PgDatabasePlugin(String url, long pool_size)" in src
    assert "public PgDatabasePlugin()" in src
    assert "this.url = null;" in src
    assert "this.pool_size = 10L;" in src


def test_await_lowers_to_async_plugin():
    src = emit.emit(_ir("migrator"))
    assert "import io.cordis4j.core.AsyncPlugin;" in src
    assert "public static final class MigratorPlugin implements AsyncPlugin" in src
    assert "public Disposable apply(Context ctx) throws Exception" in src
    assert 'Job.run("migrations");' in src
    assert "UnsupportedOperationException" not in src


def test_component_if_setup_and_fail_emit_real_java():
    ir = compile_source(
        """
        component Failing {
          config { replicas: Int = 0 }
          let scratch = effect Map.new() undo scratch.drop()
          if (config.replicas < 1) fail "at least one replica required"
          let pool = effect {
            let url = "postgres://db"
            Pool.open(url, config.replicas)
          } undo pool.close()
        }
        """
    )
    src = emit.emit(ir)
    assert "import io.cordis4j.core.CordisException;" in src
    assert "var scratch = Map.create();" in src
    assert "if ((replicas < 1L))" in src
    assert 'throw new CordisException(String.valueOf("at least one replica required"));' in src
    assert 'final var url = "postgres://db";' in src
    assert "var pool = Pool.open(url, replicas);" in src
    assert "fx.track(Disposables.of(() -> pool.close()));" in src


def test_v2_realms_emit_isolate_and_intercept():
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    assert ir["ir_version"] == 2
    src = emit.emit(ir)
    assert "// Generated by the revl cordis4j backend (ir_version 2)" in src
    assert 'ctx = ctx.isolate(Kv.class, "tenant_a");' in src
    assert 'ctx = ctx.isolate(Kv.class, "tenant_b");' in src
    assert 'ctx.intercept(ServiceKey.of(Kv.class), java.util.Map.of("quota", 5L, "tags", java.util.List.of("tenant_a")));' in src
    assert 'ctx.intercept(ServiceKey.of(Kv.class), java.util.Map.of("quota", 9L, "tags", java.util.List.of("tenant_b")));' in src


def test_v3_types_functions_match_emit_java_switch():
    ir = compile_source(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)

        fn add(a: Int, b: Int) -> Int { return a + b }
        fn describe(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            NotFound => "not found",
            Invalid(msg) => msg,
          }
        }
        """
    )
    assert ir["ir_version"] == 3
    src = emit.emit(ir)
    assert "// Generated by the revl cordis4j backend (ir_version 3)" in src
    assert "public static final class Row" in src
    assert "public sealed interface Outcome" in src
    assert "final class Ok implements Outcome" in src
    assert "public static long add(long a, long b)" in src
    assert "public static String describe(Outcome outcome)" in src
    assert "switch (outcome)" in src
    assert "case Outcome.Ok" in src
    assert "case Outcome.NotFound" in src
    assert "case Outcome.Invalid" in src
    assert "yield (row.name);" in src


def test_v3_extern_requires_java_body():
    ir = compile_source(
        """
        extern pure fn sha256(data: Bytes) -> Str
          = @java { return java.util.HexFormat.of().formatHex(data); }
        """
    )
    src = emit.emit(ir)
    assert "public static String sha256(byte[] data)" in src
    assert "return java.util.HexFormat.of().formatHex(data);" in src

    missing = compile_source(
        "extern pure fn only_ts(data: Bytes) -> Str = @ts { return String(data) }"
    )
    with pytest.raises(emit.EmitError, match="no @java body"):
        emit.emit(missing)


def test_percent_in_template_is_escaped_for_string_format():
    """Review finding: a literal `%` reached String.format unescaped and
    threw UnknownFormatConversionException at runtime (SQL LIKE patterns)."""
    ir = compile_source(
        """
        service Db { emission fn ex(s: Str) -> Int }
        component P requires db: Db {
          let m = effect Map.new() undo m.drop()
          emit db.ex(`SELECT 100% of ${m}`)
        }
        """
    )
    src = emit.emit(ir)
    assert 'String.format("SELECT 100%% of %s", m)' in src


def test_stdlib_builtins_use_typed_overloads():
    """Review finding: slice lowered List-only (subList on a String) and
    concat Str-only (stringifying lists); instanceof ternaries did not even
    compile for receivers statically typed String."""
    ir = compile_source(
        """
        pub fn head(s: Str) -> Str { return s.slice(0, 1) }
        pub fn cat(xs: List[Int], ys: List[Int]) -> List[Int] { return xs.concat(ys) }
        pub fn find(s: Str, sub: Str) -> Int { return s.indexOf(sub) }
        """
    )
    src = emit.emit(ir)
    assert "revlSlice(s, 0L, 1L)" in src
    assert "revlConcat(xs, ys)" in src
    assert "revlIndexOf(s, sub)" in src
    assert "private static String revlSlice(String s, long a, long b)" in src
    assert "private static <T> java.util.List<T> revlConcat" in src


STDLIB_SRC = """
pub fn seq(n: Int) -> List[Int] { var out = [] var i = 0 while (i < n) { out = out.push(i) i += 1 } return out }
pub fn head(s: Str) -> Str { return s.slice(0, 1) }
pub fn find(s: Str, sub: Str) -> Int { return s.indexOf(sub) }
pub fn findL(xs: List[Int], v: Int) -> Int { return xs.indexOf(v) }
pub fn cat(a: Str, b: Str) -> Str { return a.concat(b) }
pub fn catL(xs: List[Int], ys: List[Int]) -> List[Int] { return xs.concat(ys) }
pub fn pieces(s: Str, sep: Str) -> List[Str] { return s.split(sep) }
pub fn glue(xs: List[Str], sep: Str) -> Str { return xs.join(sep) }
pub fn times(s: Str, n: Int) -> Str { return s.repeat(n) }
test "stdlib parity with the python backend" {
  assert seq(5).length() == 5
  assert seq(5)[4] == 4
  assert head("revl") == "r"
  assert find("revl", "zz") == 0 - 1
  assert find("revl", "ev") == 1
  assert findL([4, 5, 6], 9) == 0 - 1
  assert findL([4, 5, 6], 6) == 2
  assert cat("re", "vl") == "revl"
  assert catL([1], [2, 3]).length() == 3
  assert pieces("a,,b", ",").length() == 3
  assert pieces("a,", ",").length() == 2
  assert pieces("abc", "").length() == 3
  assert pieces("a-b", "-")[0] == "a"
  assert glue(pieces("a-b", "-"), "+") == "a+b"
  assert times("ab", 3) == "ababab"
}
"""


# --- the javac gate: emitted Java must actually compile -----------------------

@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_user_cache_against_stubs(tmp_path):
    """The golden path through a real compiler (review finding: `Map.new()`
    shipped inside a byte-equality golden because nothing ran javac)."""
    _javac_compile(tmp_path, emit.emit(_ir("user_cache")))


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_migrator_async(tmp_path):
    _javac_compile(tmp_path, emit.emit(_ir("migrator")))


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_v2_realms(tmp_path):
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    _javac_compile(tmp_path, emit.emit(ir))


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_v3_stdlib_types_match(tmp_path):
    ir = compile_source(
        STDLIB_SRC
        + """
type Row = { id: Int, name: Str }
type Outcome = Ok(Row) | NotFound | Invalid(Str)
fn describe(outcome: Outcome) -> Str {
  return match outcome {
    Ok(row) => row.name,
    NotFound => "not found",
    Invalid(why) => why,
  }
}
pub fn first(xs: List[Int], i: Int) -> Int { return xs[i] }
"""
    )
    _javac_compile(tmp_path, emit.emit(ir))


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_method_level_compensate(tmp_path):
    """`N.ping` must itself be declared `emission`: it reaches `db.ex`, and a
    service declaration is an upper bound on its providers' effects (G4
    emission propagation)."""
    ir = compile_source(
        """
        service Db { fn q(s: Str) -> Int
          emission fn ex(s: Str) -> Int }
        service N { emission fn ping(u: Str) }
        component C requires db: Db provides n: N {
          let m = effect Map.new() undo m.drop()
          provide n {
            fn ping(u) { emit db.ex(u) compensate db.ex(u) }
          }
        }
        """
    )
    _javac_compile(tmp_path, emit.emit(ir))


CORDIS4J_CLASSES = os.environ.get("REVL_CORDIS4J_CLASSES")


@pytest.mark.skipif(
    JAVAC is None or JAVA is None or not CORDIS4J_CLASSES,
    reason="needs a JDK and REVL_CORDIS4J_CLASSES (compiled cordis4j-core classes)",
)
def test_runtime_scenarios_on_real_cordis4j(tmp_path):
    """The A1/G7 exit criterion for the Java tier: emitted components driven
    by the REAL cordis4j runtime (clone github.com/1na-ko/cordis4j, javac
    cordis4j-core/src/main/java, point REVL_CORDIS4J_CLASSES at the classes
    dir — CI does exactly this). Scenarios: G7 LIFO teardown, A8 fail-revert
    with failure routing, reactive inject gating + withdrawal ordering
    (Theorem 63), and the async boundary."""
    fixture = ROOT / "backends" / "rust" / "scenarios" / "probe.rvl"
    ir = compile_files([str(fixture)])
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(emit.emit(ir), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    harness = HERE / "scenarios" / "RunRealScenarios.java"
    compile_all = subprocess.run(
        [JAVAC, "--release", "21", "-cp", CORDIS4J_CLASSES, "-d", str(out),
         str(pkg / "Components.java"), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_all.returncode == 0, compile_all.stderr
    run = subprocess.run(
        [JAVA, "-cp", f"{CORDIS4J_CLASSES}{os.pathsep}{out}", "RunRealScenarios"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "REAL_SCENARIOS_OK" in run.stdout


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no working JDK")
def test_java_runs_runtime_scenarios_on_stub_runtime(tmp_path):
    """G7/lifecycle scenarios: emitted components driven on the JVM by the
    stub reference runtime (LIFO EffectScope/composite). Shares fixtures
    with the Rust backend (../rust/scenarios/probe.rvl); the harness is
    scenarios/RunScenarios.java. A8 and reactive gating need the real
    cordis4j jar — tracked in docs/v2.0-roadmap.md."""
    fixture = ROOT / "backends" / "rust" / "scenarios" / "probe.rvl"
    ir = compile_files([str(fixture)])
    out = _javac_compile(tmp_path, emit.emit(ir))
    harness = HERE / "scenarios" / "RunScenarios.java"
    compile_harness = subprocess.run(
        [JAVAC, "--release", "21", "-cp", str(out), "-d", str(out), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_harness.returncode == 0, compile_harness.stderr
    run = subprocess.run(
        [JAVA, "-cp", str(out), "RunScenarios"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "SCENARIOS_OK" in run.stdout


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no working JDK")
def test_java_runs_emitted_stdlib_semantics(tmp_path):
    """Not just compiles: run the emitted test block (REVL_TESTS) on a JVM —
    persistent push, -1 when absent, spec-table semantics."""
    ir = compile_source(STDLIB_SRC)
    out = _javac_compile(tmp_path, emit.emit(ir))
    runner = tmp_path / "RunRevlTests.java"
    runner.write_text(
        "public class RunRevlTests {\n"
        "    public static void main(String[] args) {\n"
        "        revl.Components.REVL_TESTS.forEach(Runnable::run);\n"
        "        System.out.println(\"REVL_TESTS_OK\");\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    compile_runner = subprocess.run(
        [JAVAC, "--release", "21", "-cp", str(out), "-d", str(out), str(runner)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_runner.returncode == 0, compile_runner.stderr
    run = subprocess.run(
        [JAVA, "-cp", str(out), "RunRevlTests"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr
    assert "REVL_TESTS_OK" in run.stdout


# --------------------------------------------------------------------------
# Conformance gaps closed on this tier (docs/conformance.md). Each construct
# below used to raise instead of lowering; the matrix now reports java as
# clean apart from the deliberate extern refusals.
# --------------------------------------------------------------------------


def test_nullish_lowers_to_or_else_get():
    """`a ?? b` on `Opt[T]` = `java.util.Optional<T>`. `orElseGet` (not
    `orElse`) because `??` must not evaluate its right operand when the left
    is present, and `orElse` takes an already-evaluated value."""
    ir = compile_source(
        """
        fn side(n: Int) -> Int { return n * 3 }
        fn pick(a: Opt[Int]) -> Int { return a ?? side(7) }
        """
    )
    src = emit.emit(ir)
    assert "a.orElseGet(() -> side(7L))" in src
    assert ".orElse(" not in src  # eager form would evaluate `side(7)` always


def test_nullish_lowers_in_component_method_bodies():
    ir = compile_source(
        """
        service Bus { fn maybe(n: Int) -> Opt[Int] }
        service S { fn f(x: Int) -> Int }
        component C requires bus: Bus provides s: S {
          provide s { fn f(x) = bus.maybe(x) ?? 0 }
        }
        """
    )
    src = emit.emit(ir)
    assert "this.bus.maybe(x).orElseGet(() -> 0L)" in src


def test_bare_return_lowers_for_void_service_operations():
    """`{"step": "return", "expr": null}` used to crash with an AttributeError
    in the expression-body fast path."""
    ir = compile_source(
        """
        service S { fn f(x: Int) }
        component C provides s: S { provide s { fn f(x) { return } } }
        """
    )
    src = emit.emit(ir)
    assert "public void f(long x) { return; }" in src


def test_keyword_named_function_is_renamed_not_rejected():
    """`fn double(..)` is legal revl; `double` is a Java keyword. A3 renaming
    (`src/revl/lower.py::_safe_name`) applies at declaration and call sites."""
    ir = compile_source(
        """
        fn double(n: Int) -> Int { return n * 2 }
        service S { fn f(x: Int) -> Int }
        component C provides s: S { provide s { fn f(x) = double(x) } }
        """
    )
    src = emit.emit(ir)
    assert "public static long double_(long n)" in src
    assert "return double_(x);" in src
    # the un-renamed keyword must not survive anywhere as an identifier
    assert "double(" not in src


def test_keyword_named_extern_is_renamed_at_declaration_and_call():
    ir = compile_source(
        """
        extern pure fn native(n: Int) -> Int = @java { return n; } = @py { return n }
        fn use_it(n: Int) -> Int { return native(n) }
        """
    )
    src = emit.emit(ir)
    assert "public static long native_(long n)" in src
    assert "return native_(n);" in src


def test_renaming_collision_is_refused():
    """A3 renaming is table-free, so `double` and `double_` would both land on
    `double_`. That is the one lossy case and it must refuse, not pick one."""
    ir = compile_source(
        """
        fn double(n: Int) -> Int { return n * 2 }
        fn double_(n: Int) -> Int { return n * 4 }
        fn use_both(n: Int) -> Int { return double(n) + double_(n) }
        """
    )
    with pytest.raises(emit.EmitError, match="both lower to the Java name"):
        emit.emit(ir)


def test_match_in_a_component_method_body():
    ir = compile_source(
        """
        type Outcome = Found(Int) | Missing
        service S { fn f(x: Int) -> Int }
        component C provides s: S {
          provide s {
            fn f(x) {
              let o = Found(x)
              return match o { Found(v) => v, Missing => 0 }
            }
          }
        }
        """
    )
    src = emit.emit(ir)
    assert "Outcome.Found" in src
    assert "Outcome.Missing" in src
