"""Java backend tests: IR v1/v2/v3 -> cordis4j.

String-level assertions run everywhere; the javac gate compiles emitted
sources against the stubbed cordis4j API in ./stubs (and runs emitted test
blocks) when a working JDK is present.
"""

import json
import os
import re
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
# A red golden is a REVIEW prompt, not a wall: the goldens are snapshot tests
# (docs/conformance.md, "Golden policy: snapshot, not freeze"), so regenerating
# and reviewing the diff is always an acceptable resolution. Every golden
# assertion says which command regenerates it.
_TAIL = ("If the change is intended: python3 tools/regen_goldens.py {t}, then review "
         "the diff. Goldens are snapshots, not a freeze (docs/conformance.md).")

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


def test_format_emits_a_concatenation_chain():
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
    # item 433 F1: a `format` node is a concatenation chain, not String.format.
    # The only conversion this emitter ever produced was `%s`, which is
    # `String.valueOf` for every non-Formattable argument (and no revl value is
    # Formattable), so the two are output-identical while the concatenation
    # compiles to one `invokedynamic makeConcatWithConstants` instead of
    # allocating a varargs array, a Formatter and a re-parsed specifier list on
    # every call.
    assert '"hi " + x' in src
    assert "String.format(" not in src


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


def test_compensate_routes_through_revl_frame_two_phase_loop():
    """item 243 Slice 2b / 247: an `emit ... compensate` no longer joins the
    raw `fx.track(Disposables.of(...))` stack (the OLD placeholder that fired
    on every teardown, TCK a5 pre-respec). It routes through
    `RevlFrame.compensation`, which enqueues for Phase 2 instead of running
    inline (docs/design/teardown-contract.md)."""
    src = (
        "service Bus { emission fn send(n: Int) -> Int }\n"
        "service S { fn f(x: Int) -> Int }\n"
        "component C requires bus: Bus provides s: S {\n"
        "  emit bus.send(1) compensate bus.send(0)\n"
        "  provide s { fn f(x) = x }\n"
        "}\n"
    )
    out = emit.emit(compile_source(src))
    assert "private static final class RevlFrame" in out
    assert 'frame.compensation("send", "send", () -> bus.send(0L))' in out
    assert "frame.commit();" in out
    assert "frame.runPhase2();" in out
    # the raw unconditional-replay shape must be gone for this entry
    assert "Disposables.of(() -> bus.send(0L))" not in out


def test_bracket_only_component_has_no_revl_frame():
    """A component using ONLY plain `acquire`/`undo` brackets — no witnessed
    extern, no `compensate` — must keep emitting exactly as before this
    slice: no RevlFrame, plain `Disposables.of(...)` teardown. Byte-identical
    precedent (docs/design/243-witnessed-externs.md 'Slice 1 as implemented'
    #3: 'every non-witnessed program emits byte for byte as before'),
    extended here to compensation."""
    src = (
        "service KV { fn count() -> Int  emission fn put(key: Str, value: Str) }\n"
        "component MemKV provides kv: KV {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide kv {\n"
        "    fn count() = store.size()\n"
        "    fn put(key, value) {\n"
        "      effect store.insert(key, value)\n"
        "      undo   store.remove(key)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    out = emit.emit(compile_source(src))
    assert "RevlFrame" not in out
    assert "Disposables.of(() -> store.drop())" in out


def test_witnessed_effect_routes_through_revl_frame_transactional():
    """item 243: a witnessed effect in effect position registers the
    extern's DECLARED inverse as a `transactional` entry (Ok-conditional),
    not a bracket — the accumulator owns the undo, no site-spelled one."""
    src = (
        "type Stash = { path: Str }\n"
        "type FsError = { code: Str }\n"
        "extern pure fn unstash(w: Stash) -> Unit = @java { return; } = @py { return }\n"
        "extern witnessed[fs] fn stash() -> Result[Stash, FsError] undo unstash(result)"
        " = @java { return new RevlResult.Ok<>(new Stash(\"p\")); }"
        " = @py { return Ok({'path': 'p'}) }\n"
        "component C {\n"
        "  effect stash()\n"
        "}\n"
    )
    out = emit.emit(compile_source(src))
    assert "private static final class RevlFrame" in out
    assert "instanceof RevlResult.Ok<?, ?>" in out
    assert 'frame.transactional("stash", "unstash", () -> unstash(result))' in out
    # no site-spelled bracket registration for the witnessed call
    assert "frame.bracket(" not in out


def test_method_body_witnessed_routes_through_transactional_method():
    """item 318 (the per-tool-call H1 seam): a witnessed effect inside a
    PROVIDE-METHOD body registers the extern's DECLARED inverse into the
    ENCLOSING COMPONENT's activation frame via `transactionalMethod` — the
    entry is tracked on the provider struct's activation-scope `fx`/`frame`
    (`this.fx`/`this.frame`), so it outlives the method call and is disposed by
    the component's own unload, not at method-return."""
    ir = compile_files([str(HERE / "scenarios" / "method_witnessed.rvl")])
    out = emit.emit(ir)
    # the method routes through transactionalMethod, Ok-conditional, no bracket
    assert 'frame.transactionalMethod("stash_path", "unstash", () -> unstash(result))' in out
    assert "instanceof RevlResult.Ok<?, ?>" in out
    assert "frame.bracket(" not in out
    # the frame-bearing apply returns the reject-reachable RevlActivation, whose
    # abort() flips the commit discriminator so the next unload reverts
    assert "public static final class RevlActivation implements Disposable" in out
    assert "return new RevlActivation(fx, frame);" in out
    assert "void abort() {" in out
    # the provider struct holds the component activation scope + frame
    assert "AgentOps(Context ctx, Context.EffectScope fx, RevlFrame frame)" in out


def test_method_body_witnessed_does_not_perturb_non_witnessed_output():
    """Byte-identity gate (docs/design/243-witnessed-externs.md 'Slice 1 as
    implemented' #3): the item-318 method-witnessed wiring is inert for any
    program that registers no witnessed method effect — no RevlFrame, no
    RevlActivation, plain `return fx;`."""
    src = (
        "service Ops { fn ping() -> Int }\n"
        "component C provides ops: Ops {\n"
        "  provide ops { fn ping() { return 1 } }\n"
        "}\n"
    )
    out = emit.emit(compile_source(src))
    assert "RevlFrame" not in out
    assert "RevlActivation" not in out
    assert "transactionalMethod" not in out


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
    assert src == golden, (
        "backends/java/golden/user_cache.java drifted from the emitter. "
        + _TAIL.format(t="java"))


def test_host_objects_are_real_java_runtime_classes():
    src = emit.emit(_ir("user_cache"))
    assert "UnsupportedOperationException" not in src
    # FR-4: the host Map is generic over its value type (learned per site).
    # item 397: the backing map is a thread-safe ConcurrentHashMap.
    assert ("private final java.util.concurrent.ConcurrentHashMap<String, V> values =" in src
            and "new java.util.concurrent.ConcurrentHashMap<>();" in src)
    # the atomic compare-and-set verb (item 397).
    assert "public boolean insert_if_absent(String key, V value)" in src
    assert "return values.putIfAbsent(key, value) == null;" in src
    # `new` is a Java reserved word — the constructor must be renamed.
    assert "public static <V> Map<V> create()" in src
    assert "static Map new()" not in src
    assert "Map.new()" not in src.replace("`Map.new()`", "")
    assert "public void insert(String key, V value)" in src
    assert "public java.util.Optional<V> get(String key)" in src
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
    assert "Map<String> scratch = Map.create();" in src
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


def test_percent_in_template_needs_no_escaping():
    """A literal `%` used to reach String.format unescaped and throw
    UnknownFormatConversionException at runtime (SQL LIKE patterns), so the
    emitter doubled it. item 433 F1 renders a `format` node as a concatenation
    chain, which has no conversion syntax at all, so the `%` is carried
    verbatim and the whole hazard is gone."""
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
    assert '"SELECT 100% of " + m' in src
    assert "100%%" not in src


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


# host `Map.new()` iteration surface — `keys()` / `size()` (roadmap item 86).
# The value-Map builtins `size()`/`keys()` (docs/stdlib-2.0.md §Map) type-check
# on a host `Map.new()` receiver too, and emit lowers both as plain method calls
# on the runtime object. The generated `Map<V>` class therefore has to carry
# them, or the emitted component fails javac (`cannot find symbol: method
# size()`). Mirrors backends/python/tests/test_host_map_iter.py.
_HOST_MAP_ITER_SRC = """
service KV {
  fn count() -> Int
  fn all_keys() -> List[Str]
  emission fn put(key: Str, value: Str)
}

component MemKV provides kv: KV {
  let store = effect Map.new() undo store.drop()

  provide kv {
    fn count()    = store.size()
    fn all_keys() = store.keys()
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""


def test_host_map_backs_keys_and_size():
    """The generated `Map<V>` runtime carries `size`/`keys`, and the provide
    body lowers them as method calls on the store."""
    src = emit.emit(compile_source(_HOST_MAP_ITER_SRC, "memkv.rvl"))
    # runtime methods exist, with value-Map semantics: Int -> long count, and
    # keys in canonical (code-point) order.
    assert "public long size() {" in src
    assert "public java.util.List<String> keys() {" in src
    assert "int ca = a.codePointAt(i), cb = b.codePointAt(j);" in src
    # provide body lowers to method calls on the host object
    assert "return this.store.size();" in src
    assert "return this.store.keys();" in src


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_host_map_iteration(tmp_path):
    """The reproduction gate: before `size`/`keys` were added to the runtime,
    the emitted component failed javac (`cannot find symbol: method size()`)."""
    _javac_compile(tmp_path, emit.emit(compile_source(_HOST_MAP_ITER_SRC, "memkv.rvl")))


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
def test_java_method_witnessed_h1_on_stub_runtime(tmp_path):
    """item 318 — the per-tool-call H1 gate, proven at RUNTIME against REAL
    files on the JVM (stub EffectScope; provide/get/effect are the whole seam
    this proof needs). Mirrors tests/test_provide_method_witnessed.py:

      * a provide-method does a witnessed fs mutation, called PER TOOL CALL;
      * each call registers a transactional inverse into the component's
        activation frame (RevlFrame.transactionalMethod);
      * a clean unload COMMITS — every per-call mutation PERSISTS (deliverable);
      * an abort (RevlActivation.abort() — item 245's reject seam) REVERTS every
        per-call mutation, residue-free (the world is pristine on every path).

    Harness: scenarios/RunMethodWitnessedH1.java; fixture:
    scenarios/method_witnessed.rvl (java-owned)."""
    fixture = HERE / "scenarios" / "method_witnessed.rvl"
    ir = compile_files([str(fixture)])
    out = _javac_compile(tmp_path, emit.emit(ir))
    harness = HERE / "scenarios" / "RunMethodWitnessedH1.java"
    compile_harness = subprocess.run(
        [JAVAC, "--release", "21", "-cp", str(out), "-d", str(out), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_harness.returncode == 0, compile_harness.stderr
    run = subprocess.run(
        [JAVA, "-cp", str(out), "RunMethodWitnessedH1"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "METHOD_WITNESSED_H1_OK" in run.stdout


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
def test_java_two_phase_abort_and_bracket_fault_continue_stub_runtime(tmp_path):
    """docs/design/teardown-contract.md, exit test 3 (loop conformance),
    proven at RUNTIME against the stub EffectScope: two brackets and one
    compensation register on a component whose SECOND (later-registered,
    thus LIFO-first-to-dispose) bracket's undo THROWS.

    Asserts, in one run: (1) continue-and-record — the fault does not stop
    the earlier bracket's undo from running; (2) the original activation
    failure ('boom') is what propagates, not the bracket fault; (3) a5b —
    every Phase-1 entry (both bracket undos) completes before the Phase-2
    compensation fires at all."""
    ir = compile_source(
        """
        service Probe { fn mark(m: Str) -> Int
          fn boomUndo()
          emission fn send(m: Str) -> Int }
        component C requires probe: Probe {
          effect probe.mark("acquire-a")
            undo probe.mark("undo-a")
          effect probe.mark("acquire-b")
            undo probe.boomUndo()
          emit probe.send("emit") compensate probe.send("compensate")
          fail "boom"
        }
        """
    )
    out = _javac_compile(tmp_path, emit.emit(ir))
    runner = tmp_path / "FaultProbe.java"
    runner.write_text(
        "import io.cordis4j.core.Context;\n"
        "import io.cordis4j.core.ServiceKey;\n"
        "public final class FaultProbe {\n"
        "    private static final java.util.List<String> LOG = new java.util.ArrayList<>();\n"
        "    static final class Rec implements revl.Components.Probe {\n"
        "        public long mark(String m) { LOG.add(m); return 0L; }\n"
        "        public void boomUndo() {\n"
        "            LOG.add(\"boomUndo-attempted\");\n"
        "            throw new RuntimeException(\"undo-b exploded\");\n"
        "        }\n"
        "        public long send(String m) { LOG.add(m); return 0L; }\n"
        "    }\n"
        "    public static void main(String[] args) throws Exception {\n"
        "        Context ctx = new Context();\n"
        "        ctx.provide(ServiceKey.of(revl.Components.Probe.class), new Rec());\n"
        "        boolean threw = false;\n"
        "        try {\n"
        "            new revl.Components.CPlugin().apply(ctx);\n"
        "        } catch (RuntimeException e) {\n"
        "            threw = \"boom\".equals(e.getMessage());\n"
        "        }\n"
        "        var want = java.util.List.of(\"acquire-a\", \"acquire-b\", \"emit\",\n"
        "                \"boomUndo-attempted\", \"undo-a\", \"compensate\");\n"
        "        if (!threw || !LOG.equals(want)) {\n"
        "            System.err.println(\"FAIL threw=\" + threw + \" LOG=\" + LOG + \" want=\" + want);\n"
        "            System.exit(1);\n"
        "        }\n"
        "        System.out.println(\"TWO_PHASE_ABORT_OK\");\n"
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
        [JAVA, "-cp", str(out), "FaultProbe"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "TWO_PHASE_ABORT_OK" in run.stdout


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no working JDK")
def test_java_compensation_discharges_on_clean_unload_stub_runtime(tmp_path):
    """TCK a5a (docs/design/teardown-contract.md exit test 1): a clean,
    successful unload DISCHARGES an activation-time compensation — it never
    runs. The forward emission is the deliverable; best-effort cleanup on
    success is wrong (247 decision 1)."""
    ir = compile_source(
        """
        service Bus { emission fn send(n: Int) -> Int }
        component C requires bus: Bus {
          emit bus.send(1) compensate bus.send(0)
        }
        """
    )
    out = _javac_compile(tmp_path, emit.emit(ir))
    runner = tmp_path / "CommitProbe.java"
    runner.write_text(
        "import io.cordis4j.core.Context;\n"
        "import io.cordis4j.core.Disposable;\n"
        "import io.cordis4j.core.ServiceKey;\n"
        "public final class CommitProbe {\n"
        "    private static final java.util.List<String> LOG = new java.util.ArrayList<>();\n"
        "    static final class Rec implements revl.Components.Bus {\n"
        "        public long send(long n) { LOG.add(\"send:\" + n); return 0L; }\n"
        "    }\n"
        "    public static void main(String[] args) throws Exception {\n"
        "        Context ctx = new Context();\n"
        "        ctx.provide(ServiceKey.of(revl.Components.Bus.class), new Rec());\n"
        "        Disposable comp = new revl.Components.CPlugin().apply(ctx);\n"
        "        comp.dispose();\n"
        "        if (!LOG.equals(java.util.List.of(\"send:1\"))) {\n"
        "            System.err.println(\"FAIL LOG=\" + LOG + \" want=[send:1]\");\n"
        "            System.exit(1);\n"
        "        }\n"
        "        System.out.println(\"COMMIT_DISCHARGE_OK\");\n"
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
        [JAVA, "-cp", str(out), "CommitProbe"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "COMMIT_DISCHARGE_OK" in run.stdout


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no working JDK")
def test_java_witnessed_persists_on_commit_reverts_on_abort_stub_runtime(tmp_path):
    """item 243, proven at runtime: a witnessed effect's declared inverse is
    DISCHARGED on a clean commit (the mutation persists) and REPLAYED on a
    mid-activation abort (the mutation reverts) — the load-bearing
    distinction from a bracket, which would replay on both paths."""
    ir = compile_source(
        "type Stash = { path: Str }\n"
        "type FsError = { code: Str }\n"
        "extern pure fn unstash(w: Stash) -> Unit = @java {\n"
        "    revl.TransactLog.LOG.add(\"undo:\" + w.path);\n"
        "    return;\n"
        "} = @py { return }\n"
        "extern witnessed[fs] fn stash() -> Result[Stash, FsError] undo unstash(result) = @java {\n"
        "    revl.TransactLog.LOG.add(\"do\");\n"
        "    return new RevlResult.Ok<>(new Stash(\"p\"));\n"
        "} = @py { return Ok({'path': 'p'}) }\n"
        "component Commits {\n"
        "  effect stash()\n"
        "}\n"
        "component Aborts {\n"
        "  effect stash()\n"
        "  fail \"boom\"\n"
        "}\n"
    )
    # Components.java's extern bodies reference revl.TransactLog directly, so
    # it must compile IN THE SAME PASS as Components.java (not via
    # `_javac_compile`, which only ever sees the stubs + Components.java).
    package_dir = tmp_path / "revl"
    package_dir.mkdir()
    (package_dir / "Components.java").write_text(emit.emit(ir), encoding="utf-8")
    (package_dir / "TransactLog.java").write_text(
        "package revl;\n"
        "public final class TransactLog {\n"
        "    public static final java.util.List<String> LOG = new java.util.ArrayList<>();\n"
        "}\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    compile_all = subprocess.run(
        [JAVAC, "--release", "21", "-d", str(out)]
        + [str(s) for s in STUB_SOURCES]
        + [str(package_dir / "TransactLog.java"), str(package_dir / "Components.java")],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_all.returncode == 0, compile_all.stderr
    runner = tmp_path / "WitnessProbe.java"
    runner.write_text(
        "import io.cordis4j.core.Context;\n"
        "public final class WitnessProbe {\n"
        "    public static void main(String[] args) throws Exception {\n"
        "        Context commitCtx = new Context();\n"
        "        var commitHandle = new revl.Components.CommitsPlugin().apply(commitCtx);\n"
        "        commitHandle.dispose();\n"
        "        if (!revl.TransactLog.LOG.equals(java.util.List.of(\"do\"))) {\n"
        "            System.err.println(\"COMMIT FAIL LOG=\" + revl.TransactLog.LOG);\n"
        "            System.exit(1);\n"
        "        }\n"
        "        revl.TransactLog.LOG.clear();\n"
        "        Context abortCtx = new Context();\n"
        "        boolean threw = false;\n"
        "        try {\n"
        "            new revl.Components.AbortsPlugin().apply(abortCtx);\n"
        "        } catch (RuntimeException e) {\n"
        "            threw = true;\n"
        "        }\n"
        "        if (!threw || !revl.TransactLog.LOG.equals(java.util.List.of(\"do\", \"undo:p\"))) {\n"
        "            System.err.println(\"ABORT FAIL threw=\" + threw + \" LOG=\" + revl.TransactLog.LOG);\n"
        "            System.exit(1);\n"
        "        }\n"
        "        System.out.println(\"WITNESSED_TEARDOWN_OK\");\n"
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
        [JAVA, "-cp", str(out), "WitnessProbe"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "WITNESSED_TEARDOWN_OK" in run.stdout


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


def test_renaming_collision_is_no_longer_lossy():
    """A3 renaming is table-free but now INJECTIVE, so `double` and `double_`
    land on two distinct Java names instead of one.

    This case used to be REFUSED: the rename appended `_` while the name was
    reserved, which sent both onto `double_`, and refusing beat silently
    emitting one method twice. The rename now shifts the whole `kw`/`kw_`/`kw__`
    ladder up one rung, so the program is accepted and both callables survive.
    `_check_fn_name_collisions` stays as a belt-and-braces assertion of the
    property (see its docstring); it can no longer fire on a keyword rename."""
    ir = compile_source(
        """
        fn double(n: Int) -> Int { return n * 2 }
        fn double_(n: Int) -> Int { return n * 4 }
        fn use_both(n: Int) -> Int { return double(n) + double_(n) }
        """
    )
    src = emit.emit(ir)
    assert src.count("long double_(long n)") == 1
    assert src.count("long double__(long n)") == 1
    assert "return Math.addExact(double_(n), double__(n));" in src


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


# A wildcarded payload (`Found(_) => ..`) has no name to bind. The parser
# records that as `bind == "_"` (a real, non-empty string), not `None` — the
# same shape the cordis-rs backend hit (roadmap item 186 / the Rust
# wildcard-payload fix). Before this fix every case-bind site here (the plain
# sealed-interface switch, the built-in Result switch, and the built-in Opt
# `.map` lambda) treated any truthy bind as a real name and declared
# `final var _ = ..;` / `.map(_ -> ..)` — a literal `_` local or lambda
# parameter, which `javac --release 21` refuses without --enable-preview
# ("as of release 9, '_' is a keyword, and may not be used as an identifier";
# JEP 443/456 only lifts that under the preview flag this tier does not pass).
_WILDCARD_PAYLOAD_SRC = """
type Outcome = Found(Int) | Missing
fn has(o: Outcome) -> Bool { return match o { Found(_) => true, Missing => false } }
fn firstOpt(x: Opt[Int]) -> Bool { return match x { Some(_) => true, None => false } }
fn firstRes(x: Result[Int, Str]) -> Bool { return match x { Ok(_) => true, Err(_) => false } }
"""


def test_wildcard_payload_arm_does_not_declare_a_literal_underscore():
    src = emit.emit(compile_source(_WILDCARD_PAYLOAD_SRC))
    assert "case Outcome.Found" in src
    # the malformed constructs this regression guards against, verbatim.
    assert "final var _ " not in src
    assert "final var _=" not in src
    assert "(_ ->" not in src
    assert "map(_ ->" not in src


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_wildcard_payload_match(tmp_path):
    _javac_compile(tmp_path, emit.emit(compile_source(_WILDCARD_PAYLOAD_SRC)))


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
    # item 433 F8: the empty map is the preallocated immutable singleton, not
    # a fresh mutable HashMap — every writer above copies before it mutates, so
    # nothing ever writes through it (48 B per escaping evaluation, against 0).
    assert "return java.util.Map.of();" in src
    assert "new java.util.HashMap<>()" not in src


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_the_map_value_type(tmp_path):
    _javac_compile(tmp_path, emit.emit(compile_source(MAP_SRC + STDLIB_SRC)))


# --------------------------------------------------------------------------
# FR-4 (FEATURE-REQUESTS.md FR-4 / docs/v2.0-roadmap.md item 77(c)) — non-
# String values in the HOST Map. The session ledger (`Map[Str, List[Msg]]`)
# used to emit a hardcoded `HashMap<String, String>` and fail javac
# ("incompatible types: List<Msg> cannot be converted to String"); the host
# Map is now generic over its value type, learned per site from the IR's
# `insert` calls.
# --------------------------------------------------------------------------

LEDGER_SRC = """
type Msg = { role: Str, content: Str }
service SessionStore {
  fn load(id: Str) -> List[Msg]
  emission fn append(id: Str, msg: Msg)
}
component SessionLedger provides sessions: SessionStore {
  let store = effect Map.new() undo store.drop()
  provide sessions {
    fn load(id) = store.get(id) ?? []
    fn append(id, msg) {
      let prev = store.get(id) ?? []
      effect store.insert(id, prev.push(msg))
      undo   store.insert(id, prev)
    }
  }
}
"""

HOST_MAP_TYPES_SRC = """
service Counters {
  fn get(k: Str) -> Int
  fn put(k: Str, v: Int)
}
component Counters provides counters: Counters {
  let store = effect Map.new() undo store.drop()
  provide counters {
    fn get(k) = store.get(k) ?? 0
    fn put(k, v) { effect store.insert(k, v) undo store.remove(k) }
  }
}
service Tags {
  fn get(k: Str) -> List[Str]
  fn put(k: Str, v: List[Str])
}
component Tags provides tags: Tags {
  let store = effect Map.new() undo store.drop()
  provide tags {
    fn get(k) = store.get(k) ?? []
    fn put(k, v) { effect store.insert(k, v) undo store.remove(k) }
  }
}
service Flags {
  fn get(k: Str) -> Bool
  fn put(k: Str, v: Bool)
}
component Flags provides flags: Flags {
  let store = effect Map.new() undo store.drop()
  provide flags {
    fn get(k) = store.get(k) ?? false
    fn put(k, v) { effect store.insert(k, v) undo store.remove(k) }
  }
}
"""

RECORD_VALUE_SRC = """
type Profile = { name: Str, age: Int }
service ProfileStore {
  fn get(k: Str) -> Opt[Profile]
  fn put(k: Str, p: Profile)
}
component Profiles provides profiles: ProfileStore {
  let store = effect Map.new() undo store.drop()
  provide profiles {
    fn get(k) = store.get(k)
    fn put(k, p) { effect store.insert(k, p) undo store.remove(k) }
  }
}
"""


def test_ledger_shape_carries_the_map_value_type():
    """The session-ledger shape: the emitted provider field and constructor
    pin `Map<java.util.List<Msg>>` (FR-4), the host Map class is generic, and
    the historical hardcoding is gone."""
    src = emit.emit(compile_source(LEDGER_SRC))
    assert "public static final class Map<V>" in src
    assert "private final java.util.concurrent.ConcurrentHashMap<String, V> values" in src
    assert "public void insert(String key, V value)" in src
    assert "public java.util.Optional<V> get(String key)" in src
    assert "public static <V> Map<V> create()" in src
    assert "private final Map<java.util.List<Msg>> store;" in src
    assert "Map<java.util.List<Msg>> store = Map.create();" in src
    assert "HashMap<String, String>" not in src


def test_int_and_list_map_values_reach_the_emitted_types():
    src = emit.emit(compile_source(HOST_MAP_TYPES_SRC))
    assert "private final Map<java.lang.Long> store;" in src
    assert "private final Map<java.util.List<String>> store;" in src
    assert "private final Map<java.lang.Boolean> store;" in src
    assert "Map<java.lang.Long> store = Map.create();" in src
    assert "Map<java.util.List<String>> store = Map.create();" in src
    assert "Map<java.lang.Boolean> store = Map.create();" in src
    src = emit.emit(compile_source(RECORD_VALUE_SRC))
    assert "private final Map<Profile> store;" in src
    assert "Map<Profile> store = Map.create();" in src


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_ledger_shaped_map_component(tmp_path):
    """The FR-4 exit criterion: the harness's core state shape (session ledger
    `Map[Str, List[Msg]]`, Msg a record) COMPILES on the java tier."""
    _javac_compile(tmp_path, emit.emit(compile_source(LEDGER_SRC)))


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_int_bool_and_list_map_values(tmp_path):
    """Map[Str, Int], Map[Str, Bool] and Map[Str, List[Str]] host maps."""
    _javac_compile(tmp_path, emit.emit(compile_source(HOST_MAP_TYPES_SRC)))


@pytest.mark.skipif(JAVAC is None, reason="no working javac")
def test_javac_compiles_record_map_value(tmp_path):
    """A record value type (`Map[Str, Profile]`) — the ledger's Msg minus the
    List wrapper."""
    _javac_compile(tmp_path, emit.emit(compile_source(RECORD_VALUE_SRC)))


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no working JDK")
def test_java_runs_non_string_map_values_on_the_stub_runtime(tmp_path):
    """Runtime proof, not just a compile: a Map[Str, Int] and a Map[Str,
    List[Str]] host map are driven through the stub reference runtime —
    insert/get round-trip non-String values, absent keys fall back, and
    teardown leaves nothing behind (the no-residue shape)."""
    out = _javac_compile(tmp_path, emit.emit(compile_source(HOST_MAP_TYPES_SRC)))
    runner = tmp_path / "RunHostMaps.java"
    runner.write_text(
        "public class RunHostMaps {\n"
        "    public static void main(String[] args) throws Exception {\n"
        "        io.cordis4j.core.Context ctx = new io.cordis4j.core.Context();\n"
        "        var d = new revl.Components.CountersPlugin().apply(ctx);\n"
        "        revl.Components.Counters c = ctx.get(revl.Components.Counters.class);\n"
        "        c.put(\"a\", 7L);\n"
        "        if (c.get(\"a\") != 7L) {\n"
        "            System.err.println(\"Map[Str, Int] round-trip failed\");\n"
        "            System.exit(1);\n"
        "        }\n"
        "        if (c.get(\"missing\") != 0L) {\n"
        "            System.err.println(\"Map[Str, Int] absent-key fallback failed\");\n"
        "            System.exit(1);\n"
        "        }\n"
        "        d.dispose();\n"
        "        ctx = new io.cordis4j.core.Context();\n"
        "        d = new revl.Components.TagsPlugin().apply(ctx);\n"
        "        revl.Components.Tags t = ctx.get(revl.Components.Tags.class);\n"
        "        t.put(\"t\", java.util.List.of(\"x\", \"y\"));\n"
        "        if (!t.get(\"t\").equals(java.util.List.of(\"x\", \"y\"))) {\n"
        "            System.err.println(\"Map[Str, List[Str]] round-trip failed\");\n"
        "            System.exit(1);\n"
        "        }\n"
        "        if (!t.get(\"none\").isEmpty()) {\n"
        "            System.err.println(\"Map[Str, List[Str]] absent-key fallback failed\");\n"
        "            System.exit(1);\n"
        "        }\n"
        "        d.dispose();\n"
        "        System.out.println(\"HOST_MAPS_OK\");\n"
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
        [JAVA, "-cp", str(out), "RunHostMaps"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "HOST_MAPS_OK" in run.stdout


# ---------------------------------------------------------------------------
# Emission determinism: no host address may reach the emitted Java.
#
# The emitter used to name a destructure temporary and a witnessed step's
# Result/Ok temporaries from the AST node's `id()`. That is a host memory
# address, so emitting the SAME IR twice produced two different Java sources
# (`__revl_destructure_4313623040` in one process, `__revl_destructure_4391233664`
# in the next) — an irreproducible build, and the reason
# scenarios/crashproof/revl/Components.java had to be exempted from the golden
# drift check (`tools/regen_goldens.py`'s `unstable`). The reference tier hit
# the same bug and fixed it the same way (item 179,
# backends/python/emit.py's `_Lines._destructure_seq`); the rust tier has always
# indexed by emission order (`env.wit_counter`). These pin the property so no
# future gensym reaches for `id()` again.
# ---------------------------------------------------------------------------

_DESTRUCTURE_SRC = """
type Row = { id: Int, name: Str }

fn one(row: Row) -> Int {
  let {id, name} = row
  return id + name.length
}

fn two(row: Row) -> Int {
  let {id, name} = row
  return id * 2
}

fn three(xs: List[Int]) -> Int {
  let [head, ...rest] = xs
  return head + rest.length
}
"""

_ADDRESSY = re.compile(r"__revl_destructure_(\d+)|_revl_wit(\d+)|_revl_ok(\d+)")


def _emitted_gensym_indices(java: str) -> list[int]:
    return [int(next(g for g in m.groups() if g is not None))
            for m in _ADDRESSY.finditer(java)]


def test_destructure_temporaries_are_indexed_by_emission_order():
    java = emit.emit(compile_source(_DESTRUCTURE_SRC))
    names = sorted(set(re.findall(r"__revl_destructure_\d+", java)))
    assert names == ["__revl_destructure_1", "__revl_destructure_2",
                     "__revl_destructure_3"], (
        "a destructure temporary is not named from its emission order. If it is "
        f"named from `id(node)` the emitted Java is irreproducible: {names}")


def test_no_generated_local_carries_a_host_address():
    """A gensym index is a small ordinal. An `id()` is a 10-plus digit address —
    the shape this refuses, for every generated local in one sweep."""
    for java in (emit.emit(compile_source(_DESTRUCTURE_SRC)),
                 emit.emit(_crashproof_ir(), "revl", record=True)):
        oversized = [i for i in _emitted_gensym_indices(java) if i > 10_000]
        assert not oversized, (
            "a generated local is named from a host address, not an emission "
            f"index: {oversized}")


def _crashproof_ir() -> dict:
    return json.loads(
        (HERE / "scenarios" / "crashproof" / "crashproof.ir.json").read_text(
            encoding="utf-8"))


def test_emission_is_byte_identical_in_a_fresh_process():
    """The property the indices exist for: two processes, one IR, one output.

    A fresh interpreter lays the heap out differently, so an `id()`-derived name
    differs between the two runs while an emission-order index does not."""
    script = (
        "import importlib.util, json, sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        f"spec = importlib.util.spec_from_file_location('revl_java_emit_subproc', {str(HERE / 'emit.py')!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "from revl import compile_source\n"
        f"src = mod.emit(compile_source({_DESTRUCTURE_SRC!r}))\n"
        f"ir = json.loads(open({str(HERE / 'scenarios' / 'crashproof' / 'crashproof.ir.json')!r}, encoding='utf-8').read())\n"
        "src += mod.emit(ir, 'revl', record=True)\n"
        "sys.stdout.write(src)\n"
    )
    other = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, timeout=600)
    assert other.returncode == 0, other.stderr
    here = (emit.emit(compile_source(_DESTRUCTURE_SRC))
            + emit.emit(_crashproof_ir(), "revl", record=True))
    assert other.stdout == here, (
        "the java emitter is not reproducible: the same IR emitted in a second "
        "process produced different bytes")


def test_crashproof_scenario_matches_the_emitter():
    """`scenarios/crashproof/revl/Components.java` is a committed emitted file
    that used to be regenerated but never byte-compared, because the emitter
    could not reproduce it. It can now, so it is checked like every other
    golden."""
    committed = (HERE / "scenarios" / "crashproof" / "revl" / "Components.java").read_text(
        encoding="utf-8")
    assert emit.emit(_crashproof_ir(), "revl", record=True) == committed, (
        "backends/java/scenarios/crashproof/revl/Components.java drifted from "
        "the emitter. " + _TAIL.format(t="java"))
