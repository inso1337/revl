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
# A red golden is a REVIEW prompt, not a wall: the goldens are snapshot tests
# (docs/conformance.md, "Golden policy: snapshot, not freeze"), so regenerating
# and reviewing the diff is always an acceptable resolution. Every golden
# assertion says which command regenerates it.
_TAIL = ("If the change is intended: python3 tools/regen_goldens.py {t}, then review "
         "the diff. Goldens are snapshots, not a freeze (docs/conformance.md).")


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
    assert src == golden, (
        "backends/rust/golden/user_cache.rs drifted from the emitter. "
        + _TAIL.format(t="rust"))


_CRASHPROOF = Path(__file__).resolve().parent / "scenarios" / "crashproof"


def test_record_mode_off_is_byte_identical_and_on_adds_the_wal_sink():
    """item 322 Slice 2: the `record` emit flag is gated. Off (the default) is
    byte-identical to before this slice — no WAL sink, no per-descriptor call —
    and the committed crashproof `lib.rs` is exactly the record-ON emission
    (`emit.py --record`), so the crash-recovery proof's producer never drifts
    from the emitter."""
    ir = json.loads((_CRASHPROOF / "crashproof.ir.json").read_text(encoding="utf-8"))

    off = emit.emit(ir)
    assert off == emit.emit(ir, record=False)  # default is record-off
    assert "revl_record_transactional" not in off
    assert "REVL_WAL" not in off
    assert "discharge-descriptor" not in off

    on = emit.emit(ir, record=True)
    assert on != off
    assert 'revl_record_transactional("beginRow", "deleteRow", vec![format!("{}", result)]);' in on
    assert "self.file.sync_all()" in on            # fsync per record
    assert '"record": "discharge-descriptor"' in on
    assert 'std::env::var("REVL_WAL")' in on

    golden = (_CRASHPROOF / "src" / "lib.rs").read_text(encoding="utf-8")
    assert on == golden


# ---------------------------------------------------------------------------
# stdlib JSON wire protocol crosses to rust (roadmap item 140).
#
# The `Any` return of json_parse/json_stringify erases to `cordis::Value`; the
# @rs bodies box a parsed `serde_json::Value` into that erased value and recover
# it to re-encode, so a structured document survives stringify∘parse. Before
# item 140 the module shipped no @rs body and the emitter refused it.

_JSONWIRE_RVL = Path(__file__).parent / "scenarios" / "jsonwire.rvl"
_ROUTER_RVL = Path(__file__).parent / "scenarios" / "router.rvl"

# item 167: a `#[test]` appended to the emitted router crate that boots the
# three worker realms + Router, then drives the routed `worker` provider through
# a probe plugin (the runtime has no root-level require). It proves the EMITTED
# Router body distributes round-robin and fails over when a worker withdraws.
_ROUTER_TEST_MODULE = """
#[cfg(test)]
mod _revl_router_scenario {
    use super::*;
    use std::sync::{Arc, Mutex};

    fn probe(sink: Arc<Mutex<Vec<String>>>, n: usize) -> cordis::PluginHandle {
        cordis::plugin_sync::<(), _>("Probe", cordis::Inject::new(["worker"]), move |ctx, _cfg| {
            let svc = ctx.require::<Box<dyn Worker>>("worker")?;
            let mut out = sink.lock().unwrap();
            for i in 0..n { out.push(svc.call(format!("{}", i))); }
            Ok(cordis::PluginOutput::none())
        })
    }

    fn load(root: &cordis::Context, name: &str) -> cordis::Fiber {
        let f = _revl_load(root, name, &serde_json::Value::Null).unwrap();
        f.wait().unwrap();
        f
    }

    #[test]
    fn round_robin_then_failover() {
        let root = cordis::Context::new();
        let _w1 = load(&root, "w1");
        let w2 = load(&root, "w2");
        let _w3 = load(&root, "w3");
        let _router = load(&root, "router");

        // the emitted Router body fans out round-robin across w1,w2,w3
        let sink = Arc::new(Mutex::new(Vec::new()));
        let p = root.plugin(probe(sink.clone(), 6), ());
        p.wait().unwrap();
        assert_eq!(*sink.lock().unwrap(),
            vec!["w1:0","w2:1","w3:2","w1:3","w2:4","w3:5"]);
        p.dispose().ok();

        // withdraw w2 -> its realm resolves to a non-ACTIVE handle and drops
        // out; the next calls go to the survivors (reactive failover)
        w2.dispose().ok();
        let sink2 = Arc::new(Mutex::new(Vec::new()));
        let p2 = root.plugin(probe(sink2.clone(), 6), ());
        p2.wait().unwrap();
        let got = sink2.lock().unwrap().clone();
        assert!(got.iter().all(|r| r.starts_with("w1:") || r.starts_with("w3:")), "{:?}", got);
        assert!(!got.iter().any(|r| r.starts_with("w2:")), "{:?}", got);
        p2.dispose().ok();
    }
}
"""


def test_jsonwire_scenario_emits_serde_backed_bodies():
    src = emit.emit(compile_files([str(_JSONWIRE_RVL)]))
    assert "fn json_parse(s: String) -> Value" in src
    assert "serde_json::from_str::<serde_json::Value>(&s)" in src
    assert "fn json_stringify(v: Value) -> String" in src
    assert "v.downcast::<serde_json::Value>()" in src


def test_jsonwire_golden_byte_equality():
    src = emit.emit(compile_files([str(_JSONWIRE_RVL)]))
    golden = (Path(__file__).parent / "golden" / "jsonwire.rs").read_text(encoding="utf-8")
    assert src == golden, (
        "backends/rust/golden/jsonwire.rs drifted from the emitter. "
        + _TAIL.format(t="rust"))


def test_string_literals_emit_printable_non_ascii_literally():
    """`_string` escapes control chars / quotes / backslashes, but emits a
    printable non-ASCII scalar *literally* as UTF-8 — Rust source is UTF-8, so
    `é` needs no `\\u{...}` (item 135, finding #35). The literal form carries no
    brace, so it is safe in a format-macro position where a `\\u{XXXX}` escape
    would be re-read as `{XXXX}`. ASCII output is byte-identical to before, and
    `\\u{...}` is reserved for the lone-surrogate / unprintable case."""
    assert emit._string("db") == '"db"'
    assert emit._string("a\nb") == '"a\\nb"'
    # printable non-ASCII -> literal UTF-8, no escape
    assert emit._string("héllo") == '"héllo"'
    assert emit._string("em—dash") == '"em—dash"'
    assert emit._string('say "hi"') == '"say \\"hi\\""'
    # the structural case (Inject::new): each element through the same escape
    assert emit._string(["db", "kv"]) == '["db", "kv"]'
    assert emit._string(["kév"]) == '["kév"]'
    # lone surrogate / unprintable still escapes as \u{...} (not valid UTF-8
    # source literally): the escape path is reserved for exactly this case.
    assert emit._string("\udce9") == '"\\u{dce9}"'
    assert emit._string("\x7f") == '"\\u{7f}"'
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


def _cargo_check(tmp_path: Path, src: str, *extra: str) -> subprocess.CompletedProcess:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    return _cargo("check", tmp_path, *extra)


def _cargo_test(tmp_path: Path, src: str, harness: str) -> subprocess.CompletedProcess:
    """Emit `src`, append a hand-written `#[test]` module, and `cargo test` it.

    Unlike `_cargo_check`, this executes the emitted code, so it proves runtime
    behaviour, not just that the crate type-checks."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src + "\n" + harness, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    return _cargo("test", tmp_path)


@needs_cargo
def test_cargo_check_compiles_against_cordis_rs(tmp_path):
    result = _cargo_check(tmp_path, emit.emit(_ir("user_cache")))
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_cargo_check_compiles_v2_realms(tmp_path):
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    result = _cargo_check(tmp_path, emit.emit(ir))
    assert result.returncode == 0, result.stderr


# item 270 — a non-Copy `String` whose surface type the emitter cannot infer
# (`source.charAt(i)` is a builtin method, not a declared-return call) and that
# is consumed by value more than once. Each move (a call argument, both fields
# of a record literal, and an `if`-expression branch tail) must clone, or the
# second read borrows a moved value (E0382). This is the shape the self-host
# lexer hits: before the fix its emitted crate failed with 8x E0382. A Copy
# scalar (`Int`) reused just as often must NOT clone — no needless `.clone()`.
_REUSED_VALUE_RVL = """
pub type Tok = { kind: Str, text: Str }

fn tag(c: Str) -> Str { return c }

pub fn classify(source: Str, i: Int) -> Tok {
  let c = source.charAt(i)
  let a = tag(c)
  let b = tag(c)
  let kind = if (c == "@") { "at" } else { c }
  let doubled = i + i
  return { kind: kind, text: c }
}
"""


def test_reused_uninferred_string_clones_each_move_but_not_copies():
    src = emit.emit(compile_source(_REUSED_VALUE_RVL))
    body = src.split("pub fn classify")[1].split("\n}")[0]
    # every by-value use of the un-inferred `c` is cloned: the two `tag` calls,
    # the `if`-expression else-branch tail, and the record field.
    assert body.count("tag(c.clone())") == 2, body
    assert 'else { c.clone() }' in body, body
    assert "text: c.clone()" in body, body
    # the Copy `Int` param reused across two moves is never cloned.
    assert "i.clone()" not in body, body


@needs_cargo
def test_cargo_check_reused_uninferred_string_compiles(tmp_path):
    """The regression's payoff: the emitted crate now builds. Before item 270
    the same source produced borrow-of-moved-value (E0382)."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(_REUSED_VALUE_RVL)))
    assert result.returncode == 0, result.stderr


# item 282 — a read-only `Str` parameter (only ever a builtin receiver, a
# builtin `&str` argument, an equality/`+` operand, or threaded straight to
# another such parameter) lowers to a borrowed `&str`, so the call lends the
# string instead of cloning the whole thing. A param that ESCAPES into an owned
# position (returned by value, a record field, an owned call argument) stays a
# `String` and still clones on reuse, and a `pub` entry stays owned so its ABI
# is the external `Str` contract. The whole shape is gated on the module using
# the stdlib (`peek`'s `charCodeAt`), the surface the self-host port excludes.
_BORROW_RVL = """
pub type Pair = { x: Str, y: Str }

fn peek(s: Str, i: Int) -> Int { return s.charCodeAt(i) }
fn thread(s: Str, i: Int) -> Int { return peek(s, i) }
fn pair_up(s: Str) -> Pair { return { x: s, y: s } }

pub fn run(src: Str) -> Int { return thread(src, 0) }
"""

_BORROW_TEST_MODULE = """
#[cfg(test)]
mod revl_item282_tests {
    use super::*;
    #[test]
    fn borrowed_read_only_param_threads_without_a_clone() {
        // `run` owns the String once and lends it down the &str chain.
        assert_eq!(run("abc".to_string()), 97);
        // the escaping param is still owned and cloned into both fields.
        let p = pair_up("z".to_string());
        assert_eq!(p.x, "z");
        assert_eq!(p.y, "z");
    }
}
"""


def test_read_only_string_param_lowers_to_borrow_but_owned_still_clones():
    src = emit.emit(compile_source(_BORROW_RVL))
    # read-only params (a builtin receiver, and a pass-through to one) borrow.
    # `peek` indexes its param positionally, so item 277 also threads the
    # `&[char]` view here; the borrow of `s` itself is what this test pins.
    assert "fn peek(s: &str, i: i64, s_revl_cs: &[char]) -> i64" in src, src
    assert "fn thread(s: &str, i: i64, s_revl_cs: &[char]) -> i64" in src, src
    # the borrowed param threads straight through — no clone, no re-borrow.
    thread_body = src.split("fn thread(")[1].split("\n}")[0]
    assert "peek(s, i, s_revl_cs)" in thread_body, thread_body
    assert "peek(s.clone()" not in thread_body, thread_body
    assert "peek(&s" not in thread_body, thread_body
    # a param that escapes into owned record fields stays a String and clones.
    assert "fn pair_up(s: String) -> Pair" in src, src
    pair_body = src.split("fn pair_up(")[1].split("\n}")[0]
    assert "x: s.clone()" in pair_body, pair_body
    assert "y: s.clone()" in pair_body, pair_body
    # the `pub` entry keeps the owned `Str` ABI and lends a borrow at the call.
    assert "pub fn run(src: String) -> i64" in src, src
    assert ("thread(&src, 0i64, &src_revl_cs)"
            in src.split("pub fn run(")[1]), src


@needs_cargo
def test_cargo_test_borrowed_param_builds_and_runs(tmp_path):
    """item 282 definition-of-done: the borrow lowering cargo-builds AND runs
    with byte-identical behaviour — `&str` threaded through the helpers, the
    escaping param still owned."""
    src = emit.emit(compile_source(_BORROW_RVL))
    result = _cargo_test(tmp_path, src, _BORROW_TEST_MODULE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test result: ok" in result.stdout, result.stdout


# item 277 — positional string indexing (`charAt`/`charCodeAt`/`codepoint_at`)
# lowers to `chars().nth(i)`, an O(i) front walk, so a scan is O(n^2). The
# INTERPROCEDURAL fix threads a `&[char]` view: a non-`pub` helper that indexes a
# `Str` param (or hands it to one that does) grows a synthetic
# `<name>_revl_cs: &[char]` parameter, and the `pub` entry that owns the buffer
# collects ONCE into a local and lends it down. A slot whose argument at some
# call site is not such a threaded parameter would need a fresh collect per call
# — the shape that regressed when it was done per function — so that slot is
# retracted and keeps the front walk.
_CHARVIEW_RVL = """
fn peek(s: Str, i: Int) -> Int { return s.charCodeAt(i) }

fn scan(s: Str, i: Int) -> Int {
  var j = i
  var acc = 0
  while (j < 3) {
    acc = acc + peek(s, j)
    j = j + 1
  }
  return acc
}

fn head(s: Str) -> Int { return s.charCodeAt(0) }

fn via_slice(s: Str) -> Int { return head(s.slice(1, 4)) }

pub fn run(src: Str) -> Int { return scan(src, 0) + via_slice(src) }
"""

_CHARVIEW_TEST_MODULE = """
#[cfg(test)]
mod revl_item277_tests {
    use super::*;
    #[test]
    fn char_view_indexes_the_same_code_points_as_the_front_walk() {
        // scan sums the first three code points of "abcdef" (97+98+99 = 294)
        // through the threaded view; via_slice slices "bcd" through the same
        // view and head reads "bcd"[0] = 98 through its retained `chars().nth`
        // walk. 294 + 98 = 392.
        assert_eq!(run("abcdef".to_string()), 392);
        // multi-byte input: the view indexes Unicode SCALARS, exactly like
        // `chars().nth`, so a non-ASCII buffer agrees too.
        // "\\u{e9}..\\u{ee}" = 233+234+235 = 702, then "\\u{ea}\\u{eb}\\u{ec}"[0] = 234.
        assert_eq!(run("\\u{e9}\\u{ea}\\u{eb}\\u{ec}\\u{ed}\\u{ee}".to_string()), 936);
    }
}
"""


def test_positional_index_threads_a_char_view_and_retracts_where_it_cannot():
    src = emit.emit(compile_source(_CHARVIEW_RVL))
    # the indexed helper and its caller both carry the synthetic view param,
    # and the index is an O(1) slice index, not a `chars().nth` front walk.
    assert "fn peek(s: &str, i: i64, s_revl_cs: &[char]) -> i64" in src, src
    assert "fn scan(s: &str, i: i64, s_revl_cs: &[char]) -> i64" in src, src
    peek_body = src.split("fn peek(")[1].split("\n}")[0]
    assert "s_revl_cs[(i) as usize]" in peek_body, peek_body
    assert "chars().nth" not in peek_body, peek_body
    # the view threads straight through — no per-call collect anywhere.
    assert "peek(s, j, s_revl_cs)" in src.split("fn scan(")[1], src
    assert "chars().collect::<std::vec::Vec<char>>()" not in src, src
    # the `pub` entry keeps its owned `Str` ABI, collects once, and lends it.
    assert "pub fn run(src: String) -> i64" in src, src
    run_body = src.split("pub fn run(")[1].split("\n}")[0]
    assert ("let src_revl_cs: std::vec::Vec<char> = src.chars().collect();"
            in run_body), run_body
    assert "scan(&src, 0i64, &src_revl_cs)" in run_body, run_body
    # item 333: `via_slice` positionally SLICES its `s` param (`s.slice(1, 4)`),
    # which on a `str` is `chars().skip(1)` — an O(offset) front walk — so the
    # slice seeds the same `&[char]` view. `via_slice` grows the companion param
    # and `run` threads its own view down for free; the slice reads the view.
    assert "fn via_slice(s: &str, s_revl_cs: &[char]) -> i64" in src, src
    via_body = src.split("fn via_slice(")[1].split("\n}")[0]
    assert "s_revl_cs.iter().skip" in via_body, via_body
    assert "revl_slice" not in via_body, via_body
    assert "via_slice(&src, &src_revl_cs)" in run_body, run_body
    # `head` is still reached with a COMPUTED argument (the materialised slice),
    # which no caller holds a view for, so ITS slot is retracted rather than
    # paying a collect per call: `head`'s signature is unchanged and its own
    # `chars().nth` index walk stays.
    assert "fn head(s: &str) -> i64" in src, src
    head_body = src.split("fn head(")[1].split("\n}")[0]
    assert "chars().nth((0i64) as usize)" in head_body, head_body


@needs_cargo
def test_cargo_test_char_view_builds_and_runs(tmp_path):
    """item 277 definition-of-done: the threaded `&[char]` view cargo-builds AND
    runs, indexing the same Unicode scalars the `chars().nth` walk did."""
    src = emit.emit(compile_source(_CHARVIEW_RVL))
    result = _cargo_test(tmp_path, src, _CHARVIEW_TEST_MODULE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test result: ok" in result.stdout, result.stdout


def test_router_emits_per_realm_routing_struct():
    """item 167: a routed require lowers to a per-key router struct that
    re-resolves the live per-realm handle off a strict, realm-scoped
    committed-view read, and the routed key leaves the Inject gate."""
    src = emit.emit(compile_files([str(_ROUTER_RVL)]))
    # the router struct implements the required service and re-resolves per call
    assert "struct RevlRouterRouterWorker" in src
    assert "impl Worker for RevlRouterRouterWorker" in src
    assert 'isolate_with(self.key.as_str(), _revl_realm(realm.as_str()))' in src
    assert 'get::<Box<dyn Worker>>(self.key.as_str())' in src
    # the routed key is bound as the router, not a single-realm require, and is
    # not in the Router's Inject gate (it has no single-realm provider)
    assert "RevlRouterRouterWorker::_revl_new(ctx.clone())" in src
    router_fn = src.split("pub fn router()")[1]
    assert "cordis::Inject::none()" in router_fn.split("|ctx")[0]


@needs_cargo
def test_cargo_test_runs_router_round_robin_and_failover(tmp_path):
    """item 167 definition-of-done on this tier: the EMITTED Router body itself
    routes. Boots three worker realms + the Router and drives the routed
    `worker` provider through a probe — round-robin across live workers, then
    failover to the survivors when one withdraws — under a real `cargo test`."""
    src = emit.emit(compile_files([str(_ROUTER_RVL)])) + _ROUTER_TEST_MODULE
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_router"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test result: ok" in result.stdout, result.stdout
    assert "1 passed" in result.stdout, result.stdout


@needs_cargo
def test_cargo_check_compiles_non_ascii_in_format_position(tmp_path):
    """Item 135 / finding #35: a non-ASCII scalar in a string that reaches a
    format-macro position (`assert!`/`format!`) must `cargo check` clean.

    The lifecycle emitter threads the test *name* into every step's message,
    and the assert steps land it in `assert!(cond, "<name>: …")`. When the name
    carried an em dash, `_string` produced `\\u{2014}`; because that message is
    escaped a second time, the source ended up with a literal `\\u{2014}` whose
    string *value* is `{2014}` — which rustc's format parser reads as a
    reference to positional argument 2014, `error: invalid reference to
    positional argument 2014`. `#[test]` bodies only typecheck under `--tests`,
    so the check runs with it. Emitting the em dash literally (UTF-8) removes
    the brace and the collision."""
    ir = compile_source(
        """
        service Counter { fn count() -> Int  emission fn tick() }
        component TickCounter provides counter: Counter {
          let store = effect Map.new() undo store.drop()
          provide counter {
            fn count() = store.size()
            fn tick() {
              let key = `t-${store.size()}`
              effect store.insert(key, "x")
              undo store.remove(key)
            }
          }
        }
        component Heartbeat requires counter: Counter {
          every 10s { emit counter.tick() }
        }
        lifecycle test "an every-timer fires — on each advanced tick" {
          load TickCounter
          load Heartbeat
          advance 25s
          let ticks = call counter.count()
          assert ticks == 2
          unload Heartbeat
          unload TickCounter
          assert no_residue
        }
        """
    )
    src = emit.emit(ir)
    # the fix: the em dash rides through as literal UTF-8, never `\u{...}`.
    assert "—" in src and "\\u{" not in src
    result = _cargo_check(tmp_path, src, "--tests")
    assert result.returncode == 0, result.stderr
    assert "positional argument" not in result.stderr, result.stderr


def _lifecycle_timer_doc(test_name: str) -> str:
    """A minimal advance-based lifecycle test with *test_name* as its name,
    the same shape `test_cargo_check_compiles_non_ascii_in_format_position`
    uses -- factored out so the two item 106 regressions below (the
    double-escape residual case and the literal-brace case) share it."""
    return (
        "service Counter { fn count() -> Int  emission fn tick() }\n"
        "component TickCounter provides counter: Counter {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide counter {\n"
        "    fn count() = store.size()\n"
        "    fn tick() {\n"
        "      let key = `t-${store.size()}`\n"
        "      effect store.insert(key, \"x\")\n"
        "      undo store.remove(key)\n"
        "    }\n"
        "  }\n"
        "}\n"
        "component Heartbeat requires counter: Counter {\n"
        "  every 10s { emit counter.tick() }\n"
        "}\n"
        f'lifecycle test "{test_name}" {{\n'
        "  load TickCounter\n"
        "  load Heartbeat\n"
        "  advance 25s\n"
        "  let ticks = call counter.count()\n"
        "  assert ticks == 2\n"
        "  unload Heartbeat\n"
        "  unload TickCounter\n"
        "  assert no_residue\n"
        "}\n"
    )


@needs_cargo
def test_cargo_check_compiles_literal_brace_in_lifecycle_test_name(tmp_path):
    """Item 106, finding #35, sibling (b): a lifecycle test name containing a
    literal `{`/`}` -- no non-ASCII involved at all -- broke the SAME two
    `assert!` message sites item 135's non-ASCII fix did not touch, because
    `_string` never doubles a literal brace (it is meant for a plain string
    position, not a format-string one). `format!`'s parser reads an
    un-doubled `{curly}` as a named-argument reference:
    `error[E0425]: cannot find value 'curly' in this scope`. Fixed by
    `_escape_format_braces` (mirrors the existing `_v3_interp` idiom) at the
    two lifecycle `assert!` message sites only."""
    ir = compile_source(_lifecycle_timer_doc(
        "an every-timer fires {curly} on each advanced tick"))
    src = emit.emit(ir)
    # the fix: the brace survives DOUBLED in source, single at runtime.
    assert '{{curly}}' in src
    result = _cargo_check(tmp_path, src, "--tests")
    assert result.returncode == 0, result.stderr
    assert "cannot find value" not in result.stderr, result.stderr


@needs_cargo
def test_cargo_check_compiles_double_escaped_unprintable_in_lifecycle_test_name(tmp_path):
    """Item 106, finding #35, sibling (a): the lifecycle emitter built `where`
    (the per-test label folded into every load/unload/call/assert/no_residue
    message) via `_string(test['name'])`, then handed `where` to a SECOND
    `_string(where + suffix)` at each use. For a test name that still needs
    `_string`'s residual `\\u{XXXX}` escape (an unprintable scalar -- a
    printable one no longer takes this path since item 135), the second
    `_string` call re-escapes the first call's own backslash (`\\` ->
    `\\\\`), stranding `u{XXXX}` as literal, un-escaped text in the compiled
    string's runtime value -- and format!'s parser reads `{XXXX}` right back
    out of it, `error: invalid reference to positional argument`. Fixed by
    keeping `where` a raw, un-escaped label: `_string` runs exactly once, at
    each downstream call site."""
    ir = compile_source(_lifecycle_timer_doc(
        "an every-timer fires \x7f on each advanced tick"))
    src = emit.emit(ir)
    assert "\\\\u{" not in src, "where must not be pre-escaped before reuse"
    result = _cargo_check(tmp_path, src, "--tests")
    assert result.returncode == 0, result.stderr
    assert "positional argument" not in result.stderr, result.stderr


@needs_cargo
def test_cargo_test_runs_the_json_wire_roundtrip(tmp_path):
    """Item 140: the emitted @rs bodies must not merely typecheck — the JSON
    round-trip has to RUN. This builds the jsonwire scenario into a crate and
    runs `cargo test`, executing the `#[test]` that asserts
    `json_stringify(json_parse(doc)) == doc` for a structured document."""
    src = emit.emit(compile_files([str(_JSONWIRE_RVL)]))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_jsonwire"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test result: ok" in result.stdout, result.stdout
    assert "1 passed" in result.stdout, result.stdout


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
    cloned, the function value is passed by reference (`&f: Fn`).

    The loop binding `it` is inferred to the `List` element type (`Str`), so a
    by-value free-fn argument of that known non-Copy type is `.clone()`d — the
    always-sound `_by_value_arg` shape, mirrored by the self-hosted port and
    proven to build by the cargo gate below. What this test pins is the function
    value reaching the `impl Fn` slot by reference (`&call`), not the presence or
    absence of a clone on the value argument."""
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
    assert "apply(&call, it.clone())" in src
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
    # (b): the persistent push rebind is an in-place mutation (item 284) — the
    # self-reassigned `current = current.push(out)` overwrites its own value, so
    # cloning the whole accumulator per iteration is pure waste. `current` was
    # cloned for the by-value `complete(..)` consume, so it uniquely owns its
    # buffer at the rebind and the in-place `push` is sound; `_cargo_check` is
    # the borrow-checker proof of that ownership.
    assert "complete(current.clone())" in src
    assert "current.push(out);" in src
    assert "current = current.revl_push(out)" not in src
    result = _cargo_check(tmp_path, src)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Roadmap item 284 — elide the persistent-collection clone-on-append when the
# receiver is a dead, uniquely-owned self-reassignment (`out = out.push(x)`).
# Item 283 measured that lowering (`let _v = self.clone(); _v.push(item); _v`)
# driven once per token as ~85% of the rust self-host gap: an O(tokens^2)
# deep copy. The rewrite turns it into an in-place `out.push(x);`.


def test_self_reassigned_push_emits_in_place():
    """The canonical safe case rewrites to an in-place `push`, dropping the
    per-iteration `revl_push` clone entirely."""
    src = emit.emit(compile_source(
        "fn build(n: Int) -> List[Int] {\n"
        "  var out = []\n"
        "  var i = 0\n"
        "  while (i < n) { out = out.push(i) i = i + 1 }\n"
        "  return out\n"
        "}\n"
    ))
    assert "out.push(i);" in src
    assert "out.revl_push(i)" not in src


def test_self_reassigned_map_set_and_remove_emit_in_place():
    """The Map siblings share the clone-then-return shape and rewrite the same
    way: `m = m.set(k, v)` -> `m.insert(..)`, `m = m.remove(k)` -> `m.remove(..)`."""
    src = emit.emit(compile_source(
        "fn mbuild() -> Map[Str, Int] {\n"
        "  var m: Map[Str, Int] = Map.empty()\n"
        "  m = m.set(\"a\", 1)\n"
        "  m = m.remove(\"a\")\n"
        "  return m\n"
        "}\n"
    ))
    assert 'm.insert(String::from("a"), 1i64);' in src
    assert 'm.remove(&String::from("a"));' in src


def test_let_binding_of_persistent_append_is_not_rewritten():
    """A `let` introduces a NEW binding whose RHS receiver stays live, so it is
    NOT the dead-receiver shape and keeps the cloning `revl_push`. Only the
    self-reassignment `assign` is elided."""
    src = emit.emit(compile_source(
        "fn build(n: Int) -> List[Int] {\n"
        "  var out = []\n"
        "  var i = 0\n"
        "  while (i < n) { let t = out.push(i) out = t i = i + 1 }\n"
        "  return out\n"
        "}\n"
    ))
    assert "out.revl_push(i)" in src


@needs_cargo
def test_in_place_append_runs_identically_to_the_cloning_form(tmp_path):
    """(a) The strongest proof: two revl functions differing only in whether the
    append is a self-reassignment. `build_inplace` is rewritten to `out.push(i)`;
    `build_cloning` binds the append to a fresh `let` first, so it keeps the
    cloning `revl_push`. A `cargo test` proves both yield the identical sequence,
    so the optimisation is behaviour-preserving on real output."""
    src = emit.emit(compile_source(
        "fn build_inplace(n: Int) -> List[Int] {\n"
        "  var out = []\n"
        "  var i = 0\n"
        "  while (i < n) { out = out.push(i) i = i + 1 }\n"
        "  return out\n"
        "}\n"
        "fn build_cloning(n: Int) -> List[Int] {\n"
        "  var out = []\n"
        "  var i = 0\n"
        "  while (i < n) { let t = out.push(i) out = t i = i + 1 }\n"
        "  return out\n"
        "}\n"
    ))
    # the two forms diverge exactly at the append lowering
    assert "out.push(i);" in src
    assert "out.revl_push(i)" in src
    harness = (
        "#[cfg(test)]\n"
        "mod revl_item284 {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn inplace_matches_cloning_and_expected_sequence() {\n"
        "        for n in [0i64, 1, 2, 50, 1000] {\n"
        "            let got = build_inplace(n);\n"
        "            assert_eq!(got, build_cloning(n));\n"
        "            let expect: Vec<i64> = (0..n).collect();\n"
        "            assert_eq!(got, expect);\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    result = _cargo_test(tmp_path, src, harness)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_cargo
def test_deliberately_aliased_self_append_is_not_silently_miscompiled(tmp_path):
    """(b) The aliased case. A raw `let a = out` alias then `out = out.push(9)`:
    the in-place rewrite still fires (`out.push(9i64)`), and it stays sound
    because the alias binding CLONES (item 278: a `let` whose RHS is a bare
    binding reused later is a value copy, `let a = out.clone()`). So `a` is an
    independent buffer, `out` is uniquely owned at the in-place push, and the
    program compiles AND yields the correct revl value semantics: `a` unchanged
    at `[1, 2]`, `out` grown to `[1, 2, 9]`, so `[a[0], out[2]]` is `[1, 9]`.

    Item 284's soundness invariant holds via a stronger route than before: the
    only way a second *live* owner of the buffer could exist is a by-value move,
    and every such move the emitter emits clones first (`_by_value_arg`,
    `_by_value_tail`, and now the `let`-RHS reuse clone), so the in-place receiver
    is always the sole owner. A `cargo test` run proves the value, not just the
    build — the strongest form of "not silently miscompiled"."""
    src = emit.emit(compile_source(
        "fn aliased() -> List[Int] {\n"
        "  var out = [1, 2]\n"
        "  let a = out\n"
        "  out = out.push(9)\n"
        "  return [a[0], out[2]]\n"
        "}\n"
    ))
    # the in-place rewrite fires, and the alias clones so `out` stays owned.
    assert "out.push(9i64);" in src
    assert "let a = out.clone();" in src
    harness = (
        "#[cfg(test)]\n"
        "mod revl_item284_alias {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn aliased_appends_without_disturbing_the_copy() {\n"
        "        assert_eq!(aliased(), vec![1i64, 9i64]);\n"
        "    }\n"
        "}\n"
    )
    result = _cargo_test(tmp_path, src, harness)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Roadmap item 101 — reuse-after-move at a service-call argument (harness
# verification of item 93, finding #24). Item 93 cloned reused non-Copy values
# at free-function call sites; the same move recurs when a non-Copy value is
# passed *into a service call* — directly, or nested in a record literal that is
# the argument — and then reused (returned). The reused value must be cloned at
# the service-call argument / record-field site, exactly as `_by_value_arg`
# clones a free-function argument.


def test_service_call_record_field_clones_reused_value():
    """The harness `mtier` shape: a provide-method passes a `Str` by value into a
    service call inside a record literal (`{ content: answer }`) and then returns
    it. The record field moves `answer`, so the return would use a moved value
    (E0382) — the emitter clones the field value."""
    src = emit.emit(compile_source(
        "type Msg = { content: Str }\n"
        "service Sessions { fn append(id: Str, m: Msg) -> Int }\n"
        "service Chat { emission fn reply(id: Str, answer: Str) -> Str }\n"
        "component Server requires sessions: Sessions provides chat: Chat {\n"
        "  provide chat {\n"
        "    fn reply(id, answer) {\n"
        "      let _n = sessions.append(id, { content: answer })\n"
        "      return answer\n"
        "    }\n"
        "  }\n"
        "}\n"
    ))
    assert "Msg { content: answer.clone() }" in src


def test_service_call_direct_arg_clones_reused_value():
    """The same move without the record wrapper: a non-Copy value passed
    *directly* as a service-call argument and then reused is cloned at the call
    site (the item-93 argument clone extended to service-call arguments)."""
    src = emit.emit(compile_source(
        "service Log { fn write(line: Str) -> Int }\n"
        "service Echo { emission fn echo(line: Str) -> Str }\n"
        "component E requires log: Log provides echo: Echo {\n"
        "  provide echo {\n"
        "    fn echo(line) {\n"
        "      let _n = log.write(line)\n"
        "      return line\n"
        "    }\n"
        "  }\n"
        "}\n"
    ))
    assert "self.log.write(line.clone())" in src


@needs_cargo
def test_cargo_check_service_call_arg_reuse(tmp_path):
    """Real cargo gate for item 101: both the record-nested and the direct
    service-call-argument reuse shapes `cargo check` clean — no E0382."""
    src = emit.emit(compile_source(
        "type Msg = { content: Str }\n"
        "service Sessions { fn append(id: Str, m: Msg) -> Int }\n"
        "service Log { fn write(line: Str) -> Int }\n"
        "service Chat { emission fn reply(id: Str, answer: Str) -> Str }\n"
        "component Server requires sessions: Sessions, log: Log provides chat: Chat {\n"
        "  provide chat {\n"
        "    fn reply(id, answer) {\n"
        "      let _m = sessions.append(id, { content: answer })\n"
        "      let _w = log.write(answer)\n"
        "      return answer\n"
        "    }\n"
        "  }\n"
        "}\n"
    ))
    assert "content: answer.clone()" in src
    result = _cargo_check(tmp_path, src)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Roadmap item 114 — item 101's clone missed the `_Env` provide-method renderer.
# Item 101 cloned reused non-Copy values that are *params* (record fields /
# service-call args). But a `let`-bound *local* reused after an emit/effect was
# never typed in the effectful `_method_body_lines` path, so it still moved
# (E0382). The fix seeds the local's type — including a required-service method
# call's declared return — so `_by_value_arg` clones it, and pre-clones any
# method-body local an `undo` closure reads (a host-Map `insert(key, ..)` moves
# its key with no call-site clone, then the undo re-reads it).


def test_env_let_local_clones_when_reused_through_an_emit_record():
    """A provide method binds a non-Copy local with `let`, passes it into an
    `emit` service call inside a record literal, then returns it. The record
    field moved the local, so the return would use a moved value (E0382) — the
    emitter clones the reused local (item 114, the `_method_body_lines` gap that
    item 101's `_V3Ctx` fix left)."""
    src = emit.emit(compile_source(
        "type Msg = { content: Str }\n"
        "fn finalize(raw: Str) -> Str { return raw.concat(\"!\") }\n"
        "service Sessions { emission fn append(id: Str, m: Msg) }\n"
        "service Chat { emission fn reply(id: Str, raw: Str) -> Str }\n"
        "component Server requires sessions: Sessions provides chat: Chat {\n"
        "  provide chat {\n"
        "    fn reply(id, raw) {\n"
        "      let answer = finalize(raw)\n"
        "      emit sessions.append(id, { content: answer })\n"
        "      return answer\n"
        "    }\n"
        "  }\n"
        "}\n"))
    assert "content: answer.clone()" in src


def test_env_service_call_bound_local_is_typed_by_declared_return():
    """The same reuse where the local is bound from a *required-service* method
    call (`let answer = model.complete(seed)`) — a source `_v3_infer_type` alone
    cannot type. Item 114 reads the method's declared return, so the local is
    cloned at the reuse site."""
    src = emit.emit(compile_source(
        "type Msg = { content: Str }\n"
        "service Model { fn complete(seed: Str) -> Str }\n"
        "service Sessions { emission fn append(id: Str, m: Msg) }\n"
        "service Chat { emission fn reply(id: Str, seed: Str) -> Str }\n"
        "component Server requires model: Model, sessions: Sessions provides chat: Chat {\n"
        "  provide chat {\n"
        "    fn reply(id, seed) {\n"
        "      let answer = model.complete(seed)\n"
        "      emit sessions.append(id, { content: answer })\n"
        "      return answer\n"
        "    }\n"
        "  }\n"
        "}\n"))
    assert "content: answer.clone()" in src


def test_env_undo_closure_preclones_a_body_local_the_acquire_consumed():
    """An effect whose acquire consumes a method-body local by value (a host-Map
    `insert(key, ..)`) while its `undo` re-reads that local: the local is
    pre-cloned into `<l>_undo` before the acquire so the `move` undo closure owns
    a copy the acquire's move cannot invalidate (item 114)."""
    src = emit.emit(compile_source(
        "service KV { fn count() -> Int  emission fn put() }\n"
        "component MemKV provides kv: KV {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide kv {\n"
        "    fn count() = store.size()\n"
        "    fn put() {\n"
        "      let key = `k-${store.size()}`\n"
        "      effect store.insert(key, \"v\")\n"
        "      undo   store.remove(key)\n"
        "    }\n"
        "  }\n"
        "}\n"))
    assert "let key_undo = key.clone();" in src
    assert "store_undo.remove(&key_undo)" in src


@needs_cargo
def test_cargo_check_env_local_reuse_and_undo_preclone(tmp_path):
    """Real cargo gate for item 114 (mirrors item 101's gate): the `let answer`
    reuse-through-emit shape and the host-Map insert/undo local-reuse shape both
    `cargo check` clean — no E0382."""
    src = emit.emit(compile_source(
        "type Msg = { content: Str }\n"
        "fn finalize(raw: Str) -> Str { return raw.concat(\"!\") }\n"
        "service Model { fn complete(seed: Str) -> Str }\n"
        "service Sessions { emission fn append(id: Str, m: Msg) }\n"
        "service Chat { emission fn reply(id: Str, raw: Str) -> Str }\n"
        "component Server requires model: Model, sessions: Sessions provides chat: Chat {\n"
        "  provide chat {\n"
        "    fn reply(id, raw) {\n"
        "      let a1 = finalize(raw)\n"
        "      let a2 = model.complete(a1)\n"
        "      emit sessions.append(id, { content: a2 })\n"
        "      return a2\n"
        "    }\n"
        "  }\n"
        "}\n"
        "service KV { fn count() -> Int  emission fn put() }\n"
        "component MemKV provides kv: KV {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide kv {\n"
        "    fn count() = store.size()\n"
        "    fn put() {\n"
        "      let key = `k-${store.size()}`\n"
        "      effect store.insert(key, \"v\")\n"
        "      undo   store.remove(key)\n"
        "    }\n"
        "  }\n"
        "}\n"))
    assert "content: a2.clone()" in src
    assert "let key_undo = key.clone();" in src
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


# ---------------------------------------------------------------------------
# Roadmap item 112 (rust half) — the `advance` lifecycle step. Item 102 gave
# py/ts an `advance <n><unit>` lifecycle statement so a timer's firing is an
# assertable timeline step; on rust it used to fail hard ("unknown lifecycle
# step 'advance'"). Item 99 already gave rust a Clock, so `advance` lowers to
# `revl_clock_advance(ms)` and a rust lifecycle test drives the clock exactly
# like the reference tiers.


def test_advance_lifecycle_lowers_to_the_clock():
    """An `advance` step in a lifecycle test lowers to `revl_clock_advance(ms)`
    (not a hard refusal), the clock preamble is pulled in, and the clock is
    reset at test start so the test sees only its own timers."""
    ir = compile_files([str(ROOT / "examples" / "lifecycle_timer.rvl")])
    src = emit.emit(ir)
    assert "let _ = revl_clock_advance(35000);" in src    # advance 35s
    assert "let _ = revl_clock_advance(30000);" in src    # advance 30s
    assert "pub fn revl_clock_advance(ms: i64) -> usize" in src
    assert "revl_clock_reset();" in src


@needs_cargo
def test_advance_lifecycle_runs_on_real_cordis_rs(tmp_path):
    """Item 112 exit criterion on the rust tier: the shared lifecycle_timer doc
    (examples/lifecycle_timer.rvl) — an `every`/`after` timer advanced through
    the clock coeffect — RUNS on real cordis-rs. Its two `advance`-driven
    lifecycle tests fire the timers and assert the tick counts, the same doc the
    py/ts tiers run. Proves `advance` is really lowered (clock moves, timer
    body runs), not skipped."""
    ir = compile_files([str(ROOT / "examples" / "lifecycle_timer.rvl")])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        emit.cargo_toml("revl_advance_scn"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "2 passed" in result.stdout


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


# ===========================================================================
# Rust-tier coverage gaps found by the item-266 self-host benchmark: the three
# reasons the rust emitter raised on a self-host stage before cargo ever ran.
# Items 267 (char-class builtins), 268 (anonymous-record struct inference for a
# non-unique field-set), 269 (a record field colliding with an emitter-reserved
# name). docs/v2.0-roadmap.md items 267/268/269.


# --- 267: the item-233 char-class builtins on the rust tier ----------------

def test_char_class_builtins_lower_to_native_ascii_tests():
    """`is_digit`/`is_alpha`/`is_alnum`/`is_space` lower to the same native
    forms the python backend uses (backends/python/emit.py): a `&str` view
    bound once (`_rc`) and an ASCII code-point-order comparison. No revl-fn call
    and no `raise EmitError('unknown builtin method ...')` fall-through."""
    src = emit.emit(compile_source(
        "pub fn d(c: Str) -> Bool { return c.is_digit() }\n"
        "pub fn a(c: Str) -> Bool { return c.is_alpha() }\n"
        "pub fn n(c: Str) -> Bool { return c.is_alnum() }\n"
        "pub fn s(c: Str) -> Bool { return c.is_space() }\n"
    ))
    assert '{ let _rc: &str = &c; "0" <= _rc && _rc <= "9" }' in src
    assert ('{ let _rc: &str = &c; ("a" <= _rc && _rc <= "z") '
            '|| ("A" <= _rc && _rc <= "Z") }') in src
    assert ('{ let _rc: &str = &c; ("0" <= _rc && _rc <= "9") '
            '|| ("a" <= _rc && _rc <= "z") || ("A" <= _rc && _rc <= "Z") }') in src
    # is_space keeps the tab/LF/CR forms a revl string literal cannot spell
    # directly, so pin them here rather than at runtime.
    assert ('{ let _rc: &str = &c; _rc == " " || _rc == "\\t" '
            '|| _rc == "\\n" || _rc == "\\r" }') in src


@needs_cargo
def test_cargo_test_runs_char_class_semantics(tmp_path):
    """Not just compiles: the emitted `#[test]` executes the char-class
    semantics: the digit/alpha/alnum ranges, and that an empty receiver is
    total (false, never a fault), matching the python lowering."""
    ir = compile_source(
        "pub fn d(c: Str) -> Bool { return c.is_digit() }\n"
        "pub fn a(c: Str) -> Bool { return c.is_alpha() }\n"
        "pub fn n(c: Str) -> Bool { return c.is_alnum() }\n"
        "pub fn s(c: Str) -> Bool { return c.is_space() }\n"
        "test \"char classes mirror python\" {\n"
        "  assert d(\"5\") assert !d(\"x\") assert !d(\"\")\n"
        "  assert a(\"q\") assert a(\"Z\") assert !a(\"5\") assert !a(\"\")\n"
        "  assert n(\"5\") assert n(\"z\") assert n(\"A\") assert !n(\"-\")\n"
        "  assert s(\" \") assert !s(\"x\") assert !s(\"\")\n"
        "}\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = _cargo("test", tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout


# --- 268: anonymous record literal -> named struct, non-unique field-set ----

def test_ambiguous_record_literal_uses_return_type_context():
    """Two structurally-identical records share the field-set `{e, i}`, so it
    is not unique. A `return` of an anonymous `{i, e}` literal names the struct
    the enclosing fn is declared to return, not the raise it used to hit."""
    src = emit.emit(compile_source(
        "type PR = { i: Int, e: Int }\n"
        "type AtExpr = { i: Int, e: Int }\n"
        "pub fn as_pr() -> PR { return { i: 0, e: 1 } }\n"
        "pub fn as_at() -> AtExpr { return { i: 2, e: 3 } }\n"
    ))
    assert "return PR { i: 0i64, e: 1i64 };" in src
    assert "return AtExpr { i: 2i64, e: 3i64 };" in src


def test_ambiguous_record_literal_uses_assigned_var_type_context():
    """An `assign` to a typed local is target-type context too: the record
    literal names the local's declared struct."""
    src = emit.emit(compile_source(
        "type PR = { i: Int, e: Int }\n"
        "type AtExpr = { i: Int, e: Int }\n"
        "fn seed() -> AtExpr { return { i: 0, e: 0 } }\n"
        "pub fn go() -> AtExpr { var acc = seed() acc = { i: 2, e: 3 } return acc }\n"
    ))
    assert "acc = AtExpr { i: 2i64, e: 3i64 };" in src


def test_ambiguous_record_literal_uses_nested_field_type_context():
    """A field VALUE that is itself an anonymous ambiguous record resolves to
    the type the enclosing struct declares for that field."""
    src = emit.emit(compile_source(
        "type PR = { i: Int, e: Int }\n"
        "type AtExpr = { i: Int, e: Int }\n"
        "type Wrap = { inner: PR, tag: Int }\n"
        "pub fn w() -> Wrap { return { inner: { i: 1, e: 2 }, tag: 9 } }\n"
    ))
    assert "inner: PR { i: 1i64, e: 2i64 }" in src


def test_identical_shape_records_pick_deterministically_without_context():
    """With no target-type context but structurally-identical candidates, the
    literal still emits: a stable pick (lexicographically first) so the literal
    and any destructuring name the same interchangeable struct."""
    src = emit.emit(compile_source(
        "type Beta = { i: Int, e: Int }\n"
        "type Alpha = { i: Int, e: Int }\n"
        "pub fn free() -> Int { let x = { i: 7, e: 8 } return x.i }\n"
    ))
    assert "Alpha { i: 7i64, e: 8i64 }" in src  # Alpha < Beta


def test_differently_shaped_records_without_context_raise_clearly():
    """The one case the emitter still refuses: a non-unique field-set whose
    candidates differ in shape and no target type is in scope, where an
    ill-typed struct would be the only alternative, so it names the ambiguity
    instead."""
    with pytest.raises(emit.EmitError, match="differ in shape"):
        emit.emit(compile_source(
            "type A = { xs: List[Int], ok: Bool }\n"
            "type B = { xs: List[Str], ok: Bool }\n"
            "pub fn free() -> Int { let x = { xs: [], ok: true } return 0 }\n"
        ))


# --- 269: a record field named `ctx` (an emitter-reserved name) -------------

def test_reserved_name_is_escaped_not_refused():
    """`_mangle` escapes an emitter-reserved name the A3 way (append `_`) and
    `_ident` no longer raises on it. The reservation still holds because the
    emitter's own scaffolding emits the bare `ctx` token, which `ctx_` clears."""
    assert emit._mangle("ctx") == "ctx_"
    assert emit._mangle("config") == "config_"
    assert emit._ident("ctx", "record field") == "ctx_"


def test_user_ctx_field_and_local_emit_consistently():
    """selfhost/checker.rvl has both a record FIELD `ctx` and a local `ctx`;
    both escape to `ctx_` at the declaration and every use site, so the emitted
    crate is valid rust rather than a refused program. The CamelCase type name
    `Ctx` is untouched (not a reserved token)."""
    src = emit.emit(compile_source(
        "type Ctx = { n: Int }\n"
        "type Ck = { ctx: Ctx, msg: Str }\n"
        "pub fn mk(x: Ctx) -> Ck { return { ctx: x, msg: \"\" } }\n"
        "pub fn read(ck: Ck) -> Ctx { return ck.ctx }\n"
        "pub fn build() -> Ck { let ctx = mk_ctx() return { ctx: ctx, msg: \"m\" } }\n"
        "fn mk_ctx() -> Ctx { return { n: 0 } }\n"
    ))
    assert "    ctx_: Ctx," in src                       # struct field decl
    assert "return Ck { ctx_: x.clone(), msg:" in src    # construction
    assert "return ck.ctx_;" in src                      # field access
    assert "let ctx_ = mk_ctx();" in src                 # local binding decl
    assert "Ck { ctx_: ctx_.clone()" in src              # local used as field value
    assert "pub struct Ctx {" in src                     # type name untouched


@needs_cargo
def test_cargo_check_compiles_267_268_269_together(tmp_path):
    """All three constructs in one crate compile against real cordis-rs: the
    char-class builtins, an ambiguous-field-set record resolved by context, and
    a `ctx` record field/local. This is the emit-to-cargo proof for the new
    lowerings (the full self-host stages additionally exercise an unrelated
    by-value-clone gap and are covered at the emit level below)."""
    src = emit.emit(compile_source(
        "type PR = { i: Int, e: Int }\n"
        "type AtExpr = { i: Int, e: Int }\n"
        "type Ctx = { n: Int }\n"
        "type Ck = { ctx: Ctx, msg: Str }\n"
        "pub fn as_pr() -> PR { return { i: 0, e: 1 } }\n"
        "pub fn as_at() -> AtExpr { return { i: 2, e: 3 } }\n"
        "pub fn mk(x: Ctx) -> Ck { return { ctx: x, msg: \"\" } }\n"
        "pub fn read(ck: Ck) -> Ctx { return ck.ctx }\n"
        "pub fn d(c: Str) -> Bool { return c.is_digit() }\n"
        "pub fn s(c: Str) -> Bool { return c.is_space() }\n"
    ))
    result = _cargo_check(tmp_path, src)
    assert result.returncode == 0, result.stderr


# --- the item-266 proof: every self-host stage emits to rust (no EmitError) --

def test_selfhost_stages_emit_to_rust_without_error():
    """The item-266 blocker each of 267/268/269 removed: the lexer, parser,
    checker and lower stages could not be emitted to rust AT ALL: each raised
    an EmitError on the hot path before cargo ever ran. Now every stage emits,
    and the constructs that used to raise are present in the output. (emit_py
    stays out: its CPython-only `py_repr` extern is non-portable by design.)"""
    lexer = emit.emit(compile_files([str(ROOT / "selfhost" / "lexer.rvl")]))
    assert "{ let _rc: &str = &" in lexer             # 267 char-class lowering
    # 268: the parser (`{e, i}`) and lower (`{i, ok, xs}`) non-unique field-sets
    # used to raise; both now emit a complete crate.
    assert emit.emit(compile_files([str(ROOT / "selfhost" / "parser.rvl")]))
    assert emit.emit(compile_files([str(ROOT / "selfhost" / "lower.rvl")]))
    checker = emit.emit(compile_files([str(ROOT / "selfhost" / "checker.rvl")]))
    assert "ctx_:" in checker                          # 269 reserved-field escape


# ---------------------------------------------------------------------------
# item 278 — parser/checker/lower did not `cargo build` (the emit gaps beyond
# the item-270 lexer clone). The blockers and their fixes:
#   * E0072/E0391 — a recursive ADT is infinitely sized; the recursive edge of
#     each containment cycle gets a `Box` indirection, and a boxed match binding
#     is unboxed up front so the arm body is unchanged.
#   * E0382 — more move shapes: a `let`-RHS bare-binding move, a `Vec` consumed
#     by `.into_iter()` twice, and non-Copy field reuse in more positions.
#   * E0308 — two structurally-identical records used interchangeably (Rust sees
#     distinct nominal types); one canonical struct + `type` aliases unify them.
#   * E0282 — an empty `vec![]` accumulator gets its element type annotated.

# A recursive AST: `Expr` carries a per-case struct (`BinN`) that again contains
# `Expr`, the exact shape the self-host parser/checker/lower use. Without a `Box`
# it is infinitely sized (E0072) and its drop-check cycles (E0391).
_RECURSIVE_ADT_RVL = """
pub type BinN = { op: Str, l: Expr, r: Expr }
pub type Expr = Lit(Str) | Bin(BinN)

pub fn mk(op: Str, l: Expr, r: Expr) -> Expr { return Bin({ op: op, l: l, r: r }) }

pub fn render(e: Expr) -> Str {
  return match e {
    Lit(v) => v,
    Bin(b) => render(b.l).concat(b.op).concat(render(b.r)),
  }
}
"""


def test_recursive_adt_boxes_the_recursive_edge():
    """The recursive enum payload is `Box`ed, construction moves it onto the heap
    with `Box::new`, and the match binding is unboxed (`let b = *b;`) so the arm
    body reads the payload exactly as an unboxed one would."""
    src = emit.emit(compile_source(_RECURSIVE_ADT_RVL))
    assert "Bin(Box<BinN>)," in src                      # boxed enum payload
    assert "Bin(Box::new(" in src                        # boxed at construction
    assert "Bin(b) => { let b = *b;" in src              # unboxed at the binding
    # the non-recursive `Lit(Str)` payload is left unboxed.
    assert "Lit(String)," in src


@needs_cargo
def test_recursive_adt_cargo_builds(tmp_path):
    """The E0072/E0391 payoff: a recursive datatype now cargo-builds (before the
    fix it was infinitely sized and failed drop-check)."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(_RECURSIVE_ADT_RVL)))
    assert result.returncode == 0, result.stderr


# A wildcarded payload on a *boxed* (recursive) case: `Arrow(_)` binds nothing,
# so there is no name for the boxed-case unboxing step to dereference. Before
# the fix, `_v3_match_expr` unboxed unconditionally whenever `arm["bind"]` was
# truthy, and the parser records a wildcarded payload as `bind == "_"` (a real,
# non-empty string) rather than `None` — so the emitter treated `_` as a real
# binding name and wrote `let _ = *_;`, which cargo refuses: "in expressions,
# `_` can only be used on the left-hand side of an assignment" (E0425-shaped).
# Found via selfhost/lower.rvl and selfhost/parser.rvl, both of which worked
# around it by binding the payload under a name they never use.
_WILDCARD_BOXED_PAYLOAD_RVL = """
pub type Expr = Arrow(Expr) | Lit(Int)

pub fn is_arrow(e: Expr) -> Bool {
  return match e { Arrow(_) => true, _ => false }
}
"""


def test_wildcard_boxed_payload_emits_no_deref_of_underscore():
    """A wildcarded payload on a recursive (boxed) case renders the bare `_`
    pattern with no unboxing `let` at all -- there is nothing to unbox."""
    src = emit.emit(compile_source(_WILDCARD_BOXED_PAYLOAD_RVL))
    assert "Expr::Arrow(_) => true," in src
    # the malformed construct this regression guards against, verbatim.
    assert "let _ = *_;" not in src


@needs_cargo
def test_cargo_check_wildcard_boxed_payload_compiles(tmp_path):
    """The payoff: a wildcarded payload on a recursive case now cargo-builds
    (before the fix, `let _ = *_;` failed with a hard parse/expression error)."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(_WILDCARD_BOXED_PAYLOAD_RVL)))
    assert result.returncode == 0, result.stderr


# Two records with the IDENTICAL field->type shape, used interchangeably: `Bind`
# is built and flows into a `List[Param]` slot. Rust sees distinct nominal types
# (E0308) unless they unify to one struct.
_SAME_SHAPE_RVL = """
pub type Param = { name: Str, ty: Str }
pub type Bind = { name: Str, ty: Str }
pub type Fn2 = { params: List[Param] }

pub fn one() -> Fn2 { let p = { name: "x", ty: "" } return { params: [p] } }
"""


def test_structurally_identical_records_unify_via_type_alias():
    """The first record of a shape is the canonical struct; a structural twin
    becomes a `type` alias, so `Bind` and `Param` are one Rust type and a
    `Vec<Bind>` is admitted where `Vec<Param>` is declared."""
    src = emit.emit(compile_source(_SAME_SHAPE_RVL))
    assert "pub struct Param {" in src
    assert "pub type Bind = Param;" in src
    assert "pub struct Bind {" not in src


@needs_cargo
def test_structurally_identical_records_cargo_build(tmp_path):
    """The E0308 payoff: the interchangeable-record program cargo-builds."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(_SAME_SHAPE_RVL)))
    assert result.returncode == 0, result.stderr


# A `Vec` iterated twice moves on the first `.into_iter()` and borrows a moved
# value on the second (E0382); the reused iterable is cloned. The empty-vec
# accumulator `out` also needs its element type annotated (E0282).
_ITER_TWICE_RVL = """
pub fn both(lines: List[Str]) -> List[Str] {
  var out = []
  for (a of lines) { out = out.push(a) }
  for (b of lines) { out = out.push(b) }
  return out
}
"""


def test_vec_iterated_twice_clones_and_annotates_empty_accumulator():
    """A reused `Vec` binding iterated twice is cloned at each loop, and the
    empty `vec![]` accumulator is annotated with its element type."""
    src = emit.emit(compile_source(_ITER_TWICE_RVL))
    assert "for a in lines.clone()" in src               # into_iter-twice clone
    assert "let mut out: Vec<String> = vec![]" in src    # E0282 annotation


@needs_cargo
def test_vec_iterated_twice_cargo_builds(tmp_path):
    """The into_iter-twice + empty-accumulator payoff: it cargo-builds."""
    result = _cargo_check(tmp_path, emit.emit(compile_source(_ITER_TWICE_RVL)))
    assert result.returncode == 0, result.stderr


@needs_cargo
def test_all_selfhost_stages_cargo_build(tmp_path):
    """The item-278 definition-of-done: EACH of the lexer/parser/checker/lower
    self-host stages emits AND `cargo build`s (item 270 got only the lexer; the
    other three hit the E0072/E0382/E0308/E0282 gaps this item closes). This is
    the precondition for the item-266 full-pipeline native benchmark."""
    for stage in ("lexer", "parser", "checker", "lower"):
        crate = tmp_path / stage
        crate.mkdir()
        src = emit.emit(compile_files([str(ROOT / "selfhost" / f"{stage}.rvl")]))
        result = _cargo_check(crate, src)
        assert result.returncode == 0, f"{stage} failed:\n{result.stderr}"


# ---------------------------------------------------------------------------
# item 243 Slice 2b (rust): the witnessed-effects three-entry-kind teardown
# loop. Design: docs/design/243-witnessed-externs.md,
# docs/design/teardown-contract.md. This mirrors
# tests/test_witnessed_runtime.py's three py runtime proofs plus the
# teardown-contract's a5a/a5b compensation respec, driven against the REAL
# cordis-rs crate (not a stub) — `cargo test` executes the emitted code.
#
# The fixture shares one `REVL_TEST_LOG` static across every `#[test]` fn in
# the crate, so the harness runs `cargo test` single-threaded
# (`--test-threads=1`, via `_cargo_test_seq` below) rather than through the
# suite's usual `_cargo_test` helper — parallel test threads would interleave
# unrelated activations' log lines.
# ---------------------------------------------------------------------------

_TEARDOWN_LOOP_RVL = """
type Stash = { n: Int }
type FsError = { code: Str }

extern pure fn record_undo(w: Stash) -> Unit = @rs {
    revl_log(format!("undo({})", w.n));
}
extern witnessed[fs] fn stash() -> Result[Stash, FsError] undo record_undo(result) = @rs {
    revl_log(String::from("stash"));
    Ok(Stash { n: 1 })
}
extern pure fn record_bracket_undo() -> Unit = @rs {
    revl_log(String::from("bracket_undo"));
}
extern acquire fn stash_acq() -> Stash undo record_bracket_undo() = @rs {
    revl_log(String::from("stash_acq"));
    Stash { n: 2 }
}
extern emission fn log_insert(s: Str) -> Int = @rs { revl_log(format!("insert({})", s)); 0 }
extern emission fn log_delete(s: Str) -> Int = @rs { revl_log(format!("delete({})", s)); 0 }
extern emission fn slow_delete(s: Str) -> Int = @rs {
    std::thread::sleep(std::time::Duration::from_millis(60));
    revl_log(format!("slow_delete({})", s));
    0
}

service Db { fn noop() -> Int
  emission fn ex(s: Str) -> Int }

component DbImpl provides db: Db {
  provide db {
    fn noop() { return 0 }
    fn ex(s) { emit log_insert(s) compensate log_delete(s) return 0 }
  }
}

component StashOk {
  effect stash()
}
component StashAbort {
  effect stash()
  fail "boom"
}
component AcqComp {
  let h = effect stash_acq() undo record_bracket_undo()
}
component CompOk {
  emit log_insert("row") compensate log_delete("row")
}
component CompAbort {
  let h = effect stash_acq() undo record_bracket_undo()
  emit log_insert("row") compensate log_delete("row")
  fail "boom2"
}
component CompBudget {
  emit log_insert("a") compensate log_delete("a")
  emit log_insert("b") compensate slow_delete("b")
  fail "boom3"
}
"""

_TEARDOWN_LOOP_HARNESS = """
static REVL_TEST_LOG: std::sync::OnceLock<std::sync::Mutex<Vec<String>>> = std::sync::OnceLock::new();

fn revl_log(s: String) {
    REVL_TEST_LOG.get_or_init(|| std::sync::Mutex::new(Vec::new())).lock().unwrap().push(s);
}
fn revl_log_clear() {
    REVL_TEST_LOG.get_or_init(|| std::sync::Mutex::new(Vec::new())).lock().unwrap().clear();
}
fn revl_log_snapshot() -> Vec<String> {
    REVL_TEST_LOG.get().map(|m| m.lock().unwrap().clone()).unwrap_or_default()
}

// 1. witnessed + clean unload: the mutation PERSISTS, the inverse is
// discharged (docs/design/243-witnessed-externs.md's central distinction).
#[test]
fn witnessed_persists_on_clean_unload() {
    revl_log_clear();
    let root = cordis::Context::new();
    let fiber = root.plugin(stash_ok(), ());
    fiber.wait().unwrap();
    assert_eq!(revl_log_snapshot(), vec!["stash".to_string()]);
    fiber.dispose().unwrap();
    assert_eq!(revl_log_snapshot(), vec!["stash".to_string()], "clean unload wrongly replayed the witnessed inverse");
}

// 2. witnessed + mid-activation abort: the inverse REPLAYS (A8).
#[test]
fn witnessed_reverts_on_abort() {
    revl_log_clear();
    let root = cordis::Context::new();
    let fiber = root.plugin(stash_abort(), ());
    assert!(fiber.wait().is_err());
    assert_eq!(revl_log_snapshot(), vec!["stash".to_string(), "undo(1)".to_string()]);
}

// 3. acquire + clean unload: the bracket STILL reverts (unchanged) — proves
// the two entry kinds are observably distinct at runtime.
#[test]
fn bracket_still_reverts_on_clean_unload() {
    revl_log_clear();
    let root = cordis::Context::new();
    let fiber = root.plugin(acq_comp(), ());
    fiber.wait().unwrap();
    assert_eq!(revl_log_snapshot(), vec!["stash_acq".to_string()]);
    fiber.dispose().unwrap();
    assert_eq!(revl_log_snapshot(), vec!["stash_acq".to_string(), "bracket_undo".to_string()]);
}

// a5a: compensation discharges on clean unload — no DELETE fires, the
// forward emission (the insert) survives as the deliverable.
#[test]
fn compensation_discharges_on_clean_unload() {
    revl_log_clear();
    let root = cordis::Context::new();
    let fiber = root.plugin(comp_ok(), ());
    fiber.wait().unwrap();
    assert_eq!(revl_log_snapshot(), vec!["insert(row)".to_string()]);
    fiber.dispose().unwrap();
    assert_eq!(revl_log_snapshot(), vec!["insert(row)".to_string()], "clean unload wrongly fired the compensation");
}

// a5b: two-phase abort — every Phase-1 (bracket/transactional) inverse
// completes BEFORE the first Phase-2 compensation, the exact inversion of
// the old single-interleaved-LIFO placeholder order.
#[test]
fn compensation_two_phase_abort_orders_after_bracket() {
    revl_log_clear();
    let root = cordis::Context::new();
    let fiber = root.plugin(comp_abort(), ());
    assert!(fiber.wait().is_err());
    assert_eq!(revl_log_snapshot(), vec![
        "stash_acq".to_string(),
        "insert(row)".to_string(),
        "bracket_undo".to_string(),
        "delete(row)".to_string(),
    ]);
}

// method-level compensation (a provide-method's `emit ... compensate`)
// discharges on clean unload too — proves the `Context::extend`/`metadata`
// plumbing (`revl_teardown_of`) recovers the SAME activation state a
// provide-method registers into, not a stale or missing one.
#[test]
fn method_level_compensation_discharges_on_clean_unload() {
    revl_log_clear();
    let root = cordis::Context::new();
    let fiber = root.plugin(db_impl(), ());
    fiber.wait().unwrap();
    let db = root.require::<Box<dyn Db>>("db").unwrap();
    db.ex("m".to_string());
    assert_eq!(revl_log_snapshot(), vec!["insert(m)".to_string()]);
    fiber.dispose().unwrap();
    assert_eq!(revl_log_snapshot(), vec!["insert(m)".to_string()], "clean unload wrongly fired the method-level compensation");
}

// Phase-2 bound: a between-compensation deadline check (rust has no in-call
// preemption — teardown-contract.md's rust row). `slow_delete` sleeps past
// the budget, so the compensation queued behind it is skipped, not run.
#[test]
fn compensation_budget_skips_after_deadline() {
    revl_log_clear();
    std::env::set_var("REVL_COMPENSATION_BUDGET_MS", "10");
    let root = cordis::Context::new();
    let fiber = root.plugin(comp_budget(), ());
    assert!(fiber.wait().is_err());
    let log = revl_log_snapshot();
    std::env::remove_var("REVL_COMPENSATION_BUDGET_MS");
    assert!(log.contains(&"slow_delete(b)".to_string()), "{log:?}");
    assert!(!log.contains(&"delete(a)".to_string()), "budget did not skip the later compensation: {log:?}");
}
"""


@needs_cargo
def test_witnessed_teardown_loop_runs_on_real_cordis_rs(tmp_path):
    """item 243 Slice 2b, runtime proof: the three-entry-kind teardown loop
    (bracket unchanged, transactional abort-only replay + commit discharge,
    compensation two-phase abort + budget bound) driven against the real
    cordis-rs crate. See `_TEARDOWN_LOOP_HARNESS` for the seven assertions."""
    ir = compile_source(_TEARDOWN_LOOP_RVL, "teardown_loop.rvl")
    src = emit.emit(ir)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src + "\n" + _TEARDOWN_LOOP_HARNESS, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    # `REVL_TEST_LOG` is one static shared by every #[test] fn in this crate;
    # cargo's default parallel test threads would interleave unrelated
    # activations' log lines, so this one crate runs single-threaded.
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, result.stdout + result.stderr


def test_witnessed_call_site_emits_transactional_registration():
    """Emit-only companion (no cargo needed): the witnessed call site compiles
    to a transactional registration keyed off `committed`, not a plain
    always-replaying bracket disposer."""
    ir = compile_source(_TEARDOWN_LOOP_RVL, "teardown_loop.rvl")
    src = emit.emit(ir)
    assert "let (ctx, _revl_teardown) = revl_teardown_begin(&ctx," in src
    assert 'if let Ok(ref _revl_witv0) = _revl_wit0 {' in src
    assert "let _ = unstash" not in src  # sanity: undo is `record_undo`, not a stray name
    assert "RevlPendingCompensation" in src
    assert "_revl_state.phase2.lock().unwrap().push(" in src


def test_non_witnessed_non_compensating_program_is_untouched():
    """A program using neither `witnessed` nor `emit ... compensate` must
    emit byte-identically to before this slice: no `RevlTeardown`, no
    `revl_teardown_begin`, no phase-2 machinery at all."""
    # An ordinary bracket. Spelled `effect Map.new() undo Map.new()` before —
    # an inverse that ACQUIRES a second host Map rather than releasing the
    # first, which a teardown slot now refuses; nothing this test asserts
    # depends on that shape.
    src = emit.emit(compile_source(
        "component C { let m = effect Map.new() undo m.drop() }\n", "plain.rvl"))
    assert "RevlTeardown" not in src
    assert "revl_teardown_begin" not in src
    assert "phase2" not in src


# ---------------------------------------------------------------------------
# item 324 (rust): THE per-tool-call H1 gate — a witnessed fs mutation that
# fires from a PROVIDE-METHOD body, per request, after activation. Mirrors the
# py reference tests/test_provide_method_witnessed.py, driven against the REAL
# cordis-rs crate (`cargo test`).
#
# 318 opened this on py: a witnessed effect can now fire from a provide-method
# body (per tool call), registering its transactional inverse into the
# enclosing component's activation frame, so the mutation PERSISTS on clean
# session-end and REVERTS residue-free on abort. This proves the same closed
# loop on the rust tier: the method-body witnessed effect registers a
# `transactional` disposer on the component's `RevlTeardown` (via
# `revl_teardown_of(&self.ctx)`), disposed by cordis-rs's LIFO unload where it
# reads the settled `committed` bit.
#
# THE DISPOSAL-ORDERING HAZARD (318 found it on py) does NOT arise on rust:
# rust flips `committed` EAGERLY at activation-end, so by the time a per-call
# method registers its sibling `self.ctx.effect` disposer the commit bit is
# already settled — no premature-disposal window, no park-for-drain needed.
# See `_emit_method_witnessed_step` in backends/rust/emit.py for the analysis.
#
# The witnessed extern is a rename-with-a-data-witness stand-in (a rename is
# enough to exercise the runtime path), parameterised by the target path so
# each per-call invocation mutates a DISTINCT file — the shape of an agent
# calling one fs tool repeatedly. That distinctness is what makes the abort
# proof "all calls, not just the last": each file is an independent crossing,
# and abort must revert every one.
# ---------------------------------------------------------------------------

_METHOD_WITNESSED_RVL = """
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }

extern pure fn unstash(w: Stash) -> Unit = @rs {
    let _ = std::fs::rename(&w.bak, &w.path);
}
extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]
    undo unstash(result) = @rs {
    let bak = format!("{}.bak", p);
    match std::fs::rename(&p, &bak) {
        Ok(()) => Ok(Stash { path: p.clone(), bak }),
        Err(e) => Err(FsError { code: e.to_string() }),
    }
}

service Ops { emission fn touch(p: Str) }

component Agent provides ops: Ops {
  provide ops {
    fn touch(p) {
      effect stash_path(p)
    }
  }
}
"""

# The activation label `revl_teardown_begin` keys the abort registry under is
# `<component>.teardown.phase2` (see `_emit_teardown_begin`). A session-level
# reject reaches the activation's `RevlTeardown` through it.
_METHOD_WITNESSED_ABORT_LABEL = "Agent.teardown.phase2"

_METHOD_WITNESSED_HARNESS = '''
use std::path::Path;

// A fresh temp directory per #[test], so the three per-call artifacts never
// collide with another test's (the crate runs single-threaded, but names would
// otherwise repeat across tests).
fn revl_tmpdir(tag: &str) -> std::path::PathBuf {
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let dir = std::env::temp_dir().join(format!("revl324-{}-{}-{}", tag, std::process::id(), nonce));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

// Three distinct deliverable files, one per simulated tool call.
fn revl_setup_files(dir: &Path) -> Vec<String> {
    let mut paths = Vec::new();
    for i in 0..3 {
        let p = dir.join(format!("artifact_{}.txt", i));
        std::fs::write(&p, format!("deliverable {}", i)).unwrap();
        paths.push(p.to_string_lossy().into_owned());
    }
    paths
}

// The witnessed rename ran and stuck: original gone, backup present.
fn revl_mutated(p: &str) -> bool {
    !Path::new(p).exists() && Path::new(&format!("{}.bak", p)).exists()
}

// The world is as it started: original present, no backup residue.
fn revl_pristine(p: &str) -> bool {
    Path::new(p).exists() && !Path::new(&format!("{}.bak", p)).exists()
}

fn revl_cleanup(dir: &Path) {
    let _ = std::fs::remove_dir_all(dir);
}

// 1. per-tool-call witnessed mutation PERSISTS on a clean unload (commit).
#[test]
fn per_tool_call_mutations_persist_on_clean_unload() {
    let dir = revl_tmpdir("persist");
    let files = revl_setup_files(&dir);

    let root = cordis::Context::new();
    let fiber = root.plugin(agent(), ());
    fiber.wait().unwrap();
    let ops = root.require::<Box<dyn Ops>>("ops").unwrap();

    // each tool call runs the provide-method, registering ONE transactional
    // inverse into the component's activation frame (per-tool-call H1).
    for f in &files {
        ops.touch(f.clone());
        assert!(revl_mutated(f), "the witnessed mutation did not apply on the call: {f}");
    }

    fiber.dispose().unwrap();  // clean unload == implicit commit

    // the deliverable persists on EVERY path; nothing was reverted.
    for f in &files {
        assert!(revl_mutated(f), "clean unload wrongly reverted a per-call mutation: {f}");
    }
    revl_cleanup(&dir);
}

// 2. per-tool-call witnessed mutation REVERTS on abort, residue-free — and
// 3. the abort is all-or-nothing across every independent per-call mutation,
//    not just the last (each file is a distinct crossing on the shared frame).
#[test]
fn per_tool_call_mutations_revert_on_abort_every_call() {
    let dir = revl_tmpdir("revert");
    let files = revl_setup_files(&dir);

    let root = cordis::Context::new();
    let fiber = root.plugin(agent(), ());
    fiber.wait().unwrap();
    let ops = root.require::<Box<dyn Ops>>("ops").unwrap();

    for f in &files {
        ops.touch(f.clone());
        assert!(revl_mutated(f), "mutation did not apply: {f}");
    }

    // abort the session's work (item 245's reject drives this seam): clear
    // `committed` so the next teardown REPLAYS every inverse instead of
    // discharging it.
    revl_abort("''' + _METHOD_WITNESSED_ABORT_LABEL + '''");
    fiber.dispose().unwrap();

    // EVERY per-call mutation reverted, and the teardown left no residue: the
    // residue set is fully enumerable (every artifact path) and empty.
    for f in &files {
        assert!(revl_pristine(f), "abort did not revert a per-call mutation: {f}");
    }
    revl_cleanup(&dir);
}

// 4. control: a component that already activated cleanly and is NOT aborted
//    commits — proves the abort in test 2 is what caused the revert, not an
//    always-revert bug.
#[test]
fn no_abort_commits_the_deliverable() {
    let dir = revl_tmpdir("control");
    let files = revl_setup_files(&dir);

    let root = cordis::Context::new();
    let fiber = root.plugin(agent(), ());
    fiber.wait().unwrap();
    let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
    for f in &files { ops.touch(f.clone()); }

    fiber.dispose().unwrap();  // no revl_abort -> commit

    for f in &files {
        assert!(revl_mutated(f), "unaborted clean unload must persist: {f}");
    }
    revl_cleanup(&dir);
}
'''


@needs_cargo
def test_method_witnessed_h1_runs_on_real_cordis_rs(tmp_path):
    """item 324 runtime proof: a witnessed fs mutation fired from a
    provide-method PER TOOL CALL persists on a clean unload and reverts,
    residue-free across every call, on abort — driven against the real
    cordis-rs crate. Mirrors tests/test_provide_method_witnessed.py."""
    ir = compile_source(_METHOD_WITNESSED_RVL, "method_witnessed.rvl")
    src = emit.emit(ir)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        src + "\n" + _METHOD_WITNESSED_HARNESS, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_m324"), encoding="utf-8")
    # each #[test] uses its own temp dir, so parallel threads are safe here, but
    # keep it single-threaded for parity with the Slice-2b teardown harness.
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, result.stdout + result.stderr


def test_method_witnessed_call_site_emits_transactional_registration():
    """Emit-only companion (no cargo): a witnessed effect in a PROVIDE-METHOD
    body compiles to a transactional registration on the component's activation
    frame — recovered via `revl_teardown_of(&self.ctx)` and keyed off
    `committed` — NOT a plain always-replaying bracket. And the component gains
    the `RevlTeardown` accumulator even though its ACTIVATION body has no
    witnessed effect (the method-body detection must trigger it)."""
    ir = compile_source(_METHOD_WITNESSED_RVL, "method_witnessed.rvl")
    src = emit.emit(ir)
    # the activation built the accumulator despite an empty activation body...
    assert "revl_teardown_begin(&ctx," in src
    # ...and the method registers a transactional (committed-gated) disposer on
    # the recovered frame, fire-and-forget (method sig is non-Result).
    assert "let _revl_state = revl_teardown_of(&self.ctx);" in src
    assert 'let _ = self.ctx.effect("Agent.touch.witnessed", move || {' in src
    assert "if !_revl_state.committed.load" in src
    assert "let _ = unstash(result);" in src
    # the out-of-band abort seam is present.
    assert "fn revl_abort(label: &str) {" in src
    # and the method effect is NOT the old always-replaying bracket shape.
    assert "let _ = self.ctx.effect(\"Agent.touch.effect" not in src


def test_method_witnessed_does_not_perturb_non_witnessed_methods():
    """A provide-method with a plain (non-witnessed) `effect` still emits the
    always-replaying bracket, unchanged by item 324 — the witnessed dispatch is
    gated on the extern class."""
    src = emit.emit(compile_source(
        "extern acquire fn acq() -> Acq undo rel() = @rs { 0 }\n"
        "extern pure fn rel() -> Unit = @rs { }\n"
        "service S { fn go() }\n"
        "component C provides s: S {\n"
        "  provide s { fn go() { effect acq() undo rel() } }\n"
        "}\n", "plain_method.rvl"))
    # non-witnessed method effect: the bracket disposer, always-replaying, no
    # committed gate, no RevlTeardown at all.
    assert "committed" not in src
    assert "RevlTeardown" not in src
    assert "self.ctx.effect(" in src


# ---------------------------------------------------------------------------
# item 130 Slice 3 — `Stream[T]` on the cordis-rs (blocking) tier.
#
# docs/design/130-stream-reactive-types.md §4.6, the rust row: this tier ERASES
# the async color. `next` blocks on a race between the item queue and the
# subscription's CANCEL signal, and `close` trips that signal — the shape that
# keeps the bracket inverse reachable off the teardown thread (§9 Part A), so a
# `next` parked on a provider that never emits cannot deadlock teardown.
# ---------------------------------------------------------------------------

_STREAM_SCENARIO = ROOT / "backends" / "rust" / "scenarios" / "stream.rvl"


def _stream_src() -> str:
    return emit.emit(compile_files([str(_STREAM_SCENARIO)]))


def test_subscribe_is_an_ordinary_bracket_whose_inverse_is_close():
    """The CORE GUARANTEE's mechanism on this tier: the subscription is a
    `ctx.effect` bracket like any other, so unloading the owner runs `close`
    through the same LIFO disposer stack a Pool rides."""
    src = _stream_src()
    assert ('let sub = Arc::new(Stream::subscribe(&src, "error", 0usize));'
            in src)
    assert 'ctx.effect("Consumer.sub.undo", move || { sub_undo.close(); Ok(()) })?;' in src


def test_next_parks_on_the_item_terminal_cancel_race():
    """`next` erases to a blocking park whose wake conditions are item /
    terminal / cancel, with the CANCEL checked first — the cancellation-first
    priority the design's `select!` spells and a condvar wake does not order on
    its own."""
    src = _stream_src()
    assert "fn next(&self) -> Result<StreamNext, String> {" in src
    assert "if st.cancelled {" in src
    assert "st = self.wake.wait(st).unwrap();" in src
    # `close` trips the signal and wakes the park, synchronously
    assert "st.cancelled = true;" in src
    assert "self.wake.notify_all();" in src


def test_a_faulted_terminal_fails_the_activation():
    """A `Faulted` terminal (a provider abort, or an `error`-policy overflow) is
    an error at the await site, so the accumulated prefix — the subscription
    bracket included — reverts LIFO. A `Closed` terminal is an ordinary value."""
    src = _stream_src()
    assert ("sub.next().map_err(|e| cordis::CordisError::with_message("
            "cordis::ErrorCode::Plugin, e))?;") in src


def test_merge_rides_the_subscriptions_single_bracket():
    """`subscribe merge(a, b)` opens the fan-in INSIDE the subscription's
    acquisition, so multi-source teardown stays ONE LIFO stack: the single
    bracket inverse closes the subscription, that closes the merge it owns, and
    closing the merge detaches it from both sources — which keep their own
    brackets (design §1)."""
    src = _stream_src()
    assert 'Stream::subscribe(&Stream::merge(&a, &b), "error", 0usize)' in src
    fanin = src.split("pub fn fanin()", 1)[1].split("\npub fn ", 1)[0]
    # three brackets — one per source, one for the subscription. The fan-in adds
    # NO fourth: it rides the subscription's.
    assert fanin.count("ctx.effect(") == 3
    # the subscription's close cascades into a derived upstream
    assert 'if closed && self.inner.src.kind != "source" {' in src


def test_stream_free_program_is_byte_identical():
    """The stream host block is pulled in only by a document that reaches a
    stream, so every stream-free program on this tier emits exactly as before."""
    src = emit.emit(_ir("user_cache"))
    assert "Subscription" not in src
    assert "StreamNext" not in src
    assert "revl_stream_pending" not in src


@needs_cargo
def test_stream_runtime_on_real_cordis_rs(tmp_path):
    """Definition of done for the rust tier: the emitted components RUN on real
    cordis-rs and prove, by running,

      * §10.2 the core guarantee — unloading the owner closes the stream, LIFO,
        with no residue;
      * §9 Part A — a parked `next` is terminated by the owner's own bracket
        inverse, run from another thread, which returns without waiting for the
        park to drain;
      * §9 Part B — a provider fault terminates an outstanding `next` through a
        real activation, and the failed activation closes the subscription;
      * `merge` — an item from either source reaches the one consumer, the
        fan-in tears down as one LIFO stack, one source's close does not strand
        the consumer on the other, and a source's fault propagates at once;
      * backpressure `error` — a full bounded buffer faults, no silent loss.

    Single-threaded: the provider/subscription registry the scenario drives is
    process-wide, so parallel `#[test]` threads would clobber each other's
    `revl_stream_reset()`.
    """
    here = Path(__file__).resolve().parent
    ir = compile_files([str(_STREAM_SCENARIO)])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(ir), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_stream_scn"),
                                         encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "stream.rs").write_text(
        (here / "scenarios" / "stream.rs").read_text(encoding="utf-8"),
        encoding="utf-8")
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "11 passed" in result.stdout
