"""The rust runner's own console channels read the registry (item 421 F6(c)).

`revl_stream_record` is the emitted runtime's one choke point, and the emitted
sinks read the declared-`Secret[T]` registry through `revl_redact_text`. The
RUNNER writes channels of its own around that choke point — the `log` line every
load/serve/swap step goes through, the probe line that prints a served method's
returned value, and the boot-failure line — and those printers live in
`placement_runner/src/main.rs`, which the emitted module does not own. So a
registry carried only by the generated module left every one of them writing
verbatim: the value a plugin's load had just registered appeared in the console
on the probe channel and in any load failure's message.

The py, ts, go and java tiers all funnel their runner consoles (`log` ->
`redact_text` / `bridge.ScrubText` / `redactSecrets`); the rust runner was the
one tier that did not, which is why the registry now lives in the runner crate
(`confidential.rs`) and the generated module imports it. That placement is what
this suite pins:

* the SHAPE (runs everywhere): every runner printer renders through
  `confidential::revl_redact_text`, the panic hook included, and the crate
  declares the module;

* the SHARED REGISTRY (needs a rust toolchain): a value the EMITTED half
  registers is scrubbed by the very function the runner's printers call, so the
  two halves are one process-global rather than two — plus a non-vacuity arm
  showing the same value crossing verbatim once nothing is registered.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("revl_rust_emit_console", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)
from revl import compile_source  # noqa: E402

MAIN_RS = BACKEND / "placement_runner" / "src" / "main.rs"
RUNNER_CONFIDENTIAL = BACKEND / "placement_runner" / "src" / "confidential.rs"
SCENARIO = (BACKEND / "scenarios" / "secret_registry.rvl").read_text()

CANARY = "SEKRIT-RUST-CONSOLE-421-F6C"
PUBLIC_URL = "pg://real-host-5432/app"
REDACTED_SECRET = "<redacted:secret>"


def _main() -> str:
    return MAIN_RS.read_text()


def _compile(source: str) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return compile_source(source)


# ---------------------------------------------------------------------------
# the runner's printer shape (runs everywhere)
# ---------------------------------------------------------------------------

def test_the_runner_declares_the_registry_module():
    """The registry is a fixed part of the runner crate, so the runner compiles
    it — otherwise `crate::confidential` would not resolve for the generated
    module the runner links either."""
    assert "mod confidential;" in _main()
    assert RUNNER_CONFIDENTIAL.exists()
    assert "pub fn revl_redact_text(text: String) -> String {" in RUNNER_CONFIDENTIAL.read_text()


def test_the_log_channel_is_funnelled():
    """Every load/serve/swap step goes through this one closure, so the funnel
    sits on the whole rendered line rather than at each call site — the same
    choke-point shape the go tier's `log` keeps."""
    main = _main()
    assert "let log = |channel: &str, subject: &str, detail: &str| {" in main
    closure = main[main.index("let log = |channel: &str, subject: &str, detail: &str| {"):]
    closure = closure[:closure.index("};")]
    assert "confidential::revl_redact_text(format!(" in closure
    assert "{channel:<6}| {subject:<16}| {detail}" in closure


def test_the_probe_line_is_funnelled():
    """The probe prints the value a served method returned — the one thing on
    this channel that can hold a registered secret."""
    main = _main()
    probe = main[main.index("fn probe_plugin("):main.index("fn spawn_monitor(")]
    assert "confidential::revl_redact_text(format!(" in probe
    assert "-> {value}" in probe


def test_the_boot_failure_line_is_funnelled():
    """A load failure's message quotes the error a host body produced, which can
    carry a value the same load just registered."""
    main = _main()
    assert "confidential::revl_redact_text(format!(\n" in main
    assert "boot failed loading {cname}: {error}" in main


def test_the_uncaught_channel_is_funnelled():
    """A panic unwinding out of a host body prints from the runtime itself,
    before `log` runs and outside every funnel the emitted program has. The
    installed hook is the runner's, so it renders through the same funnel."""
    main = _main()
    assert "std::panic::set_hook(Box::new(move |info| {" in main
    hook = main[main.index("std::panic::set_hook(Box::new(move |info| {"):]
    hook = hook[:hook.index("}));")]
    assert "confidential::revl_redact_text(format!(" in hook
    assert "FATAL panic: {message}{at}" in hook


def test_no_runner_printer_writes_a_raw_value():
    """The pre-fix shapes, by their exact text: each printed a runtime value with
    no funnel in the way. None may come back."""
    main = _main()
    for raw in (
        'println!("[{name}] probe | {key}.{method}(...) -> {value}");',
        'eprintln!("[{name}] boot failed loading {cname}: {error}");',
        'println!("[{name}] {channel:<6}| {subject:<16}| {detail}");',
    ):
        assert raw not in main, raw


def test_the_emitted_half_imports_the_runner_registry():
    """One registry, one definition: in secret mode the generated module imports
    the runner's file instead of emitting a second copy that the runner's
    printers could not reach."""
    code = emit.emit(_compile(SCENARIO))
    assert code.count("use crate::confidential::*;") == 1, code
    assert "pub fn revl_redact_text" not in code
    assert "static REVL_SECRET_VALUES" not in code


# ---------------------------------------------------------------------------
# the shared registry, by running it
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

# The runner's printers call `confidential::revl_redact_text` on the rendered
# line; this harness calls exactly that, after registering through the EMITTED
# half's `revl_mark_secret`. One registry means the value the emitted half
# registered is the value the runner's funnel replaces.
_CONSOLE_HARNESS = f'''
#[cfg(test)]
mod revl_runner_console_tests {{
    use crate::{{revl_forget_secrets, revl_mark_secret}};
    use crate::confidential::revl_redact_text;

    const CANARY: &str = "{CANARY}";
    const PUBLIC_URL: &str = "{PUBLIC_URL}";
    const REDACTED: &str = "{REDACTED_SECRET}";

    #[test]
    fn the_runner_console_scrubs_what_the_emitted_half_registered() {{
        // 1. what the emitted half registers at load is what the runner's own
        //    printer replaces, on the exact function it calls.
        revl_forget_secrets();
        revl_mark_secret(&CANARY.to_string());
        let probe = revl_redact_text(format!(
            "[svc] probe | svc.fetch(...) -> {{{{\\"key\\": \\"{CANARY}\\"}}}} at {{}}",
            PUBLIC_URL
        ));
        assert!(!probe.contains(CANARY), "the probe channel leaked: {{probe}}");
        assert!(probe.contains(REDACTED), "no marker: {{probe}}");
        assert!(probe.contains(PUBLIC_URL), "over-redacted: {{probe}}");
        let load = revl_redact_text(format!("[svc] load  | C             | FAILED: {{CANARY}}"));
        assert!(!load.contains(CANARY), "the load channel leaked: {{load}}");
        assert!(load.contains(REDACTED), "no marker: {{load}}");

        // 2. non-vacuity: with nothing registered the same lines carry the value
        //    verbatim, so the assertions above are the registry working rather
        //    than a funnel that redacts everything.
        revl_forget_secrets();
        let bare = revl_redact_text(format!("[svc] probe | svc.fetch(...) -> {{CANARY}}"));
        assert!(bare.contains(CANARY), "over-redacted with nothing registered: {{bare}}");
    }}
}}
'''


@needs_cargo
def test_the_runner_reads_the_registry_the_emitted_half_registers(tmp_path):
    src = emit.emit(_compile(SCENARIO))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "confidential.rs").write_text(
        RUNNER_CONFIDENTIAL.read_text(), encoding="utf-8")
    (tmp_path / "src" / "lib.rs").write_text(
        src + "\nmod confidential;\n" + _CONSOLE_HARNESS, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = _cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "test result: ok" in (result.stdout or "")
