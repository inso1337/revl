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


def test_string_literals_use_one_code_point_escape_path():
    """`_string` collapsed to one uniform code-point escape path (item 73):
    every string leaf — including the requirement-name lists behind
    `Inject::new([...])` — escapes the same way. The old `json.dumps` branch
    emitted lone-surrogate `\\uXXXX` for non-ASCII (Rust-invalid); the single
    path emits `\\u{XXXX}` everywhere, ASCII byte-identical to before."""
    assert emit._string("db") == '"db"'
    assert emit._string("a\nb") == '"a\\nb"'
    assert emit._string("héllo") == '"h\\u{e9}llo"'
    assert emit._string('say "hi"') == '"say \\"hi\\""'
    # the structural case (Inject::new): each element through the same escape
    assert emit._string(["db", "kv"]) == '["db", "kv"]'
    assert emit._string(["kév"]) == '["k\\u{e9}v"]'
    with pytest.raises(emit.EmitError):
        emit._string(42)


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


def test_realm_lowering_is_a_deterministic_collision_free_registry():
    """The realm-label lowering must be a fixed compile-time registry, not a
    runtime hash. `DefaultHasher` is unstable across Rust releases and its
    64-bit output can collide with cordis-rs's monotonic scope counter; the
    registry tags each distinct label into the reserved top-bit region of the
    u64 scope space, which the counter (starts at 1, +1 per isolate) cannot
    reach, and gives every distinct label a distinct index."""
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    src = emit.emit(ir)

    # The unstable/collision-prone scheme is gone for good.
    assert "DefaultHasher" not in src
    assert ".hash(" not in src

    # Reserved high region, disjoint from the framework counter.
    assert "const REVL_REALM_TAG: u64 = 0x8000_0000_0000_0000;" in src
    # Each distinct label => a distinct index in a compile-time match.
    assert '"tenant_a" => 0,' in src
    assert '"tenant_b" => 1,' in src
    assert "cordis::Isolation::from_raw(REVL_REALM_TAG | index)" in src

    # Byte-for-byte stable build-to-build: the same source lowers identically.
    assert src == emit.emit(compile_files([str(ROOT / "examples" / "tenants.rvl")]))


def test_realm_registry_maps_equal_labels_together_and_distinct_apart():
    """Determinism at the semantic level: equal label strings collapse to one
    index (same realm), distinct labels get distinct indices (distinct
    realms), regardless of how many components mention each label."""
    ir = compile_source(
        """
        service Kv { fn get(k: Str) -> Opt[Str] }
        component AStore provides kv: Kv {
          isolate kv in realm("a")
          let s = effect Map.new() undo s.drop()
          provide kv { fn get(k) = s.get(k) }
        }
        component AApp requires kv: Kv {
          isolate kv in realm("a")
          effect kv.get("x") undo kv.get("x")
        }
        component BApp requires kv: Kv {
          isolate kv in realm("b")
          effect kv.get("x") undo kv.get("x")
        }
        """
    )
    src = emit.emit(ir)
    # Two components name realm("a") but there is exactly one registry entry
    # for it, so both lower to the identical Isolation (same realm).
    assert src.count('"a" => 0,') == 1
    assert '"b" => 1,' in src
    # Equal labels share one call value; the distinct label differs.
    assert src.count('_revl_realm("a")') == 2
    assert src.count('_revl_realm("b")') == 1


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


# cordis-rs, serde and serde_json are crates.io dependencies (see
# emit.cargo_toml), and two environments both have to work:
#
#   * CI — a fresh runner with an empty registry: the crates must be fetched,
#     so an unconditional `--offline` would fail with "no matching package
#     named `cordis-rs`" and the whole cargo half would go red on day one.
#   * dev / sandbox — no network but a warm ~/.cargo registry: the crates are
#     already local, so an offline resolve succeeds in seconds.
#
# Dropping `--offline` altogether made the second environment spend ~30s per
# test retrying HTTPS before failing (a full suite run took 21m50s and left
# nine red herrings), and the reflex fix — skip every cargo test when
# index.crates.io is unreachable — is quiet but verifies *nothing* offline.
#
# So: resolve offline first, and fall back to the networked resolve ONLY when
# the offline attempt failed for a *resolution* reason (crate absent from the
# local cache). A compile error or a failing `#[test]` is a real result and is
# returned untouched — the fallback must never launder a genuine failure.


def _crates_io_reachable() -> bool:
    """Whether the index can be reached. Cached: probed once per run."""
    global _CRATES_IO
    if _CRATES_IO is None:
        import socket

        try:
            socket.create_connection(("index.crates.io", 443), timeout=3).close()
            _CRATES_IO = True
        except OSError:
            _CRATES_IO = False
    return _CRATES_IO


_CRATES_IO: bool | None = None

# Phrases cargo uses when an *offline resolve* could not find a crate. They
# are emitted before any compilation starts, which is what makes them safe to
# distinguish from a real build failure.
_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode",
    "without the offline flag",
    "--offline was specified",
    "registry index was not found",
    "no matching package",
    "failed to select a version",
)

# Phrases that mean cargo got far enough to actually build or run something.
# If any appears, the result is real and must be surfaced as-is.
_REAL_FAILURE_MARKERS = (
    "error[e",
    "could not compile",
    "test result: failed",
    "panicked at",
)


def _is_offline_resolve_failure(proc: subprocess.CompletedProcess) -> bool:
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(m in blob for m in _REAL_FAILURE_MARKERS):
        return False
    return any(m in blob for m in _OFFLINE_RESOLVE_MARKERS)


def _cargo(subcommand: str, cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    """`cargo <subcommand>` — offline first, networked resolve as fallback."""
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra], cwd=cwd, text=True,
        capture_output=True, timeout=600,
    )
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline
    # The crates are not in the local registry. Only now is the network worth
    # the wait; without it there is nothing to resolve against, so skip with a
    # reason rather than burn ~30s per test on connection retries.
    if not _crates_io_reachable():
        pytest.skip(
            "cordis-rs is not in the local cargo registry and index.crates.io "
            "is unreachable — run once with network to populate ~/.cargo"
        )
    return subprocess.run(
        ["cargo", subcommand, *extra], cwd=cwd, text=True,
        capture_output=True, timeout=600,
    )


# Gate on the toolchain only. Crate availability is handled per-invocation by
# `_cargo` above, so a warm offline cache runs the tests for real.
needs_cargo = pytest.mark.skipif(
    shutil.which("cargo") is None, reason="cargo not installed"
)


def test_offline_fallback_never_launders_a_real_failure():
    """`_cargo` retries over the network only for *resolution* failures.

    This is the safety property of the offline-first path: if a compile
    error, a failing `#[test]` or a panic were ever classified as
    "retry with network", a genuine breakage would silently become a skip on
    an offline machine. Pinned here so the classifier cannot drift.
    """
    def out(text: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=text)

    # --- resolution failures: safe to retry -------------------------------
    assert _is_offline_resolve_failure(out(
        "error: no matching package named `cordis-rs` found\n"
        "location searched: registry `crates-io`\n"
        "As a reminder, you're using offline mode (--offline) which can "
        "sometimes cause surprising resolution failures."))
    assert _is_offline_resolve_failure(out(
        "error: failed to select a version for the requirement "
        "`cordis-rs = \"^0.3\"`\n"
        "you may wish to retry without the offline flag."))
    assert _is_offline_resolve_failure(out(
        "error: registry index was not found in any configuration"))

    # --- real results: must be surfaced untouched -------------------------
    assert not _is_offline_resolve_failure(out(
        "error[E0308]: mismatched types\nerror: could not compile `revl_check`"))
    assert not _is_offline_resolve_failure(out(
        "test result: FAILED. 0 passed; 1 failed; 0 ignored"))
    assert not _is_offline_resolve_failure(out(
        "thread 'main' panicked at src/lib.rs:12:5"))
    # a compile error that happens to mention offline mode is still an error
    assert not _is_offline_resolve_failure(out(
        "error[E0433]: failed to resolve\n"
        "note: you're using offline mode (--offline)"))


@needs_cargo
def test_cargo_check_surfaces_a_real_compile_error(tmp_path):
    """End-to-end companion to the classifier test: broken Rust must come
    back as a compile error from `_cargo_check`, never as a skip."""
    result = _cargo_check(tmp_path, 'pub fn broken() -> i32 { "not an int" }\n')
    assert result.returncode != 0
    assert "E0308" in result.stderr, result.stderr


def _cargo_check(tmp_path: Path, src: str) -> subprocess.CompletedProcess:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    return _cargo("check", tmp_path)


@needs_cargo
def test_cargo_check_compiles_against_cordis_rs(tmp_path):
    result = _cargo_check(tmp_path, emit.emit(_ir("user_cache")))
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_check_compiles_v2_realms(tmp_path):
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@needs_cargo
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


# ---------------------------------------------------------------------------
# Declared function types (roadmap item 91, docs/function-types.md §4).
#
# A function type written in a `fn`/`extern` parameter or return used to be
# refused outright on this tier ("a declared function type is not lowerable").
# It now lowers position-aware: a parameter or return is `impl Fn(..)` (rustc
# monomorphises it), which is exactly what a hand-written Rust signature does.
# The motivating shape is `agent_loop` — a top-level fn over effectful callback
# arrows — the string-protocol harness's "runs on every runtime" proof, which
# booted on python/ts but not rust before this. Locals were never affected;
# escaping positions (fields, ADT payloads, container elements) stay refused.

def test_emit_lowers_declared_function_type_params_to_impl_fn():
    ir = compile_source(
        "fn agent_loop(prompt: Str, complete: (Str) -> Str, "
        "call_tool: (Str) -> Str, max_steps: Int) -> Str {\n"
        "  let first: Str = complete(prompt)\n"
        "  return call_tool(first)\n"
        "}\n"
    )
    src = emit.emit(ir)
    assert "complete: impl Fn(String) -> String" in src
    assert "call_tool: impl Fn(String) -> String" in src
    assert "not lowerable" not in src


def test_emit_lowers_function_type_return_to_impl_fn():
    src = emit.emit(compile_source(
        "fn adder(n: Int) -> (Int) -> Int { return v => v + n }\n"))
    assert "fn adder(n: i64) -> impl Fn(i64) -> i64" in src


def test_emit_still_refuses_an_escaping_function_type_by_name():
    # A record field is an escaping position (`Box<dyn Fn(..)>`, constructed at
    # the arrow's creation site) that the emitter cannot yet lower — refused by
    # name, never erased to the opaque `Value` fallback.
    with pytest.raises(emit.EmitError, match="function type"):
        emit.emit(compile_source(
            "type Handler = { run: (Int) -> Str }\n"
            "fn f(h: Handler) -> Str { let r: (Int) -> Str = h.run  return r(1) }"))


@needs_cargo
def test_cargo_check_compiles_agent_loop_declared_function_types(tmp_path):
    """The definition-of-done pin: the `agent_loop` shape (a fn with declared
    function-type params, plus a fn returning a function) emits rust that a real
    `cargo check` accepts — the document no longer refuses on this tier."""
    ir = compile_source(
        "fn agent_loop(prompt: Str, complete: (Str) -> Str, "
        "call_tool: (Str) -> Str, max_steps: Int) -> Str {\n"
        "  let first: Str = complete(prompt)\n"
        "  let tool_out: Str = call_tool(first)\n"
        "  return tool_out\n"
        "}\n"
        "fn apply_twice(g: (Int) -> Int, x: Int) -> Int { return g(g(x)) }\n"
        "fn adder(n: Int) -> (Int) -> Int { return v => v + n }\n"
    )
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Roadmap item 94 — async function-value color erasure (follow-up to item 92).
#
# Item 92 added `Async[T]` (docs/design/async-function-values.md): a first-class
# callback whose declared return is `Async[T]` colors async/await on py/ts. The
# rust tier has no async-fn machinery, so it *erases* the color — but an
# un-erased `Async` head fell through `_rust_type`'s generic dispatch to the
# opaque `Value` fallback, so `(Str) -> Async[Str]` emitted the wrong
# `impl Fn(String) -> Value` instead of `impl Fn(String) -> String`. The fix
# maps the erased `Async[T]` return to its concrete `T`.

def test_async_typed_callback_return_erases_to_concrete_type():
    ir = compile_source(
        "fn agent_loop(prompt: Str, complete: (Str) -> Async[Str], "
        "max_steps: Int) -> Str {\n"
        "  let first: Str = complete(prompt)\n"
        "  return first\n"
        "}\n"
    )
    src = emit.emit(ir)
    assert "complete: impl Fn(String) -> String" in src
    # the async return must not leak the opaque `Value` fallback into the sig
    assert "impl Fn(String) -> Value" not in src
    assert "Async" not in src


@needs_cargo
def test_cargo_check_compiles_async_typed_callback_erasure(tmp_path):
    """Definition-of-done pin: an async-typed callback param emits rust a real
    `cargo check` accepts — the color is erased to the concrete return type,
    never the `Value` fallback that would not match the callee's `String`."""
    ir = compile_source(
        "fn agent_loop(prompt: Str, complete: (Str) -> Async[Str], "
        "max_steps: Int) -> Str {\n"
        "  let first: Str = complete(prompt)\n"
        "  return first\n"
        "}\n"
    )
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Roadmap item 93 — three rust-emitter compile bugs the string-protocol harness
# loop shape hit once item 91's fn-type lowering exposed the bodies around it
# (dogfood finding #22). Each is pinned by a minimal shape that a real
# `cargo check` accepts, mirroring the item-91 cargo gate above.
#
#   (a) config was not in scope in a provide-method body (E0425): the body read
#       a bare `config`, but the provider struct never captured it.
#   (b) a persistent `push` rebind (`current = current.push(..)`) failed because
#       `current` had already been moved by a by-value use earlier in the turn.
#   (c) a non-Copy ADT value and an `impl Fn` parameter used more than once were
#       consumed on first use (E0382).


def test_config_reference_in_provide_body_captures_config_field():
    """(a) A provide-method body that reads config compiles: the provider struct
    captures config and the body reads it through `self.config`, not a bare
    `config` that is out of method scope."""
    src = emit.emit(compile_source(
        "service S { fn get() -> Str }\n"
        "component C provides s: S {\n"
        "  config { name: Str }\n"
        "  provide s { fn get() = config.name }\n"
        "}\n"
    ))
    assert "config: CConfig," in src
    assert "self.config.name.clone()" in src
    # a bare `config.name` in the impl block (no leading `self.`) is the bug.
    assert "{ config.name.clone() }" not in src


def test_provide_struct_omits_config_when_methods_never_read_it():
    """The capture is demand-driven: a provision whose methods never read config
    gains no config field (avoiding a dead field and a `config.clone()` that
    would race an effect closure that partially moved config — the Worker
    scenario)."""
    src = emit.emit(compile_source(
        "service S { fn get() -> Int }\n"
        "component C provides s: S {\n"
        "  config { name: Str }\n"
        "  provide s { fn get() = 0 }\n"
        "}\n"
    ))
    assert "struct CS {\n}" in src
    assert "__revl_provide_config" not in src


@needs_cargo
def test_cargo_check_config_in_provide_body(tmp_path):
    """(a) real cargo gate: config-in-provide compiles."""
    src = emit.emit(compile_source(
        "service S { fn get() -> Str }\n"
        "component C provides s: S {\n"
        "  config { name: Str = \"x\" }\n"
        "  provide s { fn get() = config.name }\n"
        "}\n"
    ))
    result = _cargo_check(tmp_path, src)
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_check_non_copy_value_and_impl_fn_reused(tmp_path):
    """(c) real cargo gate: a non-Copy ADT used by two calls and an `impl Fn`
    parameter passed by value inside a loop both compile — the reused value is
    cloned, the function value is passed by reference (`&f: Fn`)."""
    src = emit.emit(compile_source(
        "type Reply = Tool(Str) | Final(Str)\n"
        "fn is_tool(d: Reply) -> Bool { return match d { Tool(x) => true, Final(x) => false } }\n"
        "fn body(d: Reply) -> Str { return match d { Tool(x) => x, Final(x) => x } }\n"
        "fn handle(dec: Reply) -> Str {\n"
        "  if (is_tool(dec)) { return body(dec) }\n"
        "  return body(dec)\n"
        "}\n"
        "fn apply(f: (Str) -> Str, x: Str) -> Str { return f(x) }\n"
        "fn drive(call: (Str) -> Str, items: List[Str]) -> Str {\n"
        "  var acc = \"seed\"\n"
        "  for (it of items) { acc = apply(call, it) }\n"
        "  return acc\n"
        "}\n"
    ))
    assert "is_tool(dec.clone())" in src
    assert "apply(&call, it)" in src
    result = _cargo_check(tmp_path, src)
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_check_harness_loop_shape_config_push_and_callbacks(tmp_path):
    """(a)+(b)+(c) together: the string-protocol harness loop shape — a bounded
    loop that funnels a message list through an `impl Fn` `complete` callback,
    rebinds the accumulator with a persistent `push`, decodes into a non-Copy
    ADT used twice, and dispatches through a second `impl Fn` `call_tool` — all
    driven from a provide-method that reads config. This is the shape that
    `revl run --backend rust --once` failed to build (finding #22)."""
    src = emit.emit(compile_source(
        "type Reply = Tool(Str) | Final(Str)\n"
        "fn is_tool(dec: Reply) -> Bool { return match dec { Tool(x) => true, Final(x) => false } }\n"
        "fn exec_reply(dec: Reply, call_tool: (Str) -> Str) -> Str {\n"
        "  return match dec { Tool(name) => call_tool(name), Final(answer) => answer }\n"
        "}\n"
        "fn decode(resp: Str) -> Reply {\n"
        "  if (resp.slice(0, 6) == \"FINAL \") { return Final(resp.slice(6, resp.length())) }\n"
        "  return Tool(resp.slice(0, 10))\n"
        "}\n"
        "fn agent_loop(msgs: List[Str], ticks: List[Int], complete: (List[Str]) -> Str,\n"
        "              call_tool: (Str) -> Str) -> Str {\n"
        "  var current = msgs\n"
        "  var answer = \"none\"\n"
        "  for (i of ticks) {\n"
        "    let dec = decode(complete(current))\n"
        "    if (is_tool(dec)) {\n"
        "      let out = exec_reply(dec, call_tool)\n"
        "      current = current.push(out)\n"
        "    } else {\n"
        "      answer = exec_reply(dec, call_tool)\n"
        "    }\n"
        "  }\n"
        "  return answer\n"
        "}\n"
        "service Model { emission fn complete(h: List[Str]) -> Str }\n"
        "service Tools { emission fn call(name: Str) -> Str }\n"
        "service Agentic { emission fn complete(h: List[Str]) -> Str }\n"
        "component Agent requires model: Model, tools: Tools provides agent: Agentic {\n"
        "  config { max_steps: Int = 8, banner: Str = \"hi\" }\n"
        "  provide agent {\n"
        "    fn complete(h) {\n"
        "      let seed = [config.banner]\n"
        "      return agent_loop(seed, [1, 2, 3],\n"
        "                        msgs2 => emit model.complete(msgs2),\n"
        "                        name => emit tools.call(name))\n"
        "    }\n"
        "  }\n"
        "}\n"
    ))
    # (b): the persistent push rebind survives — current was cloned for the
    # by-value `complete(..)` consume, so the rebind still owns it.
    assert "complete(current.clone())" in src
    assert "current = current.revl_push(out)" in src
    result = _cargo_check(tmp_path, src)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# host `Map.new()` iteration surface — `keys()` / `size()` (roadmap item 86).
#
# The value-Map builtins `size()`/`keys()` (docs/stdlib-2.0.md §Map) type-check
# on a host `Map.new()` receiver too (host-receiver provenance isn't tracked in
# provide bodies), and emit lowers both as plain method calls on the runtime
# object. The `struct Map<V>` therefore has to carry those methods, or the
# emitted component fails to compile (`no method named size`). This mirrors the
# python fix (backends/python/tests/test_host_map_iter.py).

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
    """The `struct Map<V>` runtime carries `size`/`keys`, and the provide body
    lowers them as method calls on the store — not `.len()` / `.iter()` over a
    bare map (which would need `store` to be a HashMap, not a host object)."""
    src = emit.emit(compile_source(_HOST_MAP_ITER_SRC, "memkv.rvl"))
    # runtime methods exist, with value-Map semantics (count + sorted keys)
    assert "pub fn size(&self) -> i64 {" in src
    assert "pub fn keys(&self) -> Vec<String> {" in src
    assert "ks.sort();" in src  # canonical (code-point) order via String: Ord
    # provide body lowers to method calls on the host object
    assert "self.store.size()" in src
    assert "self.store.keys()" in src


@needs_cargo
def test_cargo_check_compiles_host_map_iteration(tmp_path):
    """The reproduction gate: before `size`/`keys` were added to the runtime,
    this emitted component failed to compile (`no method named size`)."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(_HOST_MAP_ITER_SRC, "memkv.rvl")))
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


@needs_cargo
def test_cargo_check_compiles_stdlib_builtins_on_str_and_list(tmp_path):
    """Review finding: slice/indexOf/concat previously failed to compile for
    one of {Str, List} each; indexing failed for both (i64 vs usize)."""
    ir = compile_source(STDLIB_SRC + "\npub fn first(xs: List[Int], i: Int) -> Int { return xs[i] }\n")
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_test_runs_emitted_stdlib_semantics(tmp_path):
    """Not just compiles: the emitted #[test] executes the spec's semantics
    (persistent push, -1 when absent, char-based string positions)."""
    ir = compile_source(STDLIB_SRC)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout


@needs_cargo
def test_cargo_check_compiles_method_level_compensate(tmp_path):
    """Review finding: `emit ... compensate` in a provide method referenced
    `*_undo` clones that were only generated when compensate was absent.

    `N.ping` must itself be declared `emission`: it reaches `db.ex`, and a
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
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@needs_cargo
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


@needs_cargo
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
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "10 passed" in result.stdout


# ---------------------------------------------------------------------------
# Timers as revertible schedules (item 57, docs/time-coeffect.md). `every` /
# `after` lower to a schedule/cancel effect on the clock coeffect: arming is the
# acquire, cancellation the derived inverse, wired into the same `ctx.effect`
# ledger — so unloading the component cancels the timer residue-free. The clock
# advances only on `revl_clock_advance`, so firings are deterministic.

_TIMER_SCENARIO = ROOT / "backends" / "rust" / "scenarios" / "timer.rvl"


def test_timer_lowers_to_schedule_cancel_effect():
    """A `timer` step lowers to a revertible schedule: armed through the schedule
    helper, cancelled through the same `ctx.effect` disposer stack any other
    effect uses, and the clock coeffect preamble is pulled in only when a timer
    is present."""
    ir = compile_files([str(_TIMER_SCENARIO)])
    src = emit.emit(ir)
    # periodic + one-shot both lower through the schedule helpers
    assert "revl_schedule_every(30000, move || {" in src
    assert "revl_schedule_after(300000, move || {" in src
    # the derived inverse is cancellation, yielded into the effect ledger
    assert "ctx.effect(\"Heartbeat.timer.undo\", move || { revl_cancel(" in src
    # the firing body carries the emission, audited like a top-level emit
    assert 'write(String::from("tick"))' in src
    # the deterministic-advance driver and the clock preamble are present
    assert "pub fn revl_clock_advance(ms: i64) -> usize" in src
    # arming takes a live-resource slot, so a leaked timer surfaces as residue
    assert "REVL_LIVE_HOST_RESOURCES.with(|c| c.set(c.get() + 1));" in src


def test_no_timer_no_clock_preamble():
    """A component with no timer must not carry the clock preamble (the frozen
    scenarios stay byte-stable)."""
    src = emit.emit(_ir("user_cache"))
    assert "revl_clock_advance" not in src
    assert "RevlTimer" not in src


@needs_cargo
def test_timer_runtime_on_real_cordis_rs(tmp_path):
    """The item-57 exit criterion on the rust tier: the emitted Heartbeat driven
    by the REAL cordis-rs runtime. Fixture in scenarios/timer.rvl, assertions in
    scenarios/timer.rs — deterministic firing under `revl_clock_advance` and
    unload-cancels-no-residue (the periodic drops out of `revl_clock_pending`
    on teardown, and a further advance fires nothing)."""
    here = Path(__file__).resolve().parent
    ir = compile_files([str(here / "scenarios" / "timer.rvl")])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_timer_scn"), encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "timer.rs").write_text(
        (here / "scenarios" / "timer.rs").read_text(encoding="utf-8"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout


@needs_cargo
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


@needs_cargo
def test_cargo_check_compiles_method_body_bindings(tmp_path):
    """A provide-method that names intermediates — the shape every other tier
    accepted while the Rust backend refused it."""
    ir = compile_source(
        """
        service Bus { emission fn send(m: Str) -> Int }
        service Greet { emission fn hello(name: Str) -> Str }
        service Plain { fn shout(name: Str) -> Str }

        component Speaker requires bus: Bus provides greet: Greet, plain: Plain {
          provide greet {
            fn hello(name) {
              emit bus.send(name)
              let prefix = "hi, "
              let message = prefix + name
              return message
            }
          }
          provide plain {
            fn shout(name) {
              var loud = name
              loud = loud + "!"
              return loud
            }
          }
        }
        """
    )
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# Conformance gaps closed on this tier (docs/conformance.md). Each construct
# below used to raise instead of lowering; the matrix now reports rust as
# clean apart from the deliberate extern refusals.
# --------------------------------------------------------------------------


def test_nullish_lowers_to_unwrap_or_else_in_fn_bodies():
    """`a ?? b` on `Opt[T]` = `Option<T>`. `unwrap_or_else` (not `unwrap_or`)
    because `??` must not evaluate its right operand when the left is present."""
    ir = compile_source(
        """
        fn side(n: Int) -> Int { return n * 3 }
        fn pick(a: Opt[Int]) -> Int { return a ?? side(7) }
        """
    )
    src = emit.emit(ir)
    assert "a.unwrap_or_else(|| side(7i64))" in src
    assert "unwrap_or(" not in src  # eager form would evaluate `side(7)` always


def test_nullish_lowers_in_component_method_bodies():
    """The component renderer speaks the v1 dialect (`req`, v1 `call`), which
    the v3 renderer cannot see — `??` has to be handled in both."""
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
    assert "bus.maybe(x).unwrap_or_else(|| 0i64)" in src


def test_bare_return_lowers_for_void_service_operations():
    """`{"step": "return", "expr": null}` — the expression-body fast path has
    no expression to inline and must fall through to the statement path."""
    ir = compile_source(
        """
        service S { fn f(x: Int) }
        component C provides s: S { provide s { fn f(x) { return } } }
        """
    )
    src = emit.emit(ir)
    assert "fn f(&self, x: i64) -> () { return; }" in src


def test_match_reaches_the_v3_renderer_from_a_component_body():
    """`match` in a provide-method body (legal since ff0d76e) fell into the gap
    between the two expression renderers."""
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
    assert "Outcome::Found(v) => v," in src
    assert "Outcome::Missing => 0i64," in src


def test_config_access_inside_a_fail_guard():
    """`config.n` reached the v3 renderer through the guard's `bin` node."""
    ir = compile_source(
        """
        service S { fn f(x: Int) -> Int }
        component C provides s: S {
          config { n: Int = 1 }
          if (config.n < 1) { fail "bad" }
          provide s { fn f(x) = x }
        }
        """
    )
    src = emit.emit(ir)
    # the emitted guard reads the local `let config = CConfig { .. }`
    assert "if (config.n.clone() < 1i64) {" in src


def test_optional_chaining_reports_the_tier_limit_not_a_generic_message():
    """`optfield`/`optcall` delegate to the v3 renderer purely so its specific
    refusal is what surfaces from a component body too."""
    ir = {"ir_version": 1, "services": {"S": {"methods": {"f": {"params": [], "returns": "Int"}}}},
          "components": [{"name": "C", "requires": {}, "provides": {"s": "S"}, "body": [
              {"step": "provide", "name": "s", "service": "S", "methods": [
                  {"name": "f", "params": [], "body": [
                      {"step": "return", "expr": {
                          "kind": "optfield", "name": "a",
                          "target": {"kind": "var", "name": "x"}}}]}]}]}]}
    with pytest.raises(emit.EmitError, match="optional chaining"):
        emit.emit(ir)


def test_provider_struct_captures_required_services():
    """A provide-method calling a required service needs that service as a
    struct field. The effectful path always captured `requires`; the pure
    path did not, so a component with no effects emitted Rust referencing a
    free variable. The conformance matrix could not see it — it only checks
    that the emitter does not raise, never that the output compiles."""
    ir = compile_source(
        "service Bus { fn ping(n: Int) -> Int }\n"
        "service S { fn g(n: Int) -> Int }\n"
        "component C requires bus: Bus provides s: S {\n"
        "  provide s { fn g(n) = bus.ping(n) }\n"
        "}"
    )
    out = emit.emit(ir)
    assert "struct CS {\n    bus: Arc<Box<dyn Bus>>,\n}" in out
    assert "fn g(&self, n: i64) -> i64 { self.bus.ping(n) }" in out
    assert "Box::new(CS { bus: bus.clone() })" in out
    # no free reference to the binding survives in the impl
    impl = out.split("impl S for CS {")[1].split("}")[0]
    assert "bus." not in impl.replace("self.bus.", "")


# ---- Map value type (docs/stdlib-2.0.md §Map) ------------------------------

MAP_SRC = """
pub fn newTable() -> Map[Str, Int] { return Map.empty() }
pub fn put(m: Map[Str, Int], k: Str, v: Int) -> Map[Str, Int] { return m.set(k, v) }
pub fn build(pairs: List[Str]) -> Map[Str, Int] {
  var m = Map.empty()
  var i = 0
  while (i < pairs.length()) {
    m = m.set(pairs[i], pairs[i].length())
    i += 1
  }
  return m
}
test "map value semantics" {
  assert newTable().has("a") == false
  assert (newTable().lookup("a") ?? 0 - 1) == 0 - 1
  assert put(newTable(), "a", 1).has("a") == true
  assert (put(newTable(), "a", 1).lookup("a") ?? 0 - 1) == 1
  // order-independent structural equality: same mapping, two insert orders
  assert build(["a", "bb", "ccc"]) == build(["ccc", "bb", "a"])
  // persistent fold over one binding: extending never disturbs existing
  // pairs, and repeated READS of the same snapshot agree (reads borrow,
  // they do not consume)
  var t = Map.empty()
  t = t.set("a", 1)
  t = t.set("b", 2)
  assert (t.lookup("a") ?? 0 - 1) == 1
  assert (t.lookup("a") ?? 0 - 1) == 1
  assert (t.lookup("b") ?? 0 - 1) == 2
  assert (t.lookup("c") ?? 0 - 1) == 0 - 1
  assert t.has("a") == true
  assert t.has("c") == false
}
"""


@needs_cargo
def test_cargo_check_compiles_the_map_value_type(tmp_path):
    """docs/stdlib-2.0.md §Map: Map.empty/set/lookup/has compile on the rust
    tier (std HashMap, cloned on write; lookup answers Option<V>)."""
    ir = compile_source(MAP_SRC)
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_test_runs_the_map_value_semantics(tmp_path):
    """Not just compiles: persistent set (receiver never mutates), lookup
    absence, and ORDER-INDEPENDENT structural equality (the last assert
    builds the same mapping in two different insertion orders)."""
    ir = compile_source(MAP_SRC)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout


# --------------------------------------------------------------------------
# FR-4 (docs/v2.0-roadmap.md item 77(c)) — non-String values in the HOST Map.
# The session ledger (`Map[Str, List[Msg]]`) used to emit a hardcoded
# `HashMap<String, String>` and fail E0308/E0599; the host Map is now generic
# over its value type, learned per site from the IR's `insert` calls.
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
    """The session-ledger shape: the emitted provider struct and constructor
    pin `Map<Vec<Msg>>` (FR-4), the host Map struct is generic, and the key is
    borrowed so a read-then-write on one key compiles without a clone."""
    src = emit.emit(compile_source(LEDGER_SRC))
    assert "pub struct Map<V>" in src
    assert "impl<V> Map<V>" in src
    assert "impl<V: Clone> Map<V>" in src
    assert "pub fn get(&self, key: &String) -> Option<V>" in src
    assert "pub fn remove(&self, key: &String)" in src
    assert "store: Arc<Map<Vec<Msg>>>" in src
    assert "let store = Arc::new(Map::<Vec<Msg>>::new());" in src
    assert "self.store.get(&id).unwrap_or_else(|| vec![])" in src
    assert "store_undo.insert(id_undo, prev);" in src
    # the historical hardcoding is gone
    assert "HashMap<String, String>" not in src


@needs_cargo
def test_cargo_check_compiles_ledger_shaped_map_component(tmp_path):
    """The FR-4 exit criterion: the harness's core state shape (session ledger
    `Map[Str, List[Msg]]`, Msg a record) COMPILES on the rust tier."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(LEDGER_SRC)))
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_check_compiles_int_bool_and_list_map_values(tmp_path):
    """Map[Str, Int], Map[Str, Bool] and Map[Str, List[Str]] host maps."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(HOST_MAP_TYPES_SRC)))
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_check_compiles_record_map_value(tmp_path):
    """A record value type (`Map[Str, Profile]`) — the ledger's Msg minus the
    List wrapper."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(RECORD_VALUE_SRC)))
    assert result.returncode == 0, result.stderr


def test_int_and_list_map_values_reach_the_emitted_types():
    src = emit.emit(compile_source(HOST_MAP_TYPES_SRC))
    assert "store: Arc<Map<i64>>" in src
    assert "store: Arc<Map<Vec<String>>>" in src
    assert "store: Arc<Map<bool>>" in src
    assert "Map::<i64>::new()" in src
    assert "Map::<Vec<String>>::new()" in src
    assert "Map::<bool>::new()" in src
    src = emit.emit(compile_source(RECORD_VALUE_SRC))
    assert "store: Arc<Map<Profile>>" in src
    assert "Map::<Profile>::new()" in src


@needs_cargo
def test_cargo_test_runs_non_string_map_values_on_the_real_runtime(tmp_path):
    """Runtime proof, not just a compile: a Map[Str, Int] and a Map[Str,
    List[Str]] host map are driven through the real cordis-rs runtime —
    insert/get round-trip non-String values, absent keys fall back, and
    teardown leaves nothing behind (the no-residue shape)."""
    ir = compile_source(HOST_MAP_TYPES_SRC)
    src = emit.emit(ir) + """

#[test]
fn host_map_roundtrips_int_and_list_values() {
    let root = cordis::Context::new();
    let fiber = root.plugin(counters(), ());
    fiber.wait().unwrap();
    let counters = root.require::<Box<dyn Counters>>("counters").unwrap();
    counters.put("a".to_string(), 7i64);
    assert_eq!(counters.get("a".to_string()), 7i64);
    assert_eq!(counters.get("missing".to_string()), 0i64);
    fiber.dispose().unwrap();

    let root2 = cordis::Context::new();
    let fiber2 = root2.plugin(tags(), ());
    fiber2.wait().unwrap();
    let tags = root2.require::<Box<dyn Tags>>("tags").unwrap();
    tags.put("t".to_string(), vec!["x".to_string(), "y".to_string()]);
    assert_eq!(tags.get("t".to_string()), vec!["x".to_string(), "y".to_string()]);
    assert_eq!(tags.get("none".to_string()), Vec::<String>::new());
    fiber2.dispose().unwrap();
}
"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout
