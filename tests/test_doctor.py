"""`revl doctor` — per-tier install / toolchain / runtime diagnosis + smoke test
(roadmap item 291).

The whole point of the command is to say, without lying, what is installed and
working. So the tests pin the two things that make that trustworthy:

  1. every status comes from an actual probe, and the classification is exactly
     OK / WARN / MISSING per what the probe returned;
  2. a probe that cannot answer (tool absent, or launched-but-erroring) is a
     WARN/MISSING with a reason, never a crash.

To keep that deterministic on any box, the probing seam (`doctor.Prober`) is
*injected*: a `FakeProber` returns canned answers, so the assertions never
depend on which tools happen to be installed on the test runner. The one real
`revl doctor` invocation at the bottom is a smoke test of the command itself:
it must print a full report and exit 0 even with tiers missing.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import doctor  # noqa: E402
from revl.doctor import (  # noqa: E402
    FAIL, MISSING, OK, PASS, SKIPPED, WARN, ProbeResult)


# --------------------------------------------------------------- fake prober


class FakeProber:
    """A deterministic stand-in for `doctor.Prober`: it answers `which` from a
    set of present executables, `run` from a table keyed by the command's first
    token, and imports/paths from explicit sets. Anything unspecified reads as
    absent — the same shape a real box with nothing installed would show."""

    def __init__(self, *, present=(), runs=None, modules=(), dirs=()):
        self._present = set(present)
        self._runs = dict(runs or {})
        self._modules = set(modules)
        self._dirs = {str(d) for d in dirs}
        self.run_calls = []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self._present else None

    def run(self, argv, timeout=doctor.PROBE_TIMEOUT, stdin=""):
        self.run_calls.append(list(argv))
        # keyed by the executable's basename so a resolved absolute path
        # (e.g. /usr/bin/javac) matches the same canned answer.
        key = Path(argv[0]).name
        answer = self._runs.get(key)
        if answer is None:
            return ProbeResult(found=False)
        return answer

    def module_available(self, name):
        return name in self._modules

    def is_dir(self, path):
        return str(path) in self._dirs


def _ok_run(stdout="", stderr=""):
    return ProbeResult(found=True, returncode=0, stdout=stdout, stderr=stderr)


def _fail_run(stdout="", stderr=""):
    return ProbeResult(found=True, returncode=1, stdout=stdout, stderr=stderr)


def _check(report, name):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c.name for c in report.checks]}")


# a prober where every backend and dependency is present and current, so the
# report is all-OK except the things that are genuinely optional/absent.
def _full_house():
    return FakeProber(
        present=("node", "cargo", "rustc", "javac", "java", "go",
                 "wasmtime", "wasm-tools", "openssl"),
        runs={
            "node": _ok_run("v26.7.0\n"),
            "cargo": _ok_run("cargo 1.85.1 (abc 2025-03-15)\n"),
            "rustc": _ok_run("rustc 1.85.1 (4eb161250 2025-03-15)\n"),
            "javac": _ok_run(stderr="javac 21.0.1\n"),
            "java": _ok_run(stderr='openjdk version "21.0.1"\n'),
            "go": _ok_run("go version go1.26.5 darwin/arm64\n"),
            "wasmtime": _ok_run("wasmtime 47.0.3 (5554cc1a6)\n"),
            "wasm-tools": _ok_run("wasm-tools 1.257.1\n"),
            "openssl": _ok_run("OpenSSL 3.6.3 9 Jun 2026\n"),
        },
        modules={"ssl", "cordis"},
        dirs=(ROOT / "backends" / "typescript" / "node_modules" / "cordis",),
    )


# --------------------------------------------------------------- structure


def test_report_has_a_row_for_every_tier_and_dependency():
    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")
    names = [c.name for c in report.checks]
    # every backend tier, every runtime, and both optional deps are on the record
    for expected in ("compiler (revl)", "python backend",
                     "typescript backend (node)", "rust backend (cargo)",
                     "java backend (JDK)", "go backend",
                     "wasm backend (wasmtime)", "wasm Component Model (wasm-tools)",
                     "cordis-py runtime", "cordis-ts runtime",
                     "mTLS support", "OpenTelemetry (revl[otel])"):
        assert expected in names, expected


def test_every_status_is_one_of_the_three_labels():
    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")
    for check in report.checks:
        assert check.status in (OK, WARN, MISSING), (check.name, check.status)


def test_all_present_reads_as_ok_with_versions():
    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")
    node = _check(report, "typescript backend (node)")
    assert node.status is OK and node.version == "26.7.0"
    assert _check(report, "rust backend (cargo)").version == "1.85.1"
    assert _check(report, "go backend").version == "1.26.5"
    assert _check(report, "java backend (JDK)").status is OK
    assert _check(report, "cordis-py runtime").status is OK
    assert _check(report, "cordis-ts runtime").status is OK


# --------------------------------------------------------- missing / broken


def test_a_missing_tool_is_missing_not_a_crash():
    # nothing installed at all — every probed tier must classify, none may raise
    report = doctor.diagnose(FakeProber(), backends_dir=ROOT / "backends")
    assert _check(report, "rust backend (cargo)").status is MISSING
    assert _check(report, "go backend").status is MISSING
    assert _check(report, "wasm backend (wasmtime)").status is MISSING
    assert _check(report, "java backend (JDK)").status is MISSING
    # the compiler and the python host are definitionally present
    assert _check(report, "compiler (revl)").status is OK
    assert _check(report, "python backend").status is OK


def test_a_tool_on_path_but_erroring_is_not_ok():
    # the macOS javac shim case: on PATH, but `-version` exits nonzero
    prober = FakeProber(present=("javac",), runs={"javac": _fail_run(
        stderr="Unable to locate a Java Runtime\n")})
    report = doctor.diagnose(prober, backends_dir=ROOT / "backends")
    assert _check(report, "java backend (JDK)").status is MISSING


def test_node_too_old_is_warn_with_the_required_version():
    prober = FakeProber(present=("node",), runs={"node": _ok_run("v18.4.0\n")})
    report = doctor.diagnose(prober, backends_dir=ROOT / "backends")
    node = _check(report, "typescript backend (node)")
    assert node.status is WARN
    assert node.version == "18.4.0"
    assert "23.6" in node.detail  # names the floor it fell under


def test_old_jdk_is_warn_naming_the_required_release():
    # a JDK 17 where the emitter needs 21 — the exact mismatch this project hit
    prober = FakeProber(present=("javac",),
                        runs={"javac": _ok_run(stderr="javac 17.0.9\n")})
    report = doctor.diagnose(prober, backends_dir=ROOT / "backends")
    java = _check(report, "java backend (JDK)")
    assert java.status is WARN
    assert java.version == "17.0.9"
    assert "21" in java.detail and "17" in java.detail


def test_absent_optional_dep_is_warn_not_missing():
    # opentelemetry and the cordis runtimes are optional: their absence is a
    # normal WARN, not an error that could fail the command
    report = doctor.diagnose(FakeProber(), backends_dir=ROOT / "backends")
    assert _check(report, "OpenTelemetry (revl[otel])").status is WARN
    assert _check(report, "cordis-py runtime").status is WARN
    assert _check(report, "cordis-ts runtime").status is WARN


def test_wasm_tools_absent_warns_but_wasmtime_still_available():
    prober = FakeProber(present=("wasmtime",),
                        runs={"wasmtime": _ok_run("wasmtime 47.0.3\n")})
    report = doctor.diagnose(prober, backends_dir=ROOT / "backends")
    wasm = _check(report, "wasm backend (wasmtime)")
    assert wasm.status is WARN
    assert wasm.available  # the tier still runs; only Component Model tooling is gone


def test_mtls_warns_when_openssl_cli_absent_but_ssl_present():
    prober = FakeProber(modules={"ssl"})  # ssl importable, no openssl on PATH
    report = doctor.diagnose(prober, backends_dir=ROOT / "backends")
    assert _check(report, "mTLS support").status is WARN


# ------------------------------------------------------------------- json


def test_json_shape_is_stable_and_complete():
    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")
    report.smoke = [doctor.Smoke("py", SKIPPED, "no runtime")]
    blob = doctor.to_json(report)
    assert set(blob) == {"revl_version", "checks", "smoke", "summary"}
    row = blob["checks"][0]
    assert set(row) == {"name", "status", "version", "detail", "tier"}
    assert blob["smoke"][0] == {"tier": "py", "outcome": SKIPPED,
                                "detail": "no runtime"}
    summary = blob["summary"]
    for key in ("ok", "warn", "missing", "smoke_pass", "smoke_fail",
                "smoke_skipped"):
        assert key in summary
    # the whole thing must round-trip as JSON (an agent consumes it)
    assert json.loads(json.dumps(blob))["revl_version"] == report.revl_version


# ----------------------------------------------------------------- smoke


def test_smoke_skips_a_missing_tier_and_never_calls_the_runner():
    # nothing installed: every tier must be skipped-because-unavailable, and the
    # runner must not be invoked for a tier whose toolchain is missing
    report = doctor.diagnose(FakeProber(), backends_dir=ROOT / "backends")
    called = []

    def runner(tier, timeout):
        called.append(tier)
        return _ok_run()

    smoke = doctor.run_smoke(report, runner=runner)
    assert called == []  # a MISSING tier is never booted
    outcomes = {s.tier: s.outcome for s in smoke}
    assert outcomes["rust"] is SKIPPED
    assert outcomes["go"] is SKIPPED
    assert outcomes["java"] is SKIPPED
    # a skip carries a reason, never an empty failure
    assert all(s.detail for s in smoke if s.outcome is SKIPPED)


def test_smoke_runs_available_tiers_and_reports_pass_fail():
    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")

    def runner(tier, timeout):
        # rust boots clean; go's runner exits nonzero with a reason on stderr
        if tier == "go":
            return _fail_run(stderr="gen.go:1: boom\n")
        return _ok_run("NO-RESIDUE\n")

    smoke = doctor.run_smoke(report, runner=runner)
    outcomes = {s.tier: s.outcome for s in smoke}
    assert outcomes["rust"] is PASS
    assert outcomes["go"] is FAIL
    # the fail carries the runner's last stderr line, not a bare "failed"
    go = next(s for s in smoke if s.tier == "go")
    assert "boom" in go.detail


def test_smoke_timeout_is_a_fail_not_a_crash():
    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")

    def runner(tier, timeout):
        return ProbeResult(found=True, timed_out=True)

    smoke = doctor.run_smoke(report, runner=runner, tiers=["rust"])
    assert smoke[0].outcome is FAIL
    assert "timed out" in smoke[0].detail


def test_py_smoke_is_skipped_without_cordis_even_if_python_is_fine():
    # the python *backend* is always OK (it is the host), but the py *tier's*
    # boot needs cordis-py — absent it, the smoke is skipped, not failed
    prober = FakeProber()  # no cordis module
    report = doctor.diagnose(prober, backends_dir=ROOT / "backends")
    called = []
    smoke = doctor.run_smoke(report,
                             runner=lambda t, to: called.append(t) or _ok_run(),
                             tiers=["py"])
    assert smoke[0].outcome is SKIPPED
    assert called == []


# ------------------------------------------------- approval WAL durability
#
# issue #289: the approval WAL falls back to the process tempdir when no durable
# per-user state directory can be created. The fallback is right (the session
# should still run) but it was silent, so the gate's record quietly stopped
# being durable. `revl doctor` is where an operator asks "is this set up
# correctly", so the fact belongs on the report — as an OK line naming the
# durable directory, or a WARN naming the cause.


def _wal_env(monkeypatch, home, platform="linux"):
    monkeypatch.delenv("REVL_WAL_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", platform)


def test_durable_wal_directory_is_an_ok_row_naming_it(tmp_path, monkeypatch):
    _wal_env(monkeypatch, tmp_path)
    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")
    check = _check(report, "approval WAL durability")
    assert check.status is OK
    assert str(tmp_path / ".local" / "state" / "revl" / "approval-wal") \
        in check.detail


def test_non_durable_wal_directory_is_a_warn_naming_the_cause(
        tmp_path, monkeypatch):
    """HOME as a regular file (unwritable for ANY uid, unlike a chmod, which
    root ignores): every durable candidate fails and the row must WARN, naming
    the candidate, the failure, and the tempdir the WAL actually lands in."""
    import tempfile as _tempfile

    home = tmp_path / "home-is-a-file"
    home.write_text("not a directory", encoding="utf-8")
    _wal_env(monkeypatch, home)

    report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")
    check = _check(report, "approval WAL durability")
    assert check.status is WARN
    assert "NOT durable" in check.detail
    assert str(home / ".local" / "state" / "revl" / "approval-wal") in check.detail
    assert "NotADirectoryError" in check.detail
    assert _tempfile.gettempdir() in check.detail
    # and it renders as a single table row, not a multi-line blob
    row = [line for line in doctor.render_text(report).splitlines()
           if "approval WAL durability" in line]
    assert len(row) == 1


def test_doctor_reports_the_wal_row_without_emitting_the_runtime_warning(
        tmp_path, monkeypatch):
    """Asking the question is not taking the fallback: doctor reads through
    `resolve_wal_dir`, so diagnosing a non-durable setup reports it rather than
    raising the session-path warning at the operator a second time."""
    import warnings

    from revl.wal import NonDurableWALWarning

    home = tmp_path / "home-is-a-file"
    home.write_text("not a directory", encoding="utf-8")
    _wal_env(monkeypatch, home)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = doctor.diagnose(_full_house(), backends_dir=ROOT / "backends")
    assert not [w for w in caught
                if issubclass(w.category, NonDurableWALWarning)]
    assert _check(report, "approval WAL durability").status is WARN


# ----------------------------------------------------------- the real command


def test_revl_doctor_runs_for_real_and_exits_zero_with_missing_tiers():
    # the command itself: a full report, exit 0, even though this interpreter is
    # missing several tiers. --no-smoke keeps it fast and hermetic.
    env = {"PYTHONPATH": str(ROOT / "src")}
    import os
    env = {**os.environ, **env}
    result = subprocess.run(
        [sys.executable, "-m", "revl", "doctor", "--no-smoke"],
        capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert "revl doctor" in result.stdout
    assert "compiler (revl)" in result.stdout
    # and the JSON form parses
    result_json = subprocess.run(
        [sys.executable, "-m", "revl", "doctor", "--no-smoke", "--json"],
        capture_output=True, text=True, env=env, check=False)
    assert result_json.returncode == 0, result_json.stderr
    blob = json.loads(result_json.stdout)
    assert "checks" in blob and blob["checks"]
