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


def test_emit_with_compensate_keeps_the_compensation():
    """An `emit` carrying a `compensate` must route through the modern path.
    The simple renderer emitted the emission but silently dropped its
    compensation — a lost teardown (G7 residue) that javac never catches
    because the output still compiles. Regression: the compensation call
    must appear in the emitted Java."""
    src = (
        "service Bus { emission fn send(n: Int) -> Int }\n"
        "service S { fn f(x: Int) -> Int }\n"
        "component C requires bus: Bus provides s: S {\n"
        "  emit bus.send(1) compensate bus.send(0)\n"
        "  provide s { fn f(x) = x }\n"
        "}\n"
    )
    out = emit.emit(compile_source(src)).replace(" ", "")
    assert "send(1" in out, "emission missing"
    assert "send(0" in out, "compensation dropped — G7 residue on the java tier"


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
    # Pool.execute reports rows-affected; 1 matches python/TS/rust (the
    # java tier used to return 0 — a cross-tier divergence this pins shut).
    assert "return 1L;" in src


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
    assert 'Job.run("migrations").await();' in src
    assert "UnsupportedOperationException" not in src


def test_await_joins_the_job_handle_it_starts():
    """A1: "evaluate expr, await its result" — not "evaluate expr".

    The step used to lower to a bare `Job.run("migrations");`. That starts the
    job and drops the handle, so activation returns with the job still in
    flight; the iteration boundary the runtime may divert at never closes.
    Rust never had the bug because its `Job::run` is an `async fn` and the
    step lowers to `.await`; Java has no await operator, so the join has to be
    emitted.
    """
    src = emit.emit(_ir("migrator"))
    assert 'Job.run("migrations");' not in src, "the handle is dropped, not awaited"
    assert 'Job.run("migrations").await();' in src
    # ...and there has to be a handle to join in the first place.
    assert "public static Job run(String name)" in src
    assert "public String await()" in src
    assert "public static long pending()" in src
    assert "public static void run(String name)" not in src


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


def test_host_call_in_a_top_level_function_is_a_method_call():
    """A host method call in a v3 top-level function — `Pool.open(..)`,
    `p.execute(..)` — is a *method invocation*, not the application of a
    functional field. `_v3_call` special-cased only `var` callees, so a
    `field` callee fell through to the generic `.apply(..)` form and emitted
    `Pool.open.apply(url, 3L)`, which does not compile ("package Pool does not
    exist"); ts/rust/python all lower this to a real call. And because the
    call never produced a `host` node, the Pool runtime class was omitted too,
    so the emitted method referenced a nonexistent `Pool`."""
    ir = compile_source(
        """
        pub fn poolExec(url: Str) -> Int {
          let p = Pool.open(url, 3)
          return p.execute("INSERT")
        }
        """
    )
    src = emit.emit(ir)
    assert "Pool.open(url, 3L)" in src
    assert 'p.execute("INSERT")' in src
    assert ".apply(" not in src, "a host method call must not lower to `.apply(..)`"
    # ...and the runtime class it calls into has to be emitted.
    assert "public static final class Pool" in src
    assert "public static Pool open(String url, long poolSize)" in src


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


@pytest.mark.skipif(
    JAVAC is None or JAVA is None or not CORDIS4J_CLASSES,
    reason="needs a JDK and REVL_CORDIS4J_CLASSES (compiled cordis4j-core classes)",
)
def test_instance_accessor_on_real_cordis4j(tmp_path):
    """The instance accessor (`s.<key>`) exit criterion for the Java tier
    (docs/design-v2-instances.md "Instance accessor — frozen"): reading a
    provision back through a spawn handle, driven on the REAL cordis4j runtime.

    Proves, by RUNNING, the three DoD properties: (1) positive —
    `s.<key>.method(..)` through a spawn handle returns THAT instance's
    provision (w1 -> 7, w2 -> 9); (2) per-instance — the two handles resolve
    distinct realms (neither reaches the other's); (3) negative — the root
    (a stand-in for any sibling) cannot resolve the instance's provision, which
    lives in the worker's private local realm. Fixture:
    backends/java/scenarios/instance_accessor.rvl (java-owned)."""
    fixture = HERE / "scenarios" / "instance_accessor.rvl"
    ir = compile_files([str(fixture)])
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(emit.emit(ir), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    harness = HERE / "scenarios" / "RunInstanceAccessor.java"
    compile_all = subprocess.run(
        [JAVAC, "--release", "21", "-cp", CORDIS4J_CLASSES, "-d", str(out),
         str(pkg / "Components.java"), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_all.returncode == 0, compile_all.stderr
    run = subprocess.run(
        [JAVA, "-cp", f"{CORDIS4J_CLASSES}{os.pathsep}{out}", "RunInstanceAccessor"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "INSTANCE_ACCESSOR_OK a=7 b=9" in run.stdout


@pytest.mark.skipif(
    JAVAC is None or JAVA is None or not CORDIS4J_CLASSES,
    reason="needs a JDK and REVL_CORDIS4J_CLASSES (compiled cordis4j-core classes)",
)
def test_global_realm_divergence_characterized(tmp_path):
    """CHARACTERIZES a KNOWN divergence, it does not assert conformance.

    revl's contract (docs/design-v2-realms.md) is that equal realm-label
    strings denote the SAME realm. That holds at runtime on cordis-py,
    cordis (TS) and cordis-rs, but is FALSE on cordis4j at the level revl's
    emitter targets: the emitter emits `ctx.isolate(Svc.class, "t")` inside
    each component's apply() (emit.py:2127-2129), and core Context.isolate
    always mints a fresh child with its own store
    (cordis4j core/internal/ContextImpl.java:160-168, ServiceRegistry.java:41);
    the label-keyed interning lives one layer up in Loader
    (core/Loader.java:67, :341-359), which the emitted plugins never reach.

    This runs revl's own emitted code (examples/tenants.rvl) on the REAL
    cordis4j jar and pins the divergent behavior: two components naming
    realm("tenant_a") for `kv` do NOT conflict/share (both load
    independently), a consumer in realm "tenant_a" does NOT resolve a
    provider in realm "tenant_a", and — the one conforming direction —
    distinct realm strings still separate. The harness fails loudly if the
    sharing directions START to conform, which is the signal to close the
    errata entry ("cordis4j global-realm divergence") and flip the Java
    xfail in tests/test_realm_conformance.py.
    """
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(emit.emit(ir), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    harness = HERE / "scenarios" / "RunRealmDivergence.java"
    compile_all = subprocess.run(
        [JAVAC, "--release", "21", "-cp", CORDIS4J_CLASSES, "-d", str(out),
         str(pkg / "Components.java"), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_all.returncode == 0, compile_all.stderr
    run = subprocess.run(
        [JAVA, "-cp", f"{CORDIS4J_CLASSES}{os.pathsep}{out}", "RunRealmDivergence"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "REALM_DIVERGENCE_CHARACTERIZED" in run.stdout


@pytest.mark.skipif(
    JAVAC is None or JAVA is None or not CORDIS4J_CLASSES,
    reason="needs a JDK and REVL_CORDIS4J_CLASSES (compiled cordis4j-core classes)",
)
def test_runtime_values_on_real_cordis4j(tmp_path):
    """Runtime *values* on the REAL cordis4j jar, the companion to the
    lifecycle-ordering exit criterion (test_runtime_scenarios_on_real_cordis4j).

    Where that harness pins call ORDER, this one pins the VALUES the
    consolidated expression renderer (commit d87d87e), the stdlib typed
    overloads and the Pool host runtime produce, plus method-time `compensate`
    ordering on a real EffectScope — coverage that was previously only
    javac-compiled or golden-matched:
      - match branch selection + payload binding (Ok(row)/NotFound/Invalid(msg))
      - string interpolation carrying a literal `%`
      - Str-vs-List stdlib overload dispatch (slice/concat/indexOf/split/join/
        repeat/push)
      - Pool capacity accounting (open/acquire/release/close/exhaustion)
      - method-time compensations run LIFO at teardown
    Fixture: backends/java/scenarios/runtime_values.rvl (java-owned)."""
    fixture = HERE / "scenarios" / "runtime_values.rvl"
    ir = compile_files([str(fixture)])
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(emit.emit(ir), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    checks = HERE / "scenarios" / "RuntimeValueChecks.java"
    harness = HERE / "scenarios" / "RunRuntimeValues.java"
    compile_all = subprocess.run(
        [JAVAC, "--release", "21", "-cp", CORDIS4J_CLASSES, "-d", str(out),
         str(pkg / "Components.java"), str(checks), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_all.returncode == 0, compile_all.stderr
    run = subprocess.run(
        [JAVA, "-cp", f"{CORDIS4J_CLASSES}{os.pathsep}{out}", "RunRuntimeValues"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "RUNTIME_VALUES_OK" in run.stdout


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no working JDK")
def test_java_runs_runtime_values_on_stub_runtime(tmp_path):
    """Offline mirror of test_runtime_values_on_real_cordis4j: the same
    runtime-independent value checks (RuntimeValueChecks) plus method-time
    ordering, driven on the JVM by the stub reference runtime
    (scenarios/RunRuntimeValuesStub.java). The pure-value checks execute the
    emitted renderer/host-runtime output identically to the real-jar run; only
    the EffectScope construction differs (concrete `new Context()` vs the real
    Contexts.create() façade)."""
    fixture = HERE / "scenarios" / "runtime_values.rvl"
    ir = compile_files([str(fixture)])
    out = _javac_compile(tmp_path, emit.emit(ir))
    checks = HERE / "scenarios" / "RuntimeValueChecks.java"
    harness = HERE / "scenarios" / "RunRuntimeValuesStub.java"
    compile_harness = subprocess.run(
        [JAVAC, "--release", "21", "-cp", str(out), "-d", str(out),
         str(checks), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_harness.returncode == 0, compile_harness.stderr
    run = subprocess.run(
        [JAVA, "-cp", str(out), "RunRuntimeValuesStub"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "RUNTIME_VALUES_OK" in run.stdout


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
def test_java_activation_leaves_no_job_pending(tmp_path):
    """The A1 boundary must *close*: run the plugin, count the residue.

    `Job.pending()` counts handles still in flight. A component whose body is
    `await Job.run(...)` must leave zero of them once `apply` returns — if the
    step only evaluates the call, the handle is dropped and the count is one.
    This is the runtime half of `test_await_joins_the_job_handle_it_starts`;
    the string assertion alone could not tell a joined handle from a leaked
    one.
    """
    ir = compile_source(
        """
        service S { fn f(x: Int) -> Int }
        component Boot provides s: S {
          await Job.run("boot")
          provide s { fn f(x) = x }
        }
        """
    )
    out = _javac_compile(tmp_path, emit.emit(ir))
    runner = tmp_path / "RunAwait.java"
    runner.write_text(
        "public class RunAwait {\n"
        "    public static void main(String[] args) throws Exception {\n"
        "        io.cordis4j.core.Context ctx = new io.cordis4j.core.Context();\n"
        "        revl.Components.Job.reset();\n"
        "        var d = new revl.Components.BootPlugin().apply(ctx);\n"
        "        long pending = revl.Components.Job.pending();\n"
        "        if (pending != 0L) {\n"
        "            System.err.println(\"left \" + pending + \" job(s) pending\");\n"
        "            System.exit(1);\n"
        "        }\n"
        "        d.dispose();\n"
        "        System.out.println(\"NO_PENDING_JOBS\");\n"
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
        [JAVA, "-cp", str(out), "RunAwait"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "NO_PENDING_JOBS" in run.stdout


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


# --- ADT bindings and switch totality ----------------------------------------
# The conformance matrix's `expr/ADT construct + match` failed javac twice over
# once the emitted java was handed to a real compiler (docs/conformance.md).

_ADT_MATCH_SRC = """
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


def test_adt_binding_is_declared_with_the_sealed_interface():
    """`let o = Found(x)` has type `Outcome` in revl, but `var` would freeze
    the java binding at `Outcome.Found` — and then the `Missing` arm of the
    switch is a pattern the selector can never match ("incompatible types:
    Found cannot be converted to Missing")."""
    src = emit.emit(compile_source(_ADT_MATCH_SRC))
    assert "Outcome o = new Outcome.Found(x);" in src
    assert "var o = new Outcome.Found" not in src


def test_a_total_switch_gets_no_default_label():
    """Arms covering every case of a sealed ADT are already exhaustive to
    javac, which then rejects the extra `default` outright."""
    src = emit.emit(compile_source(_ADT_MATCH_SRC))
    assert "case Outcome.Found" in src
    assert "case Outcome.Missing" in src
    assert "non-exhaustive match" not in src


def test_a_partial_switch_keeps_its_guard():
    src = emit.emit(compile_source(
        "type Outcome = Found(Int) | Missing | Broken\n"
        "fn f(o: Outcome) -> Int { return match o { Found(v) => v, _ => 0 } }"
    ))
    assert "default -> { yield (0L); }" in src


# --- arrows ------------------------------------------------------------------

_ARROW_SRC = """
fn add_n(n: Int) -> Int { let f = x => x + n  return f(1) }
fn twice() -> Int { let g = x => x * 2  return g(3) + g(10) }
fn capture_by_value() -> Int { var n = 1  let f = y => y + n  n = 100  return f(5) }
fn compose(a: Int) -> Int { let h = x => x + 1  let sq = x => x * x  return sq(h(a)) }
fn aliased(a: Int) -> Int { let g = x => x + 1  let h = g  return h(a) }
"""


def test_local_arrows_are_beta_reduced_at_the_call_site():
    """An arrow's parameters are untyped (typecheck.py's unchecked frontier),
    so there is no functional interface to declare the binding with and no
    `g(n)` call syntax for a lambda-valued local. The call is inlined instead,
    which is also the only lowering that does not invent a parameter type."""
    src = emit.emit(compile_source(_ARROW_SRC))
    assert "-> (" not in src, "an arrow must not be emitted as a java lambda"
    assert "return ((((3L) * 2L)) + (((10L) * 2L)));" in src  # inlined twice
    # by-value capture (syntax-2.0 §3.5): snapshot at the binding, not the call
    assert "final var __revl_capture_f_n = n;" in src
    assert "return (((5L) + __revl_capture_f_n));" in src


def test_an_arrow_in_value_position_is_refused():
    with pytest.raises(emit.EmitError, match="not lowerable on the Java tier"):
        emit.emit(compile_source(
            "fn boxed(a: Int) -> Int { let fs = [x => x + 1]  return a }"))


def test_an_arrow_argument_is_never_evaluated_twice():
    """Substitution is only safe for a pure, cheap argument; anything else
    would run once per occurrence of the parameter."""
    with pytest.raises(emit.EmitError, match="more than once"):
        emit.emit(compile_source(
            "fn step(n: Int) -> Int { return n + 1 }\n"
            "fn sq(a: Int) -> Int { let s = x => x * x  return s(step(a)) }"))


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_adt_matches_and_arrows(tmp_path):
    _javac_compile(tmp_path, emit.emit(compile_source(_ADT_MATCH_SRC + _ARROW_SRC)))


# ---- Map value type (docs/stdlib-2.0.md §Map) ------------------------------

MAP_SRC = """
pub fn newTable() -> Map[Str, Int] { return Map.empty() }
pub fn put(m: Map[Str, Int], k: Str, v: Int) -> Map[Str, Int] { return m.set(k, v) }
pub fn get(m: Map[Str, Int], k: Str) -> Int { return m.lookup(k) ?? 0 - 1 }
pub fn member(m: Map[Str, Int], k: Str) -> Bool { return m.has(k) }
"""


def test_map_value_type_lowers_to_persistent_hashmaps():
    """Text-level: set goes through the copying static (never `m.put`), and
    lookup answers the tier's Optional."""
    src = emit.emit(compile_source(MAP_SRC))
    assert "revlMapSet(m, k, v)" in src
    assert "revlMapGet(m, k)" in src
    assert "revlMapHas(m, k)" in src
    # the copying helper itself is emitted exactly once per file
    assert src.count("private static <V> java.util.Map<String, V> revlMapSet(") == 1
    # the empty map is a diamond-inferred HashMap
    assert "return new java.util.HashMap<>();" in src


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_the_map_value_type(tmp_path):
    _javac_compile(tmp_path, emit.emit(compile_source(MAP_SRC + STDLIB_SRC)))
