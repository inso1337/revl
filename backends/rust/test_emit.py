"""Rust backend tests: IR v1 -> cordis-rs, verified by `cargo check` when present."""

import json
import shutil
import subprocess
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


def test_user_cache_emits_rust_structure():
    src = emit.emit(_ir("user_cache"))
    assert "pub trait Database: Send + Sync" in src
    assert "pub trait Cache: Send + Sync" in src
    assert "pub fn pg_database() -> cordis::PluginHandle" in src
    assert "pub fn user_cache() -> cordis::PluginHandle" in src
    assert 'ctx.provide("db"' in src
    assert 'ctx.require::<Box<dyn Database>>("db")' in src
    assert 'ctx.effect("UserCache.store.undo"' in src
    assert "Box<dyn Cache>" in src
    # Rust `Drop::drop` is a destructor (E0040) — revl `drop` must be renamed.
    assert "drop_" in src
    assert ".drop()" not in src
    # Host objects are a real runtime now, not `todo!()` stubs.
    assert "todo!()" not in src


def test_rejects_unsupported_ir_version():
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit({"ir_version": 4, "components": [{"name": "X", "body": []}]})


def test_accepts_ir_versions_1_2_3_and_rejects_4():
    base = {"services": {}, "components": [{"name": "C", "requires": {}, "provides": {}, "body": []}]}
    emit.emit({**base, "ir_version": 1})
    emit.emit({**base, "ir_version": 2})
    emit.emit({**base, "ir_version": 3, "types": {}, "functions": [], "externs": [], "tests": []})
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit({**base, "ir_version": 4})


def test_user_cache_golden_byte_equality():
    src = emit.emit(_ir("user_cache"))
    golden = (Path(__file__).parent / "golden" / "user_cache.rs").read_text(encoding="utf-8")
    assert src == golden


def test_config_defaults_are_emitted():
    src = emit.emit(_ir("user_cache"))
    assert "impl Default for PgDatabaseConfig" in src
    assert "url: String::new()" in src
    assert "pool_size: 10i64" in src
    assert "let config = PgDatabaseConfig" in src
    assert "..Default::default()" in src


def test_v2_realms_emit_isolate_and_intercept():
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    assert ir["ir_version"] == 2
    src = emit.emit(ir)
    assert "fn _revl_realm" in src
    assert 'isolate_with("kv", _revl_realm("tenant_a"))' in src
    assert 'require_with("kv", TenantAAppKvIntercept1' in src
    assert "ctx: Arc<cordis::Context>" in src
    assert "self.ctx.effect" in src


def test_v3_types_functions_match_emit():
    ir = compile_source(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)
        fn add(a: Int, b: Int) -> Int { return a + b }
        fn describe(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            NotFound => "not found",
            Invalid(why) => why,
          }
        }
        """
    )
    assert ir["ir_version"] == 3
    src = emit.emit(ir)
    assert "pub struct Row" in src
    assert "pub enum Outcome" in src
    assert "Outcome::Ok(row)" in src
    assert "fn add(a: i64, b: i64) -> i64" in src


def test_extern_requires_rs_body():
    with pytest.raises(emit.EmitError, match="no @rs body"):
        emit.emit(
            {
                "ir_version": 3,
                "types": {},
                "functions": [],
                "externs": [
                    {
                        "name": "host_call",
                        "class": "pure",
                        "params": [],
                        "returns": "Unit",
                        "bodies": {"py": "pass"},
                    }
                ],
                "tests": [],
            }
        )


def test_component_await_uses_plugin_async():
    ir = {"ir_version": 1, "services": {}, "components": [
        {"name": "C", "requires": {}, "provides": {}, "body": [
            {"step": "await", "expr": {"kind": "host", "fn": "Job.run",
                                      "args": [{"kind": "lit", "value": "x"}]}}]}]}
    src = emit.emit(ir)
    assert "cordis::plugin_async::<(), _, _>" in src
    assert "|ctx, config| async move {" in src
    assert 'Job::run(String::from("x")).await;' in src


def _cargo_check(tmp_path: Path, src: str) -> subprocess.CompletedProcess:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    return subprocess.run(
        ["cargo", "check", "--offline"], cwd=tmp_path, text=True,
        capture_output=True, timeout=600,
    )


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_against_cordis_rs(tmp_path):
    result = _cargo_check(tmp_path, emit.emit(_ir("user_cache")))
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_v2_realms(tmp_path):
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_v3_types_functions_match(tmp_path):
    ir = compile_source(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)
        fn add(a: Int, b: Int) -> Int { return a + b }
        fn make_row(id: Int, name: Str) -> Row { return { id: id, name: name } }
        fn describe(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            NotFound => "not found",
            Invalid(why) => why,
          }
        }
        """
    )
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_v3_host_await_fail_block_effect(tmp_path):
    ir = {
        "ir_version": 3,
        "services": {},
        "components": [
            {
                "name": "Demo",
                "config": [],
                "requires": {},
                "provides": {},
                "body": [
                    {
                        "step": "let-effect",
                        "bind": "store",
                        "setup": [
                            {"step": "let", "name": "key", "value": {"kind": "lit", "value": "k"}}
                        ],
                        "acquire": {"kind": "host", "fn": "Map.new", "args": []},
                        "undo": {
                            "kind": "call",
                            "target": {"kind": "name", "id": "store"},
                            "method": "drop",
                            "args": [],
                        },
                    },
                    {
                        "step": "if",
                        "cond": {"kind": "lit", "value": True},
                        "then": [
                            {"step": "fail", "message": {"kind": "lit", "value": "boom"}}
                        ],
                        "else": [
                            {"step": "fail", "message": {"kind": "lit", "value": "unexpected"}}
                        ],
                    },
                    {
                        "step": "await",
                        "expr": {
                            "kind": "host",
                            "fn": "Job.run",
                            "args": [{"kind": "lit", "value": "job"}],
                        },
                    },
                ],
            }
        ],
        "types": {},
        "functions": [],
        "externs": [],
        "tests": [],
    }
    src = emit.emit(ir)
    assert "pub struct Map" in src
    assert "pub async fn run" in src
    assert "return Err(cordis::CordisError::with_message" in src
    result = _cargo_check(tmp_path, src)
    assert result.returncode == 0, result.stderr
