"""A java test that probes the JDK by hand fails when java is required.

Issue #2734. The per-tier support table credits java's durable-recovery cell
`proved` because the `frontend` job collects
``tests/test_java_crash_recovery.py``, and that test skips with a reason when
``java_runtime_reason`` finds no working JDK. A skip is green, so a JDK setup
step that broke would leave the cell resting on a measurement that never ran.

Two things have to hold for that cell to mean what it says, and this file pins
both. The repository-root ``conftest.py`` learns the java probe, so the skip
becomes a failure under ``REVL_REQUIRE_TIERS`` exactly as it does for go and
rust. And the job that collects the crash test both installs a JDK and names
java, which is what makes requiring it possible on the runner rather than an
aspiration.

The child run patches ``run_java._working_jdk_bin`` so the skip is reproducible
on a machine that has a JDK: the subject here is the conftest conversion, not
whether this host happens to have java.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

NODE = ("tests/test_java_crash_recovery.py::"
        "test_a_java_process_crash_mid_session_rolls_back_from_the_durable_wal")

#: Loaded with `-p` in the child run. Patching the resolver rather than the
#: probe keeps the real skip message, so the assertion can name the phrase the
#: conftest actually matched.
_PLUGIN = """
def pytest_configure(config):
    from revl import run_java
    run_java._working_jdk_bin = lambda: None
"""


def _path_without_javac() -> str:
    """PATH with every directory that holds a `javac` binary removed."""
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not (Path(d) / "javac").exists()]
    return os.pathsep.join(keep)


def _fake_javac(directory: Path) -> None:
    """A `javac` that is on PATH and does not answer, like the macOS shim."""
    binary = directory / "javac"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)


def _run(tmp_path: Path, *, require: str | None, unusable: bool = False) -> str:
    (tmp_path / "java_jdk_absent.py").write_text(_PLUGIN, encoding="utf-8")
    env = {**os.environ}
    env.pop("REVL_REQUIRE_TIERS", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), env.get("PYTHONPATH", "")])
    path = _path_without_javac()
    if unusable:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _fake_javac(bindir)
        path = os.pathsep.join([str(bindir), path])
    env["PATH"] = path
    if require is not None:
        env["REVL_REQUIRE_TIERS"] = require
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "java_jdk_absent", NODE],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    return result.stdout + result.stderr


def test_a_hand_probed_java_test_skips_when_java_is_not_required(tmp_path):
    """The control. Without the switch a laptop with no working JDK still
    skips with a reason, which is the behaviour this hook must not break."""
    output = _run(tmp_path, require=None)
    assert "1 skipped" in output, output


def test_a_hand_probed_java_test_fails_when_java_is_required_and_absent(
        tmp_path):
    output = _run(tmp_path, require="java")
    _assert_red(output)
    assert "`javac` is not on PATH" in output, output


def test_a_hand_probed_java_test_fails_when_javac_is_present_but_unusable(
        tmp_path):
    """`/usr/bin/javac` is a shim that errors until a JDK is installed, and a
    JDK older than 21 cannot build the emitter's output. Both are on PATH and
    both are unusable provisioning, not an environment fact."""
    output = _run(tmp_path, require="py,java", unusable=True)
    _assert_red(output)
    assert "its runtime does not resolve (javac is on PATH)" in output, output


def _assert_red(output: str) -> None:
    # a skip raised in setup (a fixture) reports as an error, one raised in the
    # test body as a failure; both are red
    assert re.search(r"\b1 (failed|error)\b", output), output
    assert "1 skipped" not in output, output
    assert "the java tier is REQUIRED here" in output, output
    assert "no working JDK found" in output, output


def _required(job: dict) -> set[str]:
    raw = str(job.get("env", {}).get("REVL_REQUIRE_TIERS", ""))
    return {n for n in raw.replace(",", " ").split() if n}


def _workflow() -> dict:
    return yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_the_frontend_job_requires_java_and_installs_a_jdk():
    """The half the conftest cannot check: the job that collects the crash
    test names java, and the runner it names it on is given a JDK to find."""
    job = _workflow()["jobs"]["frontend"]
    assert "java" in _required(job), (
        "the frontend job collects tests/test_java_crash_recovery.py, so it "
        "must require java or the skip stays green"
    )
    assert any("pytest tests/ -q" in str(step.get("run", ""))
               for step in job["steps"]), (
        "the frontend job no longer runs the root suite that collects the "
        "java crash test")
    setup = [step for step in job["steps"]
             if str(step.get("uses", "")).startswith("actions/setup-java@")]
    assert setup, "the frontend job requires java but installs no JDK"
    assert any(str(step.get("with", {}).get("java-version")) == "21"
               for step in setup), setup


def test_the_backend_java_job_requires_java_and_runs_its_suite():
    """The audit that makes every tier test run with its tier required reads
    this job for the four backends/java files; without the switch there, the
    java exemption it used to carry could not be dropped."""
    job = _workflow()["jobs"]["backend-java"]
    assert "java" in _required(job), (
        "backend-java installs a JDK and runs backends/java/, so it must "
        "require java or the audit that covers those files cannot pass")
    assert any("pytest backends/java/" in str(step.get("run", ""))
               for step in job["steps"]), (
        "backend-java no longer runs the suite the java requirement covers")


def test_the_ci_audit_registry_mirrors_the_java_probe():
    """The audit keeps its own copy of the hand probes, and the repository-root
    conftest keeps one per required tier. A java entry in one and not the
    other is how the crash test stayed invisible to the coverage audit."""
    text = (ROOT / "tests" / "test_env_gated_skips_run_somewhere.py").read_text(
        encoding="utf-8")
    assert re.search(r'_DIRECT_PROBES[^}]*"java": re\.compile\(r"java_runtime_reason\\\("\)',
                     text), (
        "the coverage audit does not mirror the conftest's java probe")
    start = text.index("_TIER_NOT_REQUIRED_IN_CI: dict[str, str] = {")
    end = text.index("\n}", start)
    assert '"java"' not in text[start:end], (
        "java is still exempted from REVL_REQUIRE_TIERS while the frontend and "
        "backend-java jobs now require it; the exemption is stale")


def test_the_probe_pattern_is_the_one_the_crash_test_spells():
    """A `_PROBES` entry that the test file does not spell is a silent no-op,
    which is the failure mode the entry exists to close."""
    conftest = (ROOT / "conftest.py").read_text(encoding="utf-8")
    match = re.search(r'"java":\s*\("javac",\s*re\.compile\(r"([^"]+)"\)\)',
                      conftest)
    assert match, "conftest.py has no java probe entry"
    text = (ROOT / "tests" / "test_java_crash_recovery.py").read_text(
        encoding="utf-8")
    assert re.search(match.group(1), text), (
        "the java probe pattern no longer matches the crash test's probe")
