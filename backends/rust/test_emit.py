"""Rust backend tests: IR v1 -> cordis-rs, verified by `cargo check` when present."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import emit  # noqa: E402


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


def test_rejects_v3():
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit({"ir_version": 3, "components": [{"name": "X", "body": []}]})


def test_migrator_await_is_rejected():
    # A1 `await` steps are not yet supported by the spike.
    ir = {"ir_version": 1, "services": {}, "components": [
        {"name": "C", "requires": {}, "provides": {}, "body": [
            {"step": "await", "expr": {"kind": "name", "id": "x"}}]}]}
    with pytest.raises(emit.EmitError, match="unsupported component step"):
        emit.emit(ir)


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_against_cordis_rs(tmp_path):
    src = emit.emit(_ir("user_cache"))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = subprocess.run(
        ["cargo", "check"], cwd=tmp_path, text=True, capture_output=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr
