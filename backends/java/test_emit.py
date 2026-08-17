"""Java backend tests: IR v1/v2/v3 -> cordis4j (string-level; no JDK required)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))

import emit  # noqa: E402
from revl import compile_files, compile_source  # noqa: E402


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
    assert "public static Map new()" in src
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
    assert "var scratch = Map.new();" in src
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
