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


def test_rejects_unsupported_ir_version():
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit({"ir_version": 4, "components": [{"name": "X", "body": []}]})


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


def test_migrator_await_is_rejected():
    # A1 `await` steps are not yet supported by the spike.
    ir = {"ir_version": 1, "services": {}, "components": [
        {"name": "C", "requires": {}, "provides": {}, "body": [
            {"step": "await", "expr": {"kind": "name", "id": "x"}}]}]}
    with pytest.raises(emit.EmitError, match="unsupported component step"):
        emit.emit(ir)


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
