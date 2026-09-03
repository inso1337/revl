"""`revl doctor` — per-tier install / toolchain / runtime diagnosis + smoke test
(roadmap item 291).

This project fans out over six backends, several external runtimes, and a
handful of optional dependencies. Any one of them can be absent or subtly wrong
(a too-old Node, a JDK the emitter's `--release 21` rejects, a cordis runtime
that never got installed), and until now the only way to find out was to run the
tier and read the traceback. `revl doctor` answers the whole question in one
command: for every tier and dependency it reports an OK / WARN / MISSING status
with the detected version, then runs a tiny end-to-end smoke test on each tier
that is actually available.

Two rules, the same honesty the runtime drivers already keep (see
:mod:`revl.run_ts` and friends):

  * **Never claim a tool is present without probing it.** Every status comes
    from actually launching the tool (`--version`), resolving an import, or
    checking a path. A probe that raises is a WARN/MISSING with the reason, not
    a crash.
  * **Absence is normal.** A missing optional tool is a WARN, never an error.
    `revl doctor` prints the whole report and exits 0 even when several tiers
    are missing, because reporting the gaps is the entire job.

All probing goes through a small :class:`Prober` seam so the report is testable
without depending on which tools happen to be installed on the test box
(tests/test_doctor.py injects a deterministic fake).
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ statuses

OK = "ok"
WARN = "warn"
MISSING = "missing"

# Smoke-test outcomes, kept distinct from the status vocabulary above: a smoke
# test either ran (pass/fail) or never ran because the tier's toolchain is not
# here (skipped). A skip is never a failure — the roadmap is explicit about that.
PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"

# The default timeout for a single probe (`--version` and friends answer in
# milliseconds; the ceiling only guards a hung binary).
PROBE_TIMEOUT = 30

# A smoke test compiles and boots a real composition, which can mean a first
# build for a compiled tier, so it gets a much larger ceiling than a version
# probe. A tier that blows past it is reported as a timed-out smoke, not a hang.
SMOKE_TIMEOUT = 90

# The version facts the emitters pin, mirrored here (with their source named) so
# doctor stays decoupled from the runtime drivers and cheap to import — it must
# not drag in the whole run stack just to say what Node it wants.
MIN_NODE = (23, 6)   # revl.run_ts._MIN_NODE — native .ts type-stripping
JAVA_RELEASE = 21    # revl.run_java.JAVAC_RELEASE — emitter lowers `match` to 21

# The trivial, self-contained composition every tier's smoke test boots. It is
# deliberately the *least common denominator* across all six emitters so that a
# smoke FAIL means the tier is broken, never that the program was too fancy:
#
#   * no config block, host builtin, or method-time effect (the wasm tier is the
#     strictest emitter and rejects all three);
#   * Int-only service methods, pure bodies, no Opt/Result construction (the go
#     emitter only builds those in return position);
#   * the service and the component are named apart (a shared name collides in
#     the go emitter's generated types).
#
# What remains is a pure provider that loads, provides, tears down, and proves no
# residue on every tier.
SMOKE_PROGRAM = """\
service Calc {
  fn dbl(n: Int) -> Int
}
component Doubler provides calc: Calc {
  provide calc {
    fn dbl(n) = n + n
  }
}
"""


# ------------------------------------------------------------------- results


@dataclass
class Check:
    """One diagnosed item: its status, the version we detected (if any), and a
    one-line reason. `tier`, when set, links the check to a runnable backend so
    the smoke runner knows whether that tier is available."""

    name: str
    status: str
    version: str | None = None
    detail: str = ""
    tier: str | None = None
    available: bool = False


@dataclass
class Smoke:
    """The outcome of one tier's end-to-end smoke test."""

    tier: str
    outcome: str
    detail: str = ""


@dataclass
class Report:
    revl_version: str
    checks: list[Check] = field(default_factory=list)
    smoke: list[Smoke] = field(default_factory=list)


# -------------------------------------------------------------------- prober


@dataclass
class ProbeResult:
    """The outcome of launching one external command. `found` is False when the
    executable could not be launched at all (not on PATH, or an OSError); a
    launched-but-erroring tool has `found=True` and a nonzero `returncode`."""

    found: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.found and not self.timed_out and self.returncode == 0

    @property
    def text(self) -> str:
        # Version banners land on stdout or stderr depending on the tool
        # (`java -version` writes to stderr); callers want whichever spoke.
        return (self.stdout or self.stderr or "").strip()


class Prober:
    """The real probing seam: run a command, resolve an import, test a path.

    Every method is trivial and side-effect-light on purpose — a test injects a
    fake with the same surface (tests/test_doctor.py) so the whole report can be
    asserted deterministically, independent of the host's installed tools."""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str], timeout: int = PROBE_TIMEOUT,
            stdin: str = "") -> ProbeResult:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  input=stdin, timeout=timeout, check=False)
        except (OSError, ValueError):
            return ProbeResult(found=False)
        except subprocess.TimeoutExpired:
            return ProbeResult(found=True, timed_out=True)
        return ProbeResult(found=True, returncode=proc.returncode,
                           stdout=proc.stdout or "", stderr=proc.stderr or "")

    def module_available(self, name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):  # a broken install resolves to absent
            return False

    def is_dir(self, path: Path) -> bool:
        try:
            return Path(path).is_dir()
        except OSError:
            return False


# ------------------------------------------------------------ small helpers


def _first_version(text: str) -> str | None:
    """The first dotted version token in a banner, e.g. `1.85.1` from
    `cargo 1.85.1 (...)` or `26.7.0` from `v26.7.0`."""
    match = re.search(r"(\d+(?:\.\d+)+)", text)
    return match.group(1) if match else None


def _major_minor(version: str | None) -> tuple[int, int] | None:
    if not version:
        return None
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None


def revl_version() -> str:
    """The installed revl version, or a best-effort read from the source tree
    when running uninstalled (no metadata)."""
    try:
        from importlib.metadata import version  # noqa: PLC0415
        return version("revl")
    except Exception:  # noqa: BLE001 — any metadata failure falls back
        pass
    # uninstalled src checkout: read the version out of pyproject.toml if we can
    root = Path(__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                match = re.search(r'"([^"]+)"', stripped)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return "unknown"


# ------------------------------------------------------------------- checks
#
# Each check probes one item and returns a Check. None of them raise: a probe
# that cannot answer is a MISSING/WARN with the reason, so the report never
# crashes on a broken tool.


def check_compiler(prober: Prober, version: str) -> Check:
    """The revl compiler itself — the one thing that is definitionally present,
    since it is the code answering. Reported so the version is on the record."""
    return Check("compiler (revl)", OK, version, "the running compiler")


def check_python(prober: Prober) -> Check:
    """The python backend: the interpreter running doctor. Always OK — it is the
    host. (cordis-py, the py runtime, is a separate line below.)"""
    version = ".".join(str(n) for n in sys.version_info[:3])
    return Check("python backend", OK, version, sys.executable,
                 tier="py", available=True)


def _tool_check(prober: Prober, name: str, argv: list[str],
                tier: str | None = None,
                missing_hint: str = "") -> tuple[Check, ProbeResult]:
    """Shared skeleton for a `<tool> --version`-style probe. Returns a Check
    plus the raw ProbeResult so a caller can layer a version-floor test on top
    (Node, Java). MISSING when the tool is absent or does not answer."""
    exe = argv[0]
    if prober.which(exe) is None:
        hint = f" — {missing_hint}" if missing_hint else ""
        return Check(name, MISSING, None, f"{exe} not on PATH{hint}", tier), \
            ProbeResult(found=False)
    result = prober.run(argv)
    if result.timed_out:
        return Check(name, WARN, None, f"{exe} did not answer in time", tier), result
    if not result.ok:
        return Check(name, MISSING, None,
                     f"{exe} on PATH but `{' '.join(argv[1:]) or exe}` failed", tier), \
            result
    return Check(name, OK, _first_version(result.text), result.text.splitlines()[0]
                 if result.text else "", tier, available=True), result


def check_typescript(prober: Prober) -> Check:
    """The ts backend: Node, and a version floor. Node older than 23.6 cannot
    strip `.ts` types natively, so the runner would fail on its first import;
    that is a WARN (present but too old), distinct from MISSING."""
    check, result = _tool_check(
        prober, "typescript backend (node)", ["node", "--version"], tier="ts",
        missing_hint=f"install Node >= {MIN_NODE[0]}.{MIN_NODE[1]}")
    if check.status is not OK:
        return check
    version = _major_minor(check.version)
    if version is not None and version < MIN_NODE:
        return Check(check.name, WARN, check.version,
                     f"Node {check.version} is too old — needs "
                     f">= {MIN_NODE[0]}.{MIN_NODE[1]} for native .ts stripping",
                     tier="ts", available=False)
    check.detail = f"node {check.version}; runner placement_runner.ts"
    return check


def check_rust(prober: Prober) -> Check:
    """The rust backend: cargo (the build/resolve driver) and, for the banner,
    rustc. cargo present is the availability gate; cordis-rs resolution is left
    to the smoke test so doctor stays fast (no `cargo generate-lockfile` here)."""
    check, _ = _tool_check(
        prober, "rust backend (cargo)", ["cargo", "--version"], tier="rust",
        missing_hint="install a Rust toolchain (https://rustup.rs)")
    if check.status is OK:
        rustc = prober.run(["rustc", "--version"])
        if rustc.ok:
            check.detail = f"cargo {check.version}; {rustc.text.splitlines()[0]}"
    return check


def check_java(prober: Prober) -> Check:
    """The java backend: a working JDK whose javac accepts the emitter's release.

    macOS ships a `javac` shim that errors until a real JDK is installed, so
    being on PATH is not enough — the probe must see it answer. A JDK older than
    the emitter's target (`--release 21`) is present-but-unusable: a WARN naming
    the required version, exactly the JDK-mismatch case this project has hit."""
    javac = _resolve_javac(prober)
    if javac is None:
        return Check("java backend (JDK)", MISSING, None,
                     "no working JDK found (javac that answers -version); "
                     f"install a JDK >= {JAVA_RELEASE}", tier="java")
    result = prober.run([javac, "-version"])
    version = _first_version(result.text)
    major = _major_minor(version)
    if major is not None and major[0] < JAVA_RELEASE:
        return Check("java backend (JDK)", WARN, version,
                     f"JDK {version} is too old — the emitter needs "
                     f">= {JAVA_RELEASE} (found {major[0]})", tier="java")
    return Check("java backend (JDK)", OK, version,
                 f"{result.text.splitlines()[0] if result.text else javac}",
                 tier="java", available=True)


def _resolve_javac(prober: Prober) -> str | None:
    """A javac that actually answers `-version`, preferring an explicitly
    configured JDK, then PATH. A lightweight echo of revl.run_java's resolver:
    doctor only needs *a* working javac to read a version off, not the full
    candidate sweep the runner does."""
    import os  # noqa: PLC0415 — only doctor's java path needs it
    candidates: list[str] = []
    for env in ("JAVA21_HOME", "JAVA_HOME"):
        home = os.environ.get(env)
        if home:
            candidates.append(str(Path(home) / "bin" / "javac"))
    on_path = prober.which("javac")
    if on_path:
        candidates.append(on_path)
    for javac in candidates:
        result = prober.run([javac, "-version"])
        if result.ok:
            return javac
    return None


def check_go(prober: Prober) -> Check:
    """The go backend. `go` reports its version via the `go version` subcommand,
    not a `--version` flag, so the probe uses that form."""
    return _tool_check(
        prober, "go backend", ["go", "version"], tier="go",
        missing_hint="install Go (https://go.dev/dl)")[0]


def check_wasm(prober: Prober) -> Check:
    """The wasm backend: wasmtime (the substrate), plus the Component Model
    tooling (wasm-tools). wasm-tools absent is a WARN on an otherwise-OK tier —
    the module runs, only the component tooling is missing."""
    check, _ = _tool_check(
        prober, "wasm backend (wasmtime)", ["wasmtime", "--version"], tier="wasm",
        missing_hint="install wasmtime (https://wasmtime.dev)")
    if check.status is not OK:
        return check
    tools = prober.run(["wasm-tools", "--version"]) if prober.which("wasm-tools") \
        else ProbeResult(found=False)
    if tools.ok:
        check.detail = f"wasmtime {check.version}; {tools.text.splitlines()[0]}"
    else:
        # Present-but-incomplete: wasmtime runs, Component Model tooling is gone.
        return Check(check.name, WARN, check.version,
                     f"wasmtime {check.version} OK, but wasm-tools "
                     "(Component Model) is not on PATH", tier="wasm",
                     available=True)
    return check


def check_wasm_tools(prober: Prober) -> Check:
    """wasm-tools on its own line too, since it is called out in the roadmap as
    the Wasm Component Model tooling."""
    return _tool_check(
        prober, "wasm Component Model (wasm-tools)", ["wasm-tools", "--version"],
        missing_hint="cargo install wasm-tools")[0]


def check_cordis_py(prober: Prober) -> Check:
    """The cordis-py runtime, resolvable by the interpreter that boots py
    processes (this one). Absent it, py runs and placements skip with a reason —
    a WARN, not a MISSING error, because the compiler itself is fine without it."""
    if prober.module_available("cordis"):
        return Check("cordis-py runtime", OK, None,
                     "importable by the running interpreter")
    return Check("cordis-py runtime", WARN, None,
                 "'cordis' not importable — set up backends/python "
                 "(sh backends/python/setup.sh)")


def check_cordis_ts(prober: Prober, backends_dir: Path) -> Check:
    """The cordis-ts runtime — installed under the typescript backend's
    node_modules, the same location the ts driver checks."""
    cordis = backends_dir / "typescript" / "node_modules" / "cordis"
    if prober.is_dir(cordis):
        return Check("cordis-ts runtime", OK, None, f"installed at {cordis}")
    return Check("cordis-ts runtime", WARN, None,
                 "not installed (backends/typescript/node_modules/cordis "
                 "missing) — run `npm install` there")


def check_mtls(prober: Prober) -> Check:
    """mTLS support: the network seam is Python `ssl` (a mutual-auth
    SSLContext), and minting loopback test certs needs the openssl CLI. `ssl`
    is stdlib, so the OK case is normal; a missing openssl CLI is a WARN because
    only the throwaway-cert path needs it, not the transport itself."""
    if not prober.module_available("ssl"):
        return Check("mTLS support", MISSING, None,
                     "the Python `ssl` module is unavailable")
    if prober.which("openssl") is None:
        return Check("mTLS support", WARN, None,
                     "ssl OK, but the openssl CLI (loopback test-cert minting) "
                     "is not on PATH")
    result = prober.run(["openssl", "version"])
    return Check("mTLS support", OK, _first_version(result.text),
                 f"ssl + {result.text.splitlines()[0] if result.ok else 'openssl'}")


def check_otel(prober: Prober) -> Check:
    """OpenTelemetry export — an optional extra (`pip install revl[otel]`). Its
    absence is the normal base install, so MISSING here reads as 'not enabled',
    a WARN-level fact rather than an error."""
    if prober.module_available("opentelemetry"):
        version = None
        try:
            from importlib.metadata import version as _v  # noqa: PLC0415
            version = _v("opentelemetry-api")
        except Exception:  # noqa: BLE001
            pass
        return Check("OpenTelemetry (revl[otel])", OK, version, "SDK importable")
    return Check("OpenTelemetry (revl[otel])", WARN, None,
                 "optional extra not installed (pip install revl[otel])")


def check_stdlib_version(prober: Prober) -> Check:
    """The stdlib version stamp (roadmap item 389).

    Reports the version this compiler expects and, crucially, warns when the
    stdlib the import resolver would actually LOAD carries a different stamp —
    the drift a consumer's vendored byte-copy falls into (the item-104
    `value_is_object` case). ``prober`` is unused: this check reads files, not
    tools, but keeps the uniform signature so ``diagnose`` treats it like any
    other check.
    """
    from .stdlib_version import (  # noqa: PLC0415 — lazy, no import-time cost
        EXPECTED_STDLIB_VERSION,
        check_drift,
        read_stamp,
        resolve_loaded_stdlib_dir,
    )

    loaded_dir = resolve_loaded_stdlib_dir()
    drift = check_drift(loaded_dir, EXPECTED_STDLIB_VERSION)
    if drift is None:
        return Check("stdlib version stamp", OK, EXPECTED_STDLIB_VERSION,
                     "loaded stdlib matches the version this compiler expects")
    loaded = read_stamp(loaded_dir) or "unstamped"
    return Check("stdlib version stamp", WARN, EXPECTED_STDLIB_VERSION,
                 f"{drift} (loaded {loaded} from {loaded_dir})")


def check_approval_wal(prober: Prober) -> Check:
    """Where the approval WAL will actually be written, and whether it is
    DURABLE (issue #289).

    The gate's authority is recorded in the WAL, and the WAL directory resolves
    at runtime from the environment (``REVL_WAL_DIR`` / ``XDG_STATE_HOME`` /
    HOME). When no durable candidate can be created — a read-only or absent HOME
    — it falls back to the process tempdir, which the OS may clear at any time.
    That used to happen silently; it is a warning at the call site now, and this
    is the same fact where an operator goes to ask "is this set up correctly".

    ``prober`` is unused (this reads the environment and the filesystem, not a
    tool) but keeps the uniform signature. Resolving CREATES the directory, the
    same one the next session would create — the honest probe of "can this be
    written" is to write it.
    """
    from .wal import resolve_wal_dir  # noqa: PLC0415 — lazy, no import-time cost

    try:
        resolution = resolve_wal_dir()
    except Exception as exc:  # noqa: BLE001 — a probe never crashes the report
        return Check("approval WAL durability", WARN, None,
                     f"could not resolve the WAL directory: {exc}")
    return Check("approval WAL durability",
                 OK if resolution.durable else WARN, None, resolution.summary())


# --------------------------------------------------------------- diagnose


def diagnose(prober: Prober | None = None,
             backends_dir: Path | None = None) -> Report:
    """Build the full report. Pure over the injected prober, so the whole thing
    is deterministically testable without touching the host's real tools."""
    prober = prober or Prober()
    if backends_dir is None:
        backends_dir = Path(__file__).resolve().parents[2] / "backends"

    version = revl_version()
    checks = [
        check_compiler(prober, version),
        check_python(prober),
        check_typescript(prober),
        check_rust(prober),
        check_java(prober),
        check_go(prober),
        check_wasm(prober),
        check_wasm_tools(prober),
        check_cordis_py(prober),
        check_cordis_ts(prober, backends_dir),
        check_mtls(prober),
        check_otel(prober),
        check_stdlib_version(prober),
        check_approval_wal(prober),
    ]
    return Report(version, checks)


# ----------------------------------------------------------------- smoke
#
# A per-tier end-to-end smoke test: compile the trivial program to the tier and
# (where a runner exists) boot it once. Only tiers whose toolchain is available
# are run; a MISSING tier is reported as skipped-because-unavailable, never a
# failure.


def _tier_availability(report: Report) -> dict[str, tuple[bool, str]]:
    """tier -> (available, reason-if-not), read off the checks that gate it."""
    out: dict[str, tuple[bool, str]] = {}
    for check in report.checks:
        if check.tier is None:
            continue
        # cordis-py additionally gates the py tier's actual boot.
        available = check.available
        reason = check.detail if not available else ""
        out[check.tier] = (available, reason)
    py = out.get("py")
    cordis_ok = any(c.name == "cordis-py runtime" and c.status is OK
                    for c in report.checks)
    if py is not None and not cordis_ok:
        out["py"] = (False, "cordis-py runtime not resolvable")
    ts = out.get("ts")
    cordis_ts_ok = any(c.name == "cordis-ts runtime" and c.status is OK
                       for c in report.checks)
    if ts is not None and ts[0] and not cordis_ts_ok:
        out["ts"] = (False, "cordis-ts runtime not installed")
    return out


def run_smoke(report: Report, prober: Prober | None = None,
              runner=None, tiers: list[str] | None = None,
              timeout: int = SMOKE_TIMEOUT) -> list[Smoke]:
    """Run the smoke test for every available tier. `runner(tier, timeout)` is
    the injectable boot step (defaults to a real `revl run --once` subprocess),
    returning a ProbeResult; a MISSING tier is skipped with its reason."""
    prober = prober or Prober()
    runner = runner or _default_smoke_runner
    tiers = tiers or ["py", "ts", "rust", "java", "go", "wasm"]
    availability = _tier_availability(report)

    results: list[Smoke] = []
    for tier in tiers:
        available, reason = availability.get(tier, (False, "tier not diagnosed"))
        if not available:
            results.append(Smoke(tier, SKIPPED,
                                 reason or "toolchain unavailable"))
            continue
        result = runner(tier, timeout)
        if result.timed_out:
            results.append(Smoke(tier, FAIL, f"smoke timed out (> {timeout}s)"))
        elif result.ok:
            results.append(Smoke(tier, PASS, "compiled and booted (--once)"))
        else:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            results.append(Smoke(tier, FAIL,
                                 detail[-1] if detail else "boot failed"))
    return results


def _default_smoke_runner(tier: str, timeout: int) -> ProbeResult:
    """Boot the trivial program on `tier` via a real `revl run --once`
    subprocess, under this same interpreter. Success is exit 0 — the driver
    exits nonzero on a residue failure or a skip-with-reason, so the exit code
    is the honest signal without parsing markers."""
    import tempfile  # noqa: PLC0415

    prober = Prober()
    with tempfile.TemporaryDirectory() as tmp:
        program = Path(tmp) / "smoke.rvl"
        program.write_text(SMOKE_PROGRAM, encoding="utf-8")
        argv = [sys.executable, "-m", "revl", "run", str(program),
                "--backend", tier, "--once"]
        # stdin closed (empty): the REPL hits EOF and the once round-trip runs.
        return prober.run(argv, timeout=timeout, stdin="")


# ---------------------------------------------------------------- rendering


_STATUS_LABEL = {OK: "OK", WARN: "WARN", MISSING: "MISSING"}
_SMOKE_LABEL = {PASS: "PASS", FAIL: "FAIL", SKIPPED: "SKIP"}


def render_text(report: Report) -> str:
    """The human table: one aligned row per check, then the smoke section."""
    lines = [f"revl doctor — compiler {report.revl_version}", ""]
    width = max((len(c.name) for c in report.checks), default=0)
    for check in report.checks:
        label = _STATUS_LABEL.get(check.status, check.status.upper())
        version = f" {check.version}" if check.version else ""
        detail = f"  {check.detail}" if check.detail else ""
        lines.append(f"  [{label:<7}] {check.name:<{width}}{version}{detail}")

    if report.smoke:
        lines.append("")
        lines.append("smoke test (compile + boot --once, available tiers only):")
        twidth = max(len(s.tier) for s in report.smoke)
        for smoke in report.smoke:
            label = _SMOKE_LABEL.get(smoke.outcome, smoke.outcome.upper())
            detail = f"  {smoke.detail}" if smoke.detail else ""
            lines.append(f"  [{label:<7}] {smoke.tier:<{twidth}}{detail}")

    counts = _counts(report)
    lines.append("")
    lines.append(f"  {counts['ok']} OK, {counts['warn']} WARN, "
                 f"{counts['missing']} MISSING"
                 + (f"; smoke {counts['smoke_pass']} pass / "
                    f"{counts['smoke_fail']} fail / "
                    f"{counts['smoke_skipped']} skipped"
                    if report.smoke else ""))
    return "\n".join(lines)


def _counts(report: Report) -> dict[str, int]:
    return {
        "ok": sum(1 for c in report.checks if c.status is OK),
        "warn": sum(1 for c in report.checks if c.status is WARN),
        "missing": sum(1 for c in report.checks if c.status is MISSING),
        "smoke_pass": sum(1 for s in report.smoke if s.outcome is PASS),
        "smoke_fail": sum(1 for s in report.smoke if s.outcome is FAIL),
        "smoke_skipped": sum(1 for s in report.smoke if s.outcome is SKIPPED),
    }


def to_json(report: Report) -> dict:
    """The machine-readable form for an agent calling `revl doctor --json`."""
    return {
        "revl_version": report.revl_version,
        "checks": [
            {"name": c.name, "status": c.status, "version": c.version,
             "detail": c.detail, "tier": c.tier}
            for c in report.checks
        ],
        "smoke": [
            {"tier": s.tier, "outcome": s.outcome, "detail": s.detail}
            for s in report.smoke
        ],
        "summary": _counts(report),
    }


# ------------------------------------------------------------------- verb


def doctor_command(args) -> int:
    """`revl doctor` — diagnose every tier and dependency, then smoke-test the
    available ones. Always exits 0: reporting the gaps is the job, so a missing
    or failing tier is a line in the report, never a nonzero exit."""
    report = diagnose()
    if not getattr(args, "no_smoke", False):
        timeout = getattr(args, "smoke_timeout", None) or SMOKE_TIMEOUT
        report.smoke = run_smoke(report, timeout=timeout)

    if getattr(args, "json", False):
        print(json.dumps(to_json(report), indent=2))
    else:
        print(render_text(report))
    return 0
