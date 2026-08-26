"""Crash recovery on the JAVA tier, end to end (roadmap item 322, Slice 2).

The java analog of tests/test_crash_recovery.py's "simulated kill -9" case and
tests/test_go_crash_recovery.py, but driven by a REAL JVM process. Slice 1
generalized the WAL/recovery core so `revl recover` reads a WAL produced by any
tier (src/revl/wal.py); Slice 2 gives the java runtime a durable WAL sink and
proves it:

* a witnessed java composition (backends/java/scenarios/crashproof) is emitted in
  record mode, so its witnessed transactional mutation writes a durable
  discharge-descriptor to a host-visible WAL file and fsyncs it via
  FileChannel.force;
* the java process is CRASHED mid-session (Runtime.halt before the discharge /
  activation-complete markers), leaving the descriptor on disk with no terminal
  marker;
* `revl recover` — the SAME tier-agnostic core the py tier uses, with no py
  backend on the path — reads the java WAL, re-issues the java tier's declared
  inverse (`delete_row(row#1)`) LIFO, and lands residue-free;
* the clean control (no crash) writes the discharge + terminal marker, and
  recover rolls forward instead.

Requires a working JDK (>= 21, the release the java emitter targets); skipped
with a reason when unavailable (the runtime-availability convention the java run
tier uses), so a CI box without a JDK does not red on it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import wal as wal_core  # noqa: E402
from revl.recovery import recover  # noqa: E402
from revl.run_java import JAVAC_RELEASE, _working_jdk_bin, java_runtime_reason  # noqa: E402

JAVA_DIR = ROOT / "backends" / "java"
SCENARIO = JAVA_DIR / "scenarios" / "crashproof"
IR_FIXTURE = SCENARIO / "crashproof.ir.json"


def _emit_java_record(ir: dict) -> str:
    spec = importlib.util.spec_from_file_location("revl_java_emit", JAVA_DIR / "emit.py")
    emit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit)
    return emit.emit(ir, "revl", record=True)


@pytest.fixture(scope="module")
def producer_classpath(tmp_path_factory):
    """Emit Components.java (record mode) from the IR, then compile it + the
    cordis4j stubs + CrashProducer into a classes dir once for the module."""
    reason = java_runtime_reason()
    if reason is not None:
        pytest.skip(f"java runtime unavailable; the java crash-recovery proof needs it: {reason}")
    jdk_bin = _working_jdk_bin()
    assert jdk_bin is not None

    work = tmp_path_factory.mktemp("crashproof")
    gen = work / "revl"
    gen.mkdir()
    ir = json.loads(IR_FIXTURE.read_text(encoding="utf-8"))
    (gen / "Components.java").write_text(_emit_java_record(ir), encoding="utf-8")

    out = work / "classes"
    out.mkdir()
    javac = str(Path(jdk_bin) / "javac")
    stubs = [str(p) for p in (JAVA_DIR / "stubs").rglob("*.java")]
    compile = subprocess.run(
        [javac, "--release", JAVAC_RELEASE, "-d", str(out), *stubs,
         str(gen / "Components.java"), str(SCENARIO / "CrashProducer.java")],
        capture_output=True, text=True,
    )
    if compile.returncode != 0:
        pytest.fail(f"compiling the java crash producer failed:\n{compile.stderr}")
    return str(out), str(Path(jdk_bin) / "java")


def _run_producer(classpath: str, java: str, wal: Path, *, crash: bool) -> subprocess.CompletedProcess:
    env = {"REVL_WAL": str(wal), "PATH": "/usr/bin:/bin"}
    if crash:
        env["REVL_CRASH_BEFORE_COMMIT"] = "1"
    return subprocess.run(
        [java, "-cp", classpath, "CrashProducer"],
        env=env, capture_output=True, text=True,
    )


def test_a_java_process_crash_mid_session_rolls_back_from_the_durable_wal(
        producer_classpath, tmp_path):
    """The proof: a real JVM process registers a witnessed mutation (durable
    descriptor), then CRASHES before commit. recover reads the java-written WAL
    through the tier-agnostic core and rolls the java tier's inverse back clean."""
    classpath, java = producer_classpath
    wal = tmp_path / "java_crash.wal"
    proc = _run_producer(classpath, java, wal, crash=True)

    # the process really died mid-session (Runtime.halt(137)), not a graceful stop
    assert proc.returncode == 137, proc.stderr

    # the durable WAL carries the descriptor but NO commit markers — read it
    # through the same tier-agnostic reader recover uses
    loaded = wal_core.read_wal(str(wal))
    descriptors = [r for r in loaded["records"]
                   if r.get("record") == "discharge-descriptor"]
    assert len(descriptors) == 1
    assert descriptors[0]["entry"] == "transactional"
    assert descriptors[0]["call"] == {
        "receiver": "begin_row", "method": "delete_row", "args": ["row#1"]}
    assert not [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert loaded["complete"] is False   # no activation-complete: the crash

    # recover re-issues the java tier's declared inverse and lands residue-free
    report = recover(str(wal))
    assert report["verdict"] == "rolled-back"
    assert [s["seq"] for s in report["transactionalRolledBack"]] == [0]
    assert report["dischargedSkipped"] == []
    # the mutation's referent was cleared by the re-issued inverse
    assert "begin_row:row#1" not in report["residue"]["worldRemaining"]
    assert report["residue"]["clean"] is True
    assert "LIFO" in report["decision"]


def test_the_clean_control_commits_and_rolls_forward(producer_classpath, tmp_path):
    """The contrast: no crash. The java process disposes cleanly, stamps the
    discharge + terminal marker, and recover rolls forward — the committed
    mutation is retained, never rolled back."""
    classpath, java = producer_classpath
    wal = tmp_path / "java_clean.wal"
    proc = _run_producer(classpath, java, wal, crash=False)
    assert proc.returncode == 0, proc.stderr

    loaded = wal_core.read_wal(str(wal))
    assert loaded["complete"] is True    # activation-complete present
    discharged = [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert discharged == [{"record": "discharge", "discharged": [0]}]

    report = recover(str(wal))
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True


def test_run_java_passthrough_records_and_rolls_forward(tmp_path, monkeypatch):
    """Item 322 Slice 2, the run-harness passthrough: `revl run --backend java`
    under REVL_WAL emits Components in record mode, hands the env to the JVM, and
    the stub once-runner (RunOnce) writes the discharge + terminal marker on its
    clean unload — so recover rolls this activation FORWARD. Proves the WAL sink
    is reachable through the run harness (the stub runner path), not only the
    dedicated crash producer."""
    reason = java_runtime_reason()
    if reason is not None:
        pytest.skip(f"java runtime unavailable; the run passthrough needs it: {reason}")

    from revl.run_java import run_java  # noqa: PLC0415 — gated behind the JDK skip

    wal = tmp_path / "run_passthrough.wal"
    monkeypatch.setenv("REVL_WAL", str(wal))
    ir = json.loads(IR_FIXTURE.read_text(encoding="utf-8"))
    rc = run_java(ir, {}, [], once=True)
    assert rc == 0

    loaded = wal_core.read_wal(str(wal))
    assert loaded["complete"] is True
    descriptors = [r for r in loaded["records"]
                   if r.get("record") == "discharge-descriptor"]
    assert len(descriptors) == 1
    assert descriptors[0]["call"] == {
        "receiver": "begin_row", "method": "delete_row", "args": ["row#1"]}

    report = recover(str(wal))
    assert report["verdict"] == "rolled-forward"
    assert report["residue"]["clean"] is True


def test_the_wal_header_records_the_shared_guarantee(producer_classpath, tmp_path):
    """The java-written header carries the same WAL guarantee text the py core
    pins, so a py tool that reads a java WAL agrees on what recovery may claim."""
    classpath, java = producer_classpath
    wal = tmp_path / "java_header.wal"
    _run_producer(classpath, java, wal, crash=False)
    loaded = wal_core.read_wal(str(wal))
    assert loaded["header"].get("walVersion") == wal_core.WAL_VERSION
    assert loaded["header"].get("guarantee") == wal_core.WAL_GUARANTEE
