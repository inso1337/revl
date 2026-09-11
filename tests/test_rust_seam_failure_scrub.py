"""The rust tier's seam failure funnel (roadmap item 421 F5).

A forward crossing into a declared `Secret[T]` receiver authorises disclosure to
the receiver; it does not authorise the *error* channel to perform the reverse
crossing the checker refuses statically. Every other tier funnels a provider-side
failure before it leaves the process -- py `bridge.seam_failure`, ts
`REDACTED_ARG`, go `bridge.SeamFailure`, java `PlacementRunner.SeamFailure` --
and the rust tier had neither stage: `_bridge_ret_ser` serialized a `Result`'s
`Err` payload unchanged into the reply's value channel, which on this tier IS
the error channel (`handle_conn` always answers `{"ok": true, "value": ...}`).

The funnel lives in the GENERATED half rather than in the runner, and that is
the one structural difference from the go tier. Go installs its stage 2 as a
hook (`bridge.SecretScrub`) because the generated package already imports
`bridge` and the reverse would be a package cycle; the registry belongs to the
composition either way. Rust has no reflection, so the registry is a
process-global the emitted code populates and reads in-line, and the runner
holds no registry for a hook to reach. So both stages are emitted where the
registry already is.

Stage 2 on this tier is defence in depth rather than the primary trigger: the
checker refuses a declared `Secret[T]` reaching a provide-method return
(`G-SECRET-FLOW`), so a failure that quotes a registered credential has to have
laundered the marking through an unmodelled host body first. Stage 1 -- the
caller's own argument values crossing back inside a driver's message -- is the
reachable one, and it is what the live run below exercises.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

BACKEND = ROOT / "backends" / "rust"

# The registry and both funnels are a fixed part of the runner crate, not
# generated code (item 421 F6(c)): the runner's own console channels read them.
RUNNER_CONFIDENTIAL = BACKEND / "placement_runner" / "src" / "confidential.rs"
_RUNNER_CONFIDENTIAL = RUNNER_CONFIDENTIAL.read_text(encoding="utf-8")

CANARY = "SEKRIT-CANARY-421-F5F6"
ARG_CANARY = "USER-ARG-CANARY-421-F5F6"
REDACTED_ARG = "<redacted:arg>"
REDACTED_SECRET = "<redacted:secret>"

# A component holding a credential as a declared secret, whose provide method
# returns `Result[Str, Str]`: the provider's failure quotes the arguments the
# call was made with, the plain shape a driver error takes.
_SECRET_DOC = f'''
extern emission[vault.connect] fn connect(url: Str, user: Str) -> Result[Str, Str]
  = @rs {{
      if url.starts_with("bad") {{
          Err(format!("vault refused {{}} for {{}}", url, user))
      }} else {{
          Ok("opened".to_string())
      }}
  }}

service Vault {{ emission fn open(url: Str, user: Str) -> Result[Str, Str] }}

component Keeper provides vault: Vault {{
  config {{ url: Str = "pg://main", api_key: Secret[Str] = "{CANARY}" }}

  provide vault {{
    fn open(url, user) {{ return emit connect(url, user) }}
  }}
}}
'''

# The same document with the marking removed: the control that the funnel is
# emitted for a document that declares a `Secret[T]` and for no other.
_PLAIN_DOC = _SECRET_DOC.replace("api_key: Secret[Str]", "api_key: Str")

# The dispatch arm a `Result`-returning method had before the funnel existed.
# Byte-identical output for a marking-free document is the contract that keeps
# every golden and the selfhost mirror untouched.
_PLAIN_ARM = (
    '"open" => { match svc.open(args[0].as_str().unwrap_or("").to_string(), '
    'args[1].as_str().unwrap_or("").to_string()) { '
    'Ok(_v) => serde_json::json!({"$kind": "Ok", "$value": serde_json::to_value(&_v)'
    '.unwrap_or(serde_json::Value::Null)}), '
    'Err(_e) => serde_json::json!({"$kind": "Err", "$value": serde_json::to_value(&_e)'
    '.unwrap_or(serde_json::Value::Null)}) } }'
)


def _rust_emit():
    # A unique module name: a bare `import emit` collides with the other
    # backends' emitters when the suites run in one pytest invocation.
    spec = importlib.util.spec_from_file_location("revl_rust_emit_seam",
                                                  BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit(source: str) -> str:
    """A literal default on a `Secret[T]` field warns (it is source, so it is in
    the IR); the scenario needs one so the no-arg constructor door exists."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _rust_emit().emit(compile_source(source))


def _arm(code: str, method: str = "open") -> str:
    line = next(l for l in code.splitlines() if f'"{method}" =>' in l)
    return line.strip().rstrip(",")


# ---------------------------------------------------------------------------
# the emitted shape (runs everywhere; no toolchain needed)
# ---------------------------------------------------------------------------

def test_secret_mode_emits_the_funnel_and_the_shared_marker():
    code = _emit(_SECRET_DOC)
    # the funnel is a fixed part of the runner crate -- the runner's own console
    # channels read the same registry (item 421 F6(c)) -- so the generated half
    # imports it rather than carrying a second copy
    assert code.count("use crate::confidential::*;") == 1
    assert "pub fn revl_seam_failure" not in code
    assert f'pub const REVL_REDACTED_ARG: &str = "{REDACTED_ARG}";' in _RUNNER_CONFIDENTIAL
    assert _RUNNER_CONFIDENTIAL.count(
        "pub fn revl_seam_failure(text: String, args: &[serde_json::Value]) -> String {") == 1
    assert _RUNNER_CONFIDENTIAL.count(
        "pub fn revl_funnel_err_value(value: &mut serde_json::Value, args: &[serde_json::Value]) {") == 1
    # ...and it is the Err half the dispatch funnels, over the args the call was
    # made with -- both stages of the contract, in the contract's order.
    arm = _arm(code)
    assert "Err(_e) => { let mut _j = serde_json::to_value(&_e)" in arm
    assert "revl_funnel_err_value(&mut _j, args);" in arm
    assert "revl_redact_text(text)" in _RUNNER_CONFIDENTIAL


def test_the_ok_half_is_left_alone():
    """The Ok half is a value the caller asked for: a `Secret[T]` return crosses
    intact and the CONSUMER registers it, exactly as on the other tiers. Only
    the failure text is funnelled."""
    arm = _arm(_emit(_SECRET_DOC))
    ok_arm = arm.split("Ok(_v) =>")[1].split("Err(_e) =>")[0]
    assert "revl_funnel_err_value" not in ok_arm
    assert "revl_seam_failure" not in ok_arm


def test_a_marking_free_document_is_byte_identical():
    code = _emit(_PLAIN_DOC)
    assert "revl_seam_failure" not in code
    assert "revl_funnel_err_value" not in code
    assert "REVL_REDACTED_ARG" not in code
    assert _arm(code) == _PLAIN_ARM, _arm(code)


def test_the_arg_marker_is_the_one_the_other_tiers_produce():
    """A polyglot seam produces the SAME marker whichever tier answered, so the
    five tiers' constants are compared against each other rather than restated."""
    def declared(path: Path, pattern: str) -> str:
        found = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
        assert found, f"{path}: {pattern!r} not found"
        return found.group(1)

    assert REDACTED_ARG == declared(
        ROOT / "backends" / "python" / "confidential.py",
        r'^REDACTED_ARG = "([^"]*)"$')
    assert REDACTED_ARG == declared(
        ROOT / "backends" / "go" / "placement_runner" / "bridge" / "bridge.go",
        r'^const RedactedArg = "([^"]*)"$')
    assert REDACTED_ARG == declared(
        ROOT / "backends" / "java" / "placement" / "PlacementRunner.java",
        r'static final String REDACTED_ARG = "([^"]*)";')
    assert REDACTED_ARG == declared(
        ROOT / "backends" / "typescript" / "bridge.ts",
        r"^export const REDACTED_ARG = '([^']*)'$")
    assert REDACTED_ARG == declared(
        RUNNER_CONFIDENTIAL,
        r'^pub const REVL_REDACTED_ARG: &str = "([^"]*)";$')
    assert "use crate::confidential::*;" in _emit(_SECRET_DOC)


# ---------------------------------------------------------------------------
# run the emitted crate against the real dispatch
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

# One #[test], the sub-checks sequential: the registry is process-global, so
# running them on one thread is what keeps the shared state from racing itself.
_HARNESS = f'''
#[cfg(test)]
mod revl_seam_failure_tests {{
    use crate::{{
        connect, revl_forget_secrets, revl_funnel_err_value, revl_mark_secret,
        revl_seam_failure, KeeperVault, REVL_REDACTED_ARG, REVL_REDACTED_SECRET,
        _revl_dispatch_vault,
    }};

    const CANARY: &str = "{CANARY}";
    const ARG_CANARY: &str = "{ARG_CANARY}";
    const REDACTED_ARG: &str = "{REDACTED_ARG}";
    const REDACTED_SECRET: &str = "{REDACTED_SECRET}";

    #[test]
    fn a_failure_does_not_carry_the_call_s_arguments_back() {{
        revl_forget_secrets();
        let svc = KeeperVault {{}};
        let args = vec![serde_json::json!("bad://host"), serde_json::json!(ARG_CANARY)];

        // non-vacuity: the host body's own text DOES quote the argument, so the
        // assertions below are the funnel working and not an empty message.
        let raw = connect("bad://host".to_string(), ARG_CANARY.to_string()).unwrap_err();
        assert!(raw.contains(ARG_CANARY), "the raw failure is already clean: {{raw}}");

        let reply = _revl_dispatch_vault(&svc, "open", &args);
        assert_eq!(reply["$kind"], "Err", "{{reply}}");
        let text = reply["$value"].as_str().unwrap().to_string();
        assert!(!text.contains(ARG_CANARY), "argument leaked: {{text}}");
        assert!(text.contains(REDACTED_ARG), "no marker: {{text}}");
        // the sentence around it survives: the reply still says what went wrong
        assert!(text.contains("vault refused"), "over-redacted: {{text}}");

        // the Ok half is not funnelled: a caller's requested value is returned
        let ok = _revl_dispatch_vault(
            &svc, "open", &vec![serde_json::json!("good"), serde_json::json!(ARG_CANARY)]);
        assert_eq!(ok["$kind"], "Ok", "{{ok}}");
        assert_eq!(ok["$value"], serde_json::json!("opened"), "{{ok}}");
    }}

    #[test]
    fn stage_two_covers_a_credential_the_call_was_not_made_with() {{
        revl_forget_secrets();
        revl_mark_secret(&CANARY.to_string());
        let out = revl_seam_failure(
            format!("driver: connecting to pg://x failed, password {{}}", CANARY),
            &[serde_json::json!("alice")],
        );
        assert!(!out.contains(CANARY), "credential leaked: {{out}}");
        assert!(out.contains(REDACTED_SECRET), "no secret marker: {{out}}");
        // stage 1 ran too: the argument is the caller's own bytes
        assert!(!out.contains("alice"), "argument survived: {{out}}");
        assert!(out.contains("driver: connecting to"), "over-redacted: {{out}}");
    }}

    #[test]
    fn a_structured_error_value_is_walked_leaf_by_leaf() {{
        revl_forget_secrets();
        revl_mark_secret(&CANARY.to_string());
        let args = vec![serde_json::json!({{ "user": ARG_CANARY, "n": 8675309 }})];
        let mut value = serde_json::json!({{
            "message": format!("refused {{}}", ARG_CANARY),
            "nested": [format!("key={{}}", CANARY), "clean"],
        }});
        revl_funnel_err_value(&mut value, &args);
        let rendered = serde_json::to_string(&value).unwrap();
        assert!(!rendered.contains(ARG_CANARY), "argument leaked: {{rendered}}");
        assert!(!rendered.contains(CANARY), "credential leaked: {{rendered}}");
        // a record's KEYS are field names the author wrote, not caller data
        assert!(rendered.contains("\\"message\\""), "key removed: {{rendered}}");
        assert!(rendered.contains("clean"), "over-redacted: {{rendered}}");

        // an argument nested inside a record is still found by stage 1
        let out = revl_seam_failure(String::from("bad 8675309 for alice"), &args);
        assert!(!out.contains("8675309"), "nested number leaked: {{out}}");
        assert!(out.contains("for alice"), "over-redacted: {{out}}");
    }}
}}
'''


@needs_cargo
def test_a_real_failure_reaches_the_consumer_without_the_arguments(tmp_path):
    src = _emit(_SECRET_DOC)
    (tmp_path / "src").mkdir()
    # the generated module imports `crate::confidential`, so the crate root
    # declares it and the runner's real registry file is copied in beside it
    (tmp_path / "src" / "confidential.rs").write_text(
        _RUNNER_CONFIDENTIAL, encoding="utf-8")
    (tmp_path / "src" / "lib.rs").write_text(
        src + "\nmod confidential;\n" + _HARNESS, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        _rust_emit().cargo_toml("revl_seam_check"), encoding="utf-8")
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")
    assert "3 passed" in (result.stdout or "")
    # the funnel is what the run proves, so pin that the crate carries it
    assert "revl_funnel_err_value(&mut _j, args);" in src
