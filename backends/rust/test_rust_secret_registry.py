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

# The registry is NOT emitted: it is a fixed part of the runner crate, so the
# runner's own console channels (item 421 F6(c)) read the same process-global
# the emitted sinks do. The generated module imports it in secret mode.
RUNNER_CONFIDENTIAL = BACKEND / "placement_runner" / "src" / "confidential.rs"
CONFIDENTIAL = RUNNER_CONFIDENTIAL.read_text()
SECRET_IMPORT = "use crate::confidential::*;"

# Long enough that an exact match means something, and not a substring of
# anything else the run prints.
CANARY = "SEKRIT-RUST-CANARY-421-F6"
# item 421 F6(q): the KEY of a declared `Secret[Map[K, V]]`. A key the caller
# chose is as confidential as the value it maps to, so it has to register too.
MAP_KEY_CANARY = "SEKRIT-RUST-MAPKEY-421-F6Q"
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
    # the generated module imports the runner's registry, once, and does not
    # carry a second copy of it
    assert code.count(SECRET_IMPORT) == 1, code
    assert "pub fn revl_redact_text" not in code
    assert CONFIDENTIAL.count("pub fn revl_redact_text(text: String) -> String {") == 1
    assert 'pub const REVL_REDACTED_SECRET: &str = "<redacted:secret>";' in CONFIDENTIAL


def test_a_secretless_document_is_byte_identical():
    """The registry is imported only for a document that declares a `Secret[T]`,
    so every existing golden and the selfhost mirror stay untouched."""
    plain = SCENARIO.replace("api_key: Secret[Str]", "api_key: Str").replace(
        "key: Secret[Str]", "key: Str")
    code = emit.emit(_compile(plain))
    assert "revl_mark_secret" not in code
    assert "revl_redact_text" not in code
    assert "REVL_REDACTED_SECRET" not in code
    assert "confidential" not in code


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


def _write_crate(tmp_path: Path, src: str, harness: str,
                 *, confidential: str = CONFIDENTIAL) -> None:
    """Materialise the emitted module as a crate root.

    In secret mode the generated module imports `crate::confidential::*`, so the
    crate root declares that module and the runner's real registry file is
    copied in beside it — the same file the runner crate compiles, which is what
    makes a run here evidence that the two halves share one registry.
    """
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "confidential.rs").write_text(confidential, encoding="utf-8")
    (tmp_path / "src" / "lib.rs").write_text(
        src + "\nmod confidential;\n" + harness, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")


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
    _write_crate(tmp_path, src, _HARNESS)
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")


# ---------------------------------------------------------------------------
# item 421 F6(d): the ESCAPED faces of a registered value
# ---------------------------------------------------------------------------
#
# Both redaction stages match a value EXACTLY against text that has already
# been RENDERED. A value holding a `"`, a `\` or a control character comes back
# ESCAPED from every encoder this tier renders it with — `serde_json` on the
# wire and in the runner's reply, `{:?}` in the host's own diagnostics — so the
# raw bytes matched nothing and the value crossed verbatim, while the identical
# value without the quote was scrubbed everywhere.
#
# `revl_renderings` registers each encoder's body beside the raw value, and
# takes the body FROM the encoder rather than restating an escape table, so a
# face cannot drift from the encoder that writes it.

ESCAPED_CANARY = 'SEK"RIT\\RUST-CANARY-421-F6D'
# JSON and Rust agree on the escaping of these characters, so the literal the
# harness declares is the JSON rendering of the same value.
ESCAPED_CANARY_LITERAL = json.dumps(ESCAPED_CANARY)


def test_the_registry_registers_the_escaped_faces_too():
    code = emit.emit(_compile(SCENARIO))
    assert SECRET_IMPORT in code
    assert "fn revl_renderings(text: &str) -> Vec<String> {" in CONFIDENTIAL
    # the remember path registers what revl_renderings hands back, not the raw
    # string alone
    assert "for face in revl_renderings(&text) {" in CONFIDENTIAL
    # ...and each face comes from the encoder that writes it, so neither can
    # drift from the escape table the encoder actually applies
    assert "serde_json::to_string(text)" in CONFIDENTIAL
    assert 'format!("{:?}", text)' in CONFIDENTIAL


def test_the_bound_gates_the_raw_value_only():
    """`REVL_MIN_MARKABLE` is checked against the raw value, before any face is
    derived: an escape can only ever EXPAND, so a value that cleared the bound
    clears it in every escaped face too."""
    remember = CONFIDENTIAL[CONFIDENTIAL.index("fn revl_remember_secret(text: String) {"):]
    assert (remember.index("text.len() < REVL_MIN_MARKABLE")
            < remember.index("revl_renderings(&text)"))


def test_a_secretless_document_carries_no_renderings():
    plain = SCENARIO.replace("api_key: Secret[Str]", "api_key: Str").replace(
        "key: Secret[Str]", "key: Str")
    code = emit.emit(_compile(plain))
    assert "revl_renderings" not in code
    assert "revl_redact_text" not in code


# The `format!` braces are doubled for the f-string; the canary is declared from
# the JSON rendering of the value so the escape cannot be mistyped here.
_ESCAPED_HARNESS = f'''
#[cfg(test)]
mod revl_escaped_face_tests {{
    use crate::{{revl_forget_secrets, revl_mark_secret, revl_redact_text}};

    const CANARY: &str = {ESCAPED_CANARY_LITERAL};
    const REDACTED: &str = "{REDACTED_SECRET}";

    #[test]
    fn the_registry_covers_every_rendering_of_the_value() {{
        // The two faces the emitted registry derives, taken from the encoders
        // themselves so this test cannot drift from them either.
        let wire = serde_json::to_string(CANARY).unwrap();
        let wire_body = wire[1..wire.len() - 1].to_string();
        let debug = format!("{{:?}}", CANARY);
        let debug_body = debug[1..debug.len() - 1].to_string();
        // the premise: both encoders really do rewrite this value, so a raw
        // needle alone could not have matched either rendering
        assert_ne!(wire_body, CANARY, "the wire encoder did not escape it");
        assert_ne!(debug_body, CANARY, "the Debug encoder did not escape it");

        revl_forget_secrets();
        revl_mark_secret(&CANARY.to_string());

        // a host line that quoted the value the way the wire encoder renders it
        assert_eq!(
            revl_redact_text(format!("reply {{}}", wire_body)),
            format!("reply {{}}", REDACTED),
            "the wire rendering crossed"
        );
        // ...and the way the host's own diagnostics render it
        assert_eq!(
            revl_redact_text(format!("panic {{}}", debug_body)),
            format!("panic {{}}", REDACTED),
            "the Debug rendering crossed"
        );
        // the raw face still matches, so the fix is additive rather than a
        // replacement
        assert_eq!(
            revl_redact_text(format!("raw {{}}", CANARY)),
            format!("raw {{}}", REDACTED)
        );
    }}
}}
'''


@needs_cargo
def test_no_sink_carries_an_escaped_value_when_run(tmp_path):
    src = emit.emit(_compile(SCENARIO))
    _write_crate(tmp_path, src, _ESCAPED_HARNESS)
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")


@needs_cargo
def test_with_the_escaped_faces_stripped_the_value_leaks(tmp_path):
    """Non-vacuity: `revl_renderings` is what stands between the encoders and
    the sinks. Register the raw face alone — the pre-F6(d) shape — and the same
    crate shows the escaped rendering surviving redaction."""
    raw_only = CONFIDENTIAL.replace("for face in revl_renderings(&text) {",
                                    "for face in vec![text.clone()] {")
    assert raw_only != CONFIDENTIAL, "the registration no longer reads through revl_renderings"
    harness = _ESCAPED_HARNESS.replace(
        "assert_eq!(\n            revl_redact_text(format!(\"reply {}\", wire_body)),\n"
        "            format!(\"reply {}\", REDACTED),\n"
        '            "the wire rendering crossed"\n        );',
        "assert!(\n            revl_redact_text(format!(\"reply {}\", wire_body))"
        ".contains(&wire_body),\n"
        '            "the wire rendering was scrubbed: it should not have been"\n        );',
    ).replace(
        "assert_eq!(\n            revl_redact_text(format!(\"panic {}\", debug_body)),\n"
        "            format!(\"panic {}\", REDACTED),\n"
        '            "the Debug rendering crossed"\n        );',
        "assert!(\n            revl_redact_text(format!(\"panic {}\", debug_body))"
        ".contains(&debug_body),\n"
        '            "the Debug rendering was scrubbed: it should not have been"\n        );',
    )
    assert harness.count("should not have been") == 2, "the leak assertions were not substituted"
    _write_crate(tmp_path, raw_only, harness, confidential=raw_only)
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")


# ---------------------------------------------------------------------------
# item 421 F6(f): the declared `Secret[T]` types that are NOT scalars
# ---------------------------------------------------------------------------
#
# `revl_mark_secret` and `revl_secret_result` were generic over
# `std::fmt::Display`, which `String`, `i64`, `i32`, `f64` and `bool` implement
# and which `Vec<u8>` (what `Bytes` lowers to) and `Vec<String>` (`List[Str]`)
# do not. A composition declaring a non-scalar `Secret[T]` was accepted by the
# front end and emitted a crate that did not build at all, so the doors now
# read the declared type: a scalar keeps the `Display` door unchanged, `Bytes`
# gets the decoded/json/Debug trio the go tier registers for a byte slice
# (item 421 F6(e)), and every other shape gets the `Debug` form -- the one
# formatter every type a `Secret[T]` lowers to implements, cordis `Value`
# included. That form is the CONTAINER's face, so a shape the emit-time walk can
# decompose (item 421 F6(i), below) registers its leaves beside it.

_NON_SCALAR_SCENARIO = '''
extern emission[vault.connect] fn connect(key: Secret[Bytes]) -> Unit
  = @rs { let _ = key; }

service Vault { emission fn open(key: Secret[Bytes]) -> Str }
service Front { emission fn put(key: Secret[List[Str]]) -> Str }

component Keeper provides vault: Vault {
  config { url: Str = "pg://main", api_key: Secret[Bytes] }

  provide vault {
    fn open(key) {
      return "opened"
    }
  }
}

component Portal requires vault: Vault provides front: Front {
  provide front {
    fn put(key) {
      return "ok"
    }
  }
}
'''


def test_a_non_scalar_secret_takes_the_door_its_declared_type_needs():
    code = emit.emit(_compile(_NON_SCALAR_SCENARIO))
    # the config door, on a `Bytes` field
    assert "revl_mark_secret_bytes(&config.api_key);" in code, code
    # the receiver doors, one per declared shape
    assert "revl_mark_secret_bytes(&key); " in code, code
    assert "revl_mark_secret_encoded(&key); " in code, code
    # ...and the scalar doors are left exactly as they were, so every existing
    # golden and the selfhost mirror stay untouched. The doors themselves live
    # in the runner crate's `confidential.rs`, which the emitted module imports.
    assert "pub fn revl_mark_secret<T: std::fmt::Display>(value: &T) {" in CONFIDENTIAL
    assert "pub fn revl_secret_result<T: std::fmt::Display>(value: T) -> T {" in CONFIDENTIAL
    assert "pub fn revl_mark_secret_bytes(value: &[u8]) {" in CONFIDENTIAL
    assert "pub fn revl_mark_secret_encoded<T: std::fmt::Debug + ?Sized>(value: &T) {" in CONFIDENTIAL
    assert "use crate::confidential::*;" in code, code


def test_a_scalar_secret_still_takes_the_display_door():
    """The scenario's `Secret[Str]` is the shape the tier always handled; its
    doors must not have moved to the encoder pair."""
    code = emit.emit(_compile(SCENARIO))
    assert "revl_mark_secret(&config.api_key);" in code
    assert "revl_mark_secret_bytes(&config.api_key)" not in code
    assert "revl_mark_secret_encoded(&config.api_key)" not in code


_NON_SCALAR_HARNESS = f'''
#[cfg(test)]
mod revl_secret_non_scalar_tests {{
    use crate::{{
        revl_forget_secrets, revl_mark_secret_bytes, revl_mark_secret_encoded,
        revl_redact_text, revl_secret_result_bytes, revl_secret_result_encoded,
    }};

    const CANARY: &str = "{CANARY}";
    const REDACTED: &str = "{REDACTED_SECRET}";

    #[test]
    fn a_non_scalar_secret_is_scrubbed_on_every_face_it_is_written_with() {{
        let payload = CANARY.as_bytes().to_vec();
        let debug_face = format!("{{:?}}", payload);
        let json_face = format!("[{{}}]", payload.iter().map(|b| b.to_string())
            .collect::<Vec<_>>().join(","));

        // 1. Bytes: the byte door registers the decoded text, the json array the
        //    wire carries, and the Debug form -- the three a sink can write.
        revl_forget_secrets();
        revl_mark_secret_bytes(&payload);
        assert_eq!(revl_redact_text(CANARY.to_string()), REDACTED);
        assert_eq!(revl_redact_text(debug_face.clone()), REDACTED);
        assert_eq!(revl_redact_text(json_face.clone()), REDACTED);

        // 2. the origin end hands the value back unchanged, like the scalar door.
        revl_forget_secrets();
        let minted = revl_secret_result_bytes(payload.clone());
        assert_eq!(minted, payload);
        assert_eq!(revl_redact_text(CANARY.to_string()), REDACTED);

        // 3. a container with no Display registers through the Debug door. The
        //    face asserted here is the HELPER's -- the container's own -- so it
        //    is not the leaf coverage: the leaves are registered by the DOOR
        //    that reads the declared type (item 421 F6(i)), which is driven end
        //    to end in test_a_returned_containers_leaf_is_scrubbed_on_its_own.
        revl_forget_secrets();
        let list = vec![CANARY.to_string()];
        revl_mark_secret_encoded(&list);
        assert_eq!(revl_redact_text(format!("{{:?}}", list)), REDACTED);
        let minted = revl_secret_result_encoded(list.clone());
        assert_eq!(minted, list);

        // 4. non-vacuity: with nothing registered every face flows verbatim, so
        //    the assertions above are the registry working, not an empty read.
        revl_forget_secrets();
        assert!(revl_redact_text(CANARY.to_string()).contains(CANARY));
        assert_eq!(revl_redact_text(debug_face.clone()), debug_face);
        assert_eq!(revl_redact_text(json_face.clone()), json_face);
    }}
}}
'''


@needs_cargo
def test_a_non_scalar_secret_composition_builds_and_is_scrubbed(tmp_path):
    """The regression this pins is a BUILD one: before the doors read the
    declared type, this crate did not compile (`E0277: Vec<u8> doesn't
    implement std::fmt::Display`, at the config and receiver doors both), so the
    front end accepted a composition whose artifact was unusable."""
    src = emit.emit(_compile(_NON_SCALAR_SCENARIO))
    _write_crate(tmp_path, src, _NON_SCALAR_HARNESS)
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")


# item 421 F6(i). The origin door is emitted as a real top-level `fn`, so this is
# the one door a test can drive END TO END: the crate calls the emitted function
# (which registers) and then asks the registry to scrub ONE element of what came
# back. Before the fix only the container's `Debug` face was registered, so the
# element crossed verbatim -- the same gap F6(e) closed on the go tier with
# `revlRegisterValue`'s reflect walk, which rust never received because it has
# no reflection to walk with.
_LEAF_WALK_SCENARIO = f'''
extern pure fn leaves() -> Secret[List[Str]]
  = @rs {{ vec!["{CANARY}".to_string()] }}

extern pure fn table() -> Secret[Map[Str, Str]]
  = @rs {{ let mut m = std::collections::HashMap::new(); m.insert("{MAP_KEY_CANARY}".to_string(), "{CANARY}".to_string()); m }}

extern pure fn maybe() -> Secret[Opt[Str]]
  = @rs {{ Some("{CANARY}".to_string()) }}
'''


_LEAF_WALK_HARNESS = f'''
#[cfg(test)]
mod revl_secret_leaf_walk_tests {{
    use crate::{{revl_forget_secrets, revl_redact_text}};

    const CANARY: &str = "{CANARY}";
    const MAP_KEY_CANARY: &str = "{MAP_KEY_CANARY}";
    const REDACTED: &str = "{REDACTED_SECRET}";

    #[test]
    fn a_returned_containers_leaf_is_scrubbed_on_its_own() {{
        // non-vacuity: with nothing registered the leaf flows verbatim, so the
        // assertions below are the registry working, not an empty read.
        revl_forget_secrets();
        assert!(revl_redact_text(CANARY.to_string()).contains(CANARY));

        revl_forget_secrets();
        let list = crate::leaves();
        assert_eq!(list, vec![CANARY.to_string()]);
        assert_eq!(revl_redact_text(format!("{{:?}}", list)), REDACTED);
        assert_eq!(revl_redact_text(CANARY.to_string()), REDACTED,
                   "a List leaf crossed verbatim");

        revl_forget_secrets();
        let _map = crate::table();
        assert_eq!(revl_redact_text(CANARY.to_string()), REDACTED,
                   "a Map VALUE leaf crossed verbatim");
        // item 421 F6(q): the KEY is the caller's data as well.
        assert_eq!(revl_redact_text(MAP_KEY_CANARY.to_string()), REDACTED,
                   "a Map KEY leaf crossed verbatim");

        revl_forget_secrets();
        let opt = crate::maybe();
        assert_eq!(opt, Some(CANARY.to_string()));
        assert_eq!(revl_redact_text(CANARY.to_string()), REDACTED,
                   "an Opt leaf crossed verbatim");
    }}
}}
'''


def test_the_container_door_emits_a_walk_over_every_reachable_leaf():
    """The EMITTED SHAPE, which runs everywhere: a container-shaped door
    registers the container's own face and then one registration per reachable
    leaf, each through the door that leaf's declared type takes. The receiver
    and config doors are inline in a plugin closure (not callable from a test),
    so their walk is pinned here and the origin door's is proved by RUNNING it
    in `test_a_returned_containers_leaf_is_scrubbed_on_its_own`."""
    code = emit.emit(_compile(_NON_SCALAR_SCENARIO))
    # the receiver door: the container face, then the walk, on one line
    assert ("revl_mark_secret_encoded(&key); for _revl_leaf0 in key.iter() "
            "{ revl_mark_secret(&_revl_leaf0); }") in code, code
    # a `Bytes` leaf takes the byte door, not the scalar one
    assert "revl_mark_secret_bytes(&config.api_key);" in code, code

    walk = emit.emit(_compile(_LEAF_WALK_SCENARIO))
    flat = " ".join(walk.split())
    assert "let _revl_v = _revl_secret_leaves();" in walk, walk
    assert 'revl_remember_secret(format!("{:?}", _revl_v));' in walk, walk
    assert ("for _revl_leaf0 in _revl_v.iter() "
            "{ revl_mark_secret(&_revl_leaf0); }") in flat, walk
    assert "for _revl_leaf0 in _revl_v.values() {" in flat
    # item 421 F6(q): a `Map`'s keys are the caller's data too, so both legs of
    # the map walk register. Only the values leg used to.
    assert "for _revl_leaf0 in _revl_v.keys() {" in flat, walk
    assert "if let Some(_revl_leaf0) = _revl_v.as_ref() {" in flat, walk
    # the scalar doors are untouched, so every existing golden stays as it was.
    assert "pub fn revl_mark_secret_encoded<T: std::fmt::Debug + ?Sized>(value: &T) {" in CONFIDENTIAL
    assert "pub fn revl_secret_result_encoded<T: std::fmt::Debug>(value: T) -> T {" in CONFIDENTIAL


@needs_cargo
def test_a_returned_containers_leaf_is_scrubbed_on_its_own(tmp_path):
    src = emit.emit(_compile(_LEAF_WALK_SCENARIO))
    _write_crate(tmp_path, src, _LEAF_WALK_HARNESS)
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")


# item 421 F6(j). revl admits recursive datatypes, and the emit-time walk of
# F6(i) recurses the DECLARED type, so a cyclic record has no base case:
# `Node = { val: Str, next: Opt[Node] }` walks Node, Opt[Node], Node, ... and
# never returns. The front end accepts the program and the emitter then dies on
# it, so this is a BUILD defect on legal source -- the same class as F6(g), not
# a disclosure. The walk stops on re-entry of a declared type already on its own
# path (item 421 F6(o)), so it terminates WITHOUT dropping a leaf: the cycle is
# cut where the shape repeats itself and every other subtree is still walked to
# its leaves. The go tier bounds the identical shape at runtime instead, because
# there the cycle arrives as a VALUE.
_RECURSIVE_SCENARIO = '''
type Node = { val: Str, next: Opt[Node] }

extern pure fn chain() -> Secret[Node]
  = @rs { Node { val: "x".to_string(), next: None } }
'''


def test_the_leaf_walk_terminates_on_a_recursive_record():
    """RED before the path bound: `RecursionError` out of `_secret_face_lines`.
    The walk must terminate AND must not swallow the non-recursive leaf, so the
    bound cuts the cycle at the repeated declared type rather than abandoning
    the shape."""
    types = {"Node": {"kind": "record",
                      "fields": {"val": "Str", "next": "Opt[Node]"}}}
    lines = emit._secret_face_lines("Node", "v", types)
    assert lines, "the bound must not abandon a shape it can still decompose"
    assert any("v.val" in line for line in lines), lines
    # bounded: re-entry of `Node` stops the walk instead of running to the
    # recursion limit
    assert len(lines) < 100, len(lines)


def test_a_recursive_record_composition_emits():
    """The build defect, from the front door: a composition declaring a
    recursive record reached `emit` and killed it. Emission must now complete,
    and the walk it produces must be bounded."""
    code = emit.emit(_compile(_RECURSIVE_SCENARIO))
    assert "use crate::confidential::*;" in code, code
    # the path bound cuts the walk at the repeated declared type rather than
    # abandoning the shape: the reachable leaves are registered, and the
    # recursion stops instead of running away.
    assert "revl_mark_secret(&_revl_v.val);" in code, code
    assert code.count("revl_mark_secret(&") < 100, code.count("revl_mark_secret(&")


# item 421 F6(o). The walk of F6(i) used to stop at `levels > 8`, and a bound on
# how DEEP the walk goes is a confidentiality regression in exactly the walk
# that exists to prevent one: it terminates the recursion, but it also stops
# REGISTERING, so the leaves of any declared shape nested deeper than the bound
# were never registered as their own text and crossed verbatim in every sink
# `revl_redact_text` covers. The bound is now the PATH of declared types the
# walk is on, so a shape stops only where it re-enters itself (F6(j)) and every
# legal shape's leaves register at any depth. The go tier carried the same
# defect, reached at four nested maps rather than nine (F6(n)).
def _deep_map_type(depth: int) -> str:
    declared = "Str"
    for _ in range(depth):
        declared = f"Map[Str, {declared}]"
    return declared


def _deep_map_literal(depth: int) -> str:
    literal = f'"{CANARY}".to_string()'
    for _ in range(depth):
        literal = ('{ let mut m = std::collections::HashMap::new(); '
                   f'm.insert("k".to_string(), {literal}); m }}')
    return literal


def _deep_scenario(depth: int) -> str:
    return (f"extern pure fn deep() -> Secret[{_deep_map_type(depth)}]\n"
            f"  = @rs {{ {_deep_map_literal(depth)} }}\n")


_DEEP_HARNESS = f'''
#[cfg(test)]
mod revl_deep_leaf_tests {{
    use crate::{{revl_forget_secrets, revl_redact_text}};

    const CANARY: &str = "{CANARY}";
    const REDACTED: &str = "{REDACTED_SECRET}";

    #[test]
    fn a_leaf_past_the_old_bound_is_still_scrubbed() {{
        // non-vacuity: with nothing registered the leaf flows verbatim, so the
        // assertion below is the registry working, not an empty read.
        revl_forget_secrets();
        assert!(revl_redact_text(CANARY.to_string()).contains(CANARY));

        revl_forget_secrets();
        let _v = crate::deep();
        assert_eq!(revl_redact_text(CANARY.to_string()), REDACTED,
                   "a leaf past the walk's bound crossed verbatim");
    }}
}}
'''


@pytest.mark.parametrize("depth", [1, 8, 9, 12])
def test_the_walk_reaches_the_leaf_at_any_declared_depth(depth):
    """The EMITTED SHAPE, which runs everywhere: the walk descends the declared
    type to its leaf however deep that leaf is, and registers exactly one leaf
    per declared shape. A bound on how deep the walk goes shows up here as a
    missing `revl_mark_secret` for the innermost element, which is what made
    this a confidentiality regression rather than a missing optimisation."""
    code = emit.emit(_compile(_deep_scenario(depth)))
    leaf = f"_revl_leaf{depth - 1}"
    assert f"revl_mark_secret(&{leaf});" in code, code
    # one registration per level's KEY, plus the innermost VALUE leaf
    assert code.count("revl_mark_secret(&") == depth + 1, code


def test_with_a_bound_on_how_deep_the_walk_goes_the_leaf_leaks(monkeypatch):
    """Non-vacuity: the emitted-shape test only bites if a depth bound really
    does drop the leaf. Wrapping the walk with the old `levels > 8` rule makes
    the innermost registration disappear, which is the regression this pins."""
    real = emit._secret_face_lines
    calls = {"n": 0}

    def depth_bounded(declared, expr, types, depth=0, path=frozenset()):
        calls["n"] += 1
        if depth > 8:
            return []
        return real(declared, expr, types, depth, path)

    monkeypatch.setattr(emit, "_secret_face_lines", depth_bounded)
    bounded = emit._secret_face_lines(_deep_map_type(12), "v", {})
    assert not any("_revl_leaf11" in line for line in bounded), (
        "a depth bound must drop the deep leaf, or the emitted-shape test is "
        "vacuous")
    assert bounded, "the shallow keys register whatever the bound is"
    assert calls["n"] > 1, calls

    monkeypatch.undo()
    walk = emit._secret_face_lines(_deep_map_type(12), "v", {})
    assert any("revl_mark_secret(&_revl_leaf11);" in line for line in walk), walk


@needs_cargo
@pytest.mark.parametrize("depth", [9, 12])
def test_a_leaf_past_the_old_bound_is_still_scrubbed(tmp_path, depth):
    """RED before the path bound, in a real crate: the emitted function returns a
    declared shape 9 (and 12) levels deep, and the leaf at the bottom crossed
    verbatim because the walk stopped registering before it got there."""
    src = emit.emit(_compile(_deep_scenario(depth)))
    _write_crate(tmp_path, src, _DEEP_HARNESS)
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")


def test_two_sibling_fields_of_one_type_are_both_walked():
    """The bound is per-branch, not a global visited set: two fields of the same
    declared type are two distinct values and both must register. A shared
    `seen` set would terminate the recursion but silently drop the second."""
    types = {"Pair": {"kind": "record",
                      "fields": {"a": "List[Str]", "b": "List[Str]"}}}
    lines = emit._secret_face_lines("Pair", "v", types)
    assert sum("revl_mark_secret(&" in line for line in lines) == 2, lines
    assert any("v.a.iter()" in line for line in lines), lines
    assert any("v.b.iter()" in line for line in lines), lines


_VARIANT_SCENARIO = f'''
type Step = Final(Str) | Retry
type Box2 = Wrap(List[Str]) | Empty
type Sign = Neg | Zero | Pos
type Holder = {{ tag: Str, step: Step }}

extern pure fn step() -> Secret[Step]
  = @rs {{ Step::Final("{CANARY}".to_string()) }}

extern pure fn boxed() -> Secret[Box2]
  = @rs {{ Box2::Wrap(vec!["{CANARY}".to_string()]) }}

extern pure fn holder() -> Secret[Holder]
  = @rs {{ Holder {{ tag: "t".to_string(), step: Step::Final("{CANARY}".to_string()) }} }}
'''


_VARIANT_HARNESS = f'''
#[cfg(test)]
mod revl_variant_leaf_tests {{
    use crate::{{revl_forget_secrets, revl_redact_text, Step}};

    const CANARY: &str = "{CANARY}";
    const REDACTED: &str = "{REDACTED_SECRET}";

    #[test]
    fn a_case_payload_is_scrubbed_on_its_own() {{
        // non-vacuity: with nothing registered the payload flows verbatim
        revl_forget_secrets();
        assert!(revl_redact_text(CANARY.to_string()).contains(CANARY));

        revl_forget_secrets();
        let s = crate::step();
        assert_eq!(revl_redact_text(format!("{{:?}}", s)), REDACTED,
                   "the container face is not registered");
        let Step::Final(v) = s else {{ panic!("expected Final") }};
        assert_eq!(revl_redact_text(v), REDACTED,
                   "a variant case payload crossed verbatim");
    }}
}}
'''


def test_the_walk_decomposes_a_variant_by_its_cases():
    """The EMITTED SHAPE, which runs everywhere: a declared variant is walked
    through a `match` on the value, one arm per case that carries a payload, and
    the `_` arm keeps the match exhaustive over the nullary cases and the
    re-entrant ones `path` cuts. A variant the walk skips shows up here as the
    absent `match` -- and then the container's own `Debug` face is the whole
    coverage, which is F6(i)'s gap on one more declared shape."""
    code = emit.emit(_compile(_VARIANT_SCENARIO))
    assert "match &_revl_v {" in code, code
    assert "Step::Final(_revl_leaf0) => {" in code, code
    assert "revl_mark_secret(&_revl_leaf0);" in code, code
    # a case payload that is itself a container is walked, not registered whole
    assert "Box2::Wrap(_revl_leaf0) => {" in code, code
    assert "for _revl_leaf2 in _revl_leaf0.iter() {" in code, code
    assert "revl_mark_secret(&_revl_leaf2);" in code, code
    # nullary cases fall to the catch-all rather than a `=> {}` arm of their own
    assert "Sign::Zero" not in code, code
    assert "_ => {}" in code, code


def test_the_variant_arm_is_taken_through_a_record_field_too():
    """A variant reached through a record field is the same walk one level in:
    the field is walked, and the variant behind it is matched on the field
    expression. This is the shape an ADT-typed member of a record takes."""
    code = emit.emit(_compile(_VARIANT_SCENARIO))
    assert "revl_mark_secret(&_revl_v.tag);" in code, code
    assert "match &_revl_v.step {" in code, code


def test_a_variant_with_no_payload_has_no_leaf_to_register():
    """Non-vacuity for the case arm: a variant whose cases carry nothing has no
    leaf, so the walk must return `[]` and leave the caller's container face as
    the whole coverage. A walk that emitted an arm per case regardless would
    register nothing here and still pass the emitted-shape test above."""
    nullary = {"Sign": {"kind": "variant",
                        "cases": [{"name": "Neg", "payload": None},
                                  {"name": "Zero", "payload": None}]}}
    assert emit._secret_face_lines("Sign", "v", nullary) == []

    payloaded = {"Step": {"kind": "variant",
                          "cases": [{"name": "Final", "payload": "Str"}]}}
    walk = emit._secret_face_lines("Step", "v", payloaded)
    assert walk == ["match &v {",
                    "    Step::Final(_revl_leaf0) => {",
                    "        revl_mark_secret(&_revl_leaf0);",
                    "    }",
                    "    _ => {}",
                    "}"], walk


@needs_cargo
def test_a_variant_case_payload_is_still_scrubbed(tmp_path):
    """RED before the case arm, in a real crate: the emitted function returns a
    `Secret[Step]`, so the container's own `Debug` face is registered and a trace
    that prints the whole value is scrubbed -- while the payload an author
    matches OUT of it is a different string and crossed verbatim."""
    src = emit.emit(_compile(_VARIANT_SCENARIO))
    _write_crate(tmp_path, src, _VARIANT_HARNESS)
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")
