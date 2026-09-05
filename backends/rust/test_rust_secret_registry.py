"""The rust tier's declared `Secret[T]` registry (roadmap item 421 F6, the rust half).

Follow-up from the java fix (PR #391) on a second tier: py, ts, go and java
funnel runtime output through a secret registry so a held `Secret[T]` value
cannot appear verbatim in a trace, seam-failure string, or WAL record. The rust
tier carried only the EMIT-TIME `_REDACTED_SECRET` witness redaction; it had no
RUNTIME registry, so a driver error, a panic message, or any host string that
quotes a held credential reached this tier's trace verbatim.

rust (native) has no reflection, so there is nothing to bind the way java binds
its runners reflectively. The registry is instead a process-global the emitted
code populates directly at every declared end — `revl_mark_secret(&config.<f>)`
at the head of the plugin closure (the config field the operator supplied at
load, the one door every load goes through), at the head of a provide method
that declares a `Secret[T]` parameter (the receiver), and `revl_secret_result`
around an extern whose declared return was `Secret[T]` (the origin) — and every
free-form emitted-runtime sink reads through `revl_redact_text`: the ordered
host trace `revl_stream_record` gathers, and the WAL descriptor arguments under
`--record`. That is the same register-at-Load shape the go tier uses
(`hostRecord` scrubs at the one choke point), on rust's runtime.

What is proved here:

* the EMITTED SHAPE (runs everywhere): the plugin closure registers the secret
  config field at load and only that field; the registry preamble is present
  once with the shared marker; a secret-free document is byte-identical (no
  registry at all); the WAL descriptor reads through the registry under
  `--record`; a `secret_return` extern is wrapped at its origin;

* the RUNTIME BEHAVIOUR (needs a rust toolchain), by RUNNING the emitted crate
  under `cargo test` and grepping the real host trace `revl_stream_marks()`
  produced: a registered value quoted by a free-form host line appears nowhere
  in the trace, an ordinary value beside it is verbatim (no over-redaction),
  `revl_secret_result` hands its value back unchanged while registering it, and
  with the value NOT registered it flows through verbatim (non-vacuity).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Load this backend's emitter under a unique module name — a bare `import emit`
# collides with the other backends' emitters when the suites run in one pytest
# invocation.
_spec = importlib.util.spec_from_file_location("revl_rust_emit_secret", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)
from revl import compile_source  # noqa: E402

SCENARIO = (BACKEND / "scenarios" / "secret_registry.rvl").read_text()

# Long enough that an exact match means something, and not a substring of
# anything else the run prints.
CANARY = "SEKRIT-RUST-CANARY-421-F6"
# The ordinary value beside it: the control that the registry redacts what was
# declared and nothing else.
PUBLIC_URL = "pg://real-host-5432/app"
REDACTED_SECRET = "<redacted:secret>"


def _compile(source: str) -> dict:
    """A literal default on a `Secret[T]` field warns (it is source, so it is in
    the IR); the scenario needs one so the no-arg constructor door exists."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return compile_source(source)


# ---------------------------------------------------------------------------
# the emitted shape (runs everywhere; no toolchain needed)
# ---------------------------------------------------------------------------

def test_the_plugin_registers_the_secret_config_field_at_load():
    code = emit.emit(_compile(SCENARIO))
    # the declared field is registered at the head of the plugin closure
    assert "revl_mark_secret(&config.api_key);" in code, code
    # ...and only the declared field: the ordinary one beside it is not marked
    assert "revl_mark_secret(&config.url" not in code
    # the registry itself, once, with the shared marker
    assert code.count("pub fn revl_redact_text(text: String) -> String {") == 1
    assert 'pub const REVL_REDACTED_SECRET: &str = "<redacted:secret>";' in code


def test_a_secretless_document_is_byte_identical():
    """The registry is emitted only for a document that declares a `Secret[T]`,
    so every existing golden and the selfhost mirror stay untouched."""
    plain = SCENARIO.replace("api_key: Secret[Str]", "api_key: Str").replace(
        "key: Secret[Str]", "key: Str")
    code = emit.emit(_compile(plain))
    assert "revl_mark_secret" not in code
    assert "revl_redact_text" not in code
    assert "REVL_REDACTED_SECRET" not in code


def test_the_host_trace_choke_point_reads_through_the_registry():
    """`revl_stream_record` — the one choke point every stream host trace mark
    passes through, and the one that interpolates a free-form `emit`ted item —
    reads the mark through the registry in secret mode, and is byte-identical
    outside it."""
    code = emit.emit(_compile(SCENARIO))
    assert "fn revl_stream_record(mark: String) {" in code
    assert "    let mark = revl_redact_text(mark);" in code
    plain = SCENARIO.replace("api_key: Secret[Str]", "api_key: Str").replace(
        "key: Secret[Str]", "key: Str")
    assert "let mark = revl_redact_text(mark);" not in emit.emit(_compile(plain))


def test_the_wal_descriptor_reads_through_the_registry_under_record():
    """The WAL is a plaintext file at rest: under `--record` a descriptor
    argument is scrubbed before it is written. Emitted only in secret mode, on
    the witnessed composition the crash-recovery proof records."""
    fixture = BACKEND / "scenarios" / "crashproof" / "crashproof.ir.json"
    ir = json.loads(fixture.read_text())
    ir["components"][0].setdefault("config", []).append(
        {"name": "api_key", "type": "Str", "default": "k", "secret": True})
    code = emit.emit(ir, record=True)
    assert "let args: Vec<String> = args.into_iter().map(revl_redact_text).collect();" in code
    assert "revl_mark_secret(&config.api_key);" in code
    plain = emit.emit(json.loads(fixture.read_text()), record=True)
    assert "pub fn revl_record_transactional" in plain  # the WAL sink is present
    assert "map(revl_redact_text)" not in plain
    assert "revl_redact_text" not in plain


def test_a_secret_return_extern_is_wrapped_at_its_origin():
    """`taint.py` strips the qualifier before lowering, so `secret_return` is
    the only surviving record that an extern's declared return was `Secret[T]`.
    The public name registers what it returns and forwards to a private impl
    carrying the verbatim body, so no call site changes."""
    source = (
        'extern pure fn mint() -> Secret[Str] = @rs { String::from("tok") }\n'
        'extern emission fn sink(k: Secret[Str]) -> Unit = @rs { let _ = k; }\n'
        'service S { emission fn go() -> Str }\n'
        'component C provides s: S {\n'
        '  provide s { fn go() { let k = mint() emit sink(k) return "ok" } }\n'
        '}\n'
    )
    code = emit.emit(_compile(source))
    assert "revl_secret_result(_revl_secret_mint())" in code
    assert "fn _revl_secret_mint() -> String {" in code


def test_the_scenario_is_the_legitimate_use():
    """The composition compiles: handing a declared `Secret[T]` config field to
    the component's own host binding is not a disclosure crossing."""
    ir = _compile(SCENARIO)
    keeper = next(c for c in ir["components"] if c["name"] == "Keeper")
    assert [f["name"] for f in keeper["config"] if f.get("secret")] == ["api_key"]


# ---------------------------------------------------------------------------
# run the emitted crate, grep the real host trace
# ---------------------------------------------------------------------------

_OFFLINE_RESOLVE_MARKERS = (
    "no matching package named",
    "failed to load source for dependency",
    "unable to get packages from source",
    "cannot be found in registry",
)
_REAL_FAILURE_MARKERS = ("error[e", "test result: failed", "panicked at")


def _crates_io_reachable() -> bool:
    import socket
    try:
        socket.create_connection(("index.crates.io", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _is_offline_resolve_failure(proc: subprocess.CompletedProcess) -> bool:
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(m in blob for m in _REAL_FAILURE_MARKERS):
        return False
    return any(m in blob for m in _OFFLINE_RESOLVE_MARKERS)


def _cargo(subcommand: str, cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    """`cargo <subcommand>` — offline first, networked resolve as fallback.
    Mirrors backends/rust/test_emit_rust.py::_cargo."""
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra], cwd=cwd, text=True,
        capture_output=True, timeout=600,
    )
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline
    if not _crates_io_reachable():
        pytest.skip(
            "cordis-rs is not in the local cargo registry and index.crates.io "
            "is unreachable — run once with network to populate ~/.cargo"
        )
    return subprocess.run(
        ["cargo", subcommand, *extra], cwd=cwd, text=True,
        capture_output=True, timeout=600,
    )


needs_cargo = pytest.mark.skipif(
    shutil.which("cargo") is None, reason="cargo not installed"
)


# One #[test], every sub-check sequential: the secret registry and the stream
# host trace are BOTH process-global (a value one plugin's load registers must
# be scrubbed from a sink another component writes), so running the checks on
# one thread is what keeps the shared state from racing itself.
_HARNESS = f'''
#[cfg(test)]
mod revl_secret_registry_tests {{
    use crate::{{
        revl_forget_secrets, revl_mark_secret, revl_redact_text, revl_secret_result,
        revl_stream_marks, Stream,
    }};

    const CANARY: &str = "{CANARY}";
    const PUBLIC_URL: &str = "{PUBLIC_URL}";
    const REDACTED: &str = "{REDACTED_SECRET}";

    #[test]
    fn no_sink_carries_a_registered_secret() {{
        // 1. a value registered at load (revl_mark_secret) is scrubbed from the
        //    ordered host trace, and the ordinary value beside it survives.
        revl_forget_secrets();
        revl_mark_secret(&CANARY.to_string());
        let s = Stream::source();
        s.emit(format!("vault refused key {{}} at {{}} for alice", CANARY, PUBLIC_URL));
        let trace = revl_stream_marks().join("\\n");
        assert!(!trace.contains(CANARY), "leaked: {{trace}}");
        assert!(trace.contains(REDACTED), "no marker: {{trace}}");
        assert!(trace.contains(PUBLIC_URL), "over-redacted: {{trace}}");
        s.close();

        // 2. revl_secret_result (the extern origin end) hands its value back
        //    UNCHANGED and registers it, so a later sink scrubs it too.
        revl_forget_secrets();
        let minted = revl_secret_result(String::from("MINTED-TOKEN-XYZ"));
        assert_eq!(minted, "MINTED-TOKEN-XYZ");
        assert_eq!(
            revl_redact_text(String::from("issued tok=MINTED-TOKEN-XYZ")),
            "issued tok=<redacted:secret>"
        );

        // 3. non-vacuity: with nothing registered, the value flows verbatim, so
        //    the assertions above are the registry working, not an empty trace.
        revl_forget_secrets();
        let s2 = Stream::source();
        s2.emit(format!("key {{}}", CANARY));
        assert!(revl_stream_marks().join("\\n").contains(CANARY));
        s2.close();
    }}
}}
'''


@needs_cargo
def test_no_sink_carries_the_secret_when_run(tmp_path):
    src = emit.emit(_compile(SCENARIO))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(src + "\n" + _HARNESS, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")
