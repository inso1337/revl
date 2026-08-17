"""Rust backend tests: IR v1 -> cordis-rs, verified by `cargo check` when present."""

import json
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

_spec = importlib.util.spec_from_file_location("revl_rust_emit", Path(__file__).resolve().parent / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)
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


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_stdlib_builtins_on_str_and_list(tmp_path):
    """Review finding: slice/indexOf/concat previously failed to compile for
    one of {Str, List} each; indexing failed for both (i64 vs usize)."""
    ir = compile_source(STDLIB_SRC + "\npub fn first(xs: List[Int], i: Int) -> Int { return xs[i] }\n")
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_test_runs_emitted_stdlib_semantics(tmp_path):
    """Not just compiles: the emitted #[test] executes the spec's semantics
    (persistent push, -1 when absent, char-based string positions)."""
    ir = compile_source(STDLIB_SRC)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = subprocess.run(
        ["cargo", "test", "--offline"], cwd=tmp_path, text=True,
        capture_output=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_method_level_compensate(tmp_path):
    """Review finding: `emit ... compensate` in a provide method referenced
    `*_undo` clones that were only generated when compensate was absent."""
    ir = compile_source(
        """
        service Db { fn q(s: Str) -> Int
          emission fn ex(s: Str) -> Int }
        service N { fn ping(u: Str) }
        component C requires db: Db provides n: N {
          let m = effect Map.new() undo m.drop()
          provide n {
            fn ping(u) { emit db.ex(u) compensate db.ex(u) }
          }
        }
        """
    )
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_check_compiles_braces_in_templates(tmp_path):
    """Review finding: literal `{`/`}` in a template reached `format!`
    unescaped and broke the format string."""
    ir = compile_source(
        """
        service Db { emission fn ex(s: Str) -> Int }
        component B requires db: Db {
          emit db.ex(`INSERT {"k": 1}`)
        }
        """
    )
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_runtime_scenarios_on_real_cordis_rs(tmp_path):
    """The A1/G7 exit criterion: emitted components driven by the real
    cordis-rs runtime. Fixtures in scenarios/probe.rvl, assertions in
    scenarios/scenarios.rs — G7 LIFO teardown, A8 fail-revert, reactive
    provider/consumer lifecycle, the A1 boundary, and a concurrent-divert
    race loop (no torn state). See the header of scenarios.rs for the
    documented A1 divergence on this runtime."""
    here = Path(__file__).resolve().parent
    ir = compile_files([str(here / "scenarios" / "probe.rvl")])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_scenarios"), encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "scenarios.rs").write_text(
        (here / "scenarios" / "scenarios.rs").read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
        ["cargo", "test", "--offline"], cwd=tmp_path, text=True,
        capture_output=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "5 passed" in result.stdout


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
