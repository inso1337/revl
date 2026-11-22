"""A failed WAL fsync stops the run on every tier that writes a WAL (issue #2345).

Split from #2183 item 2. The durable write-ahead log is only a guarantee if a
record the run goes on to act on really reached the disk. The py tier's
`WriteAheadLog._write` already raises on a failed fsync
(tests/test_emission_settlement_2183.py). This file covers the other writers:

* go: the emitted sink did `_ = w.f.Sync()`;
* rust: the emitted sink did `let _ = self.file.sync_all();`;
* java: the emitted sink caught and dropped the `IOException` from
  `FileChannel.force(true)`;
* wasm: the host drain `revl.run_wasm.write_wal_record` swallowed the
  `OSError` from `os.fsync`.

Each now fails closed, and each keeps one explicit, visible opt-out for a
target that cannot fsync: `REVL_WAL_ALLOW_UNSYNCED=1`, which the log records in
its header (`"durability": "unsynced-opt-out"`) and in a `durability-opt-out`
record, the same marks the py `--allow-unsynced-wal` leaves.

The fault is real, not mocked, for the compiled tiers: `/dev/null` accepts
writes but refuses fsync (EINVAL on Linux, ENODEV from F_FULLFSYNC on macOS),
so pointing `REVL_WAL` at it makes the producer's first fsync fail. On go the
header is written from the package `init`, so the producer's test body never
starts. The py-level wasm drain is fault-injected through `os.fsync`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import run_wasm  # noqa: E402

UNSYNCABLE = "/dev/null"
OPT_OUT = {"REVL_WAL_ALLOW_UNSYNCED": "1"}
FSYNC_FAILED = "write-ahead log fsync failed"


def _load_emitter(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"revl_{tier}_emit_2345", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _crashproof_ir(tier: str) -> dict:
    path = ROOT / "backends" / tier / "scenarios" / "crashproof" / "crashproof.ir.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---- emitted source: no writer may drop a sync error ----------------------


def test_go_sink_checks_the_sync_error():
    src = _load_emitter("go").emit(_crashproof_ir("go"), "crashrecovery", record=True)
    # the record's own fsync; the best-effort fsync after a cut lives in `cut`
    write = src[src.index("func (w *revlWAL) write("):]
    assert "_ = w.f.Sync()" not in write[:write.index("\n}\n")]
    assert "if err := w.f.Sync(); err != nil && !w.unsynced {" in src
    assert 'panic("revl: write-ahead log fsync failed: " + err.Error())' in src
    assert '"REVL_WAL_ALLOW_UNSYNCED"' in src
    assert '"unsynced-opt-out"' in src


def test_rust_sink_checks_the_sync_error():
    src = _load_emitter("rust").emit(_crashproof_ir("rust"), record=True)
    # the record's own fsync; the best-effort fsync after a cut lives in `cut`
    write = src[src.index("fn write(&mut self"):]
    assert "let _ = self.file.sync_all()" not in write[:write.index("\n    }\n")]
    assert "if let Err(e) = self.file.sync_all() {" in src
    assert 'panic!("revl: write-ahead log fsync failed: {e}");' in src
    assert '"REVL_WAL_ALLOW_UNSYNCED"' in src


def test_java_sink_checks_the_force_error():
    src = _load_emitter("java").emit(_crashproof_ir("java"), "revl", record=True)
    assert "catch (java.io.IOException ignored)" not in src
    assert ('throw new java.io.UncheckedIOException("revl: write-ahead log fsync failed", sync);'
            in src)
    assert '"REVL_WAL_ALLOW_UNSYNCED"' in src


# ---- go: build and run against a target that refuses fsync -----------------


GO = shutil.which("go")
GO_SCENARIOS = ROOT / "backends" / "go" / "scenarios"


@pytest.fixture(scope="module")
def go_producer(tmp_path_factory):
    if GO is None:
        pytest.skip("go toolchain not available; the go fsync proof needs it")
    out = tmp_path_factory.mktemp("crashproof_go") / "crashproof.test"
    build = subprocess.run([GO, "test", "-c", "-o", str(out), "./crashproof/"],
                           cwd=str(GO_SCENARIOS), capture_output=True, text=True)
    if build.returncode != 0:
        pytest.fail(f"building the go crash producer failed:\n{build.stderr}")
    return out


def _run_go(binary: Path, extra: dict) -> subprocess.CompletedProcess:
    env = {"REVL_WAL": UNSYNCABLE, "PATH": "/usr/bin:/bin", **extra}
    return subprocess.run([str(binary), "-test.v", "-test.run", "TestGoCrashRecoveryProducer"],
                          env=env, capture_output=True, text=True)


def test_go_failed_fsync_stops_before_the_body(go_producer):
    proc = _run_go(go_producer, {})
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert FSYNC_FAILED in output
    # the header fsync failed in the package init: the producer never started
    assert "=== RUN" not in proc.stdout, output


def test_go_opt_out_runs_and_warns(go_producer):
    proc = _run_go(go_producer, OPT_OUT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "=== RUN   TestGoCrashRecoveryProducer" in proc.stdout
    assert "REVL_WAL_ALLOW_UNSYNCED=1 disables the WAL fsync guarantee" in proc.stderr


# ---- rust: build and run against a target that refuses fsync ---------------


RUST_SCENARIO = ROOT / "backends" / "rust" / "scenarios" / "crashproof"


@pytest.fixture(scope="module")
def rust_producer():
    from revl.run_rust import rust_runtime_reason  # noqa: PLC0415
    reason = rust_runtime_reason()
    if reason is not None:
        pytest.skip(f"cordis-rs runtime not available; the rust fsync proof needs it: {reason}")
    build = subprocess.run(
        ["cargo", "build", "--bin", "crash_producer",
         "--manifest-path", str(RUST_SCENARIO / "Cargo.toml")],
        capture_output=True, text=True)
    if build.returncode != 0:
        pytest.fail(f"building the rust crash producer failed:\n{build.stderr}")
    target = Path(os.environ.get("CARGO_TARGET_DIR") or (RUST_SCENARIO / "target"))
    binary = target / "debug" / "crash_producer"
    assert binary.exists(), f"expected {binary} after cargo build"
    return binary


def _run_rust(binary: Path, extra: dict) -> subprocess.CompletedProcess:
    env = {"REVL_WAL": UNSYNCABLE, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}
    return subprocess.run([str(binary)], env=env, capture_output=True, text=True)


def test_rust_failed_fsync_stops_the_run(rust_producer):
    proc = _run_rust(rust_producer, {})
    assert proc.returncode != 0, proc.stderr
    assert FSYNC_FAILED in proc.stderr


def test_rust_opt_out_runs_and_warns(rust_producer):
    proc = _run_rust(rust_producer, OPT_OUT)
    assert proc.returncode == 0, proc.stderr
    assert "REVL_WAL_ALLOW_UNSYNCED=1 disables the WAL fsync guarantee" in proc.stderr


# ---- java: build and run against a target that refuses fsync ---------------


@pytest.fixture(scope="module")
def java_producer(tmp_path_factory):
    from revl.run_java import JAVAC_RELEASE, _working_jdk_bin, java_runtime_reason  # noqa: PLC0415
    reason = java_runtime_reason()
    if reason is not None:
        pytest.skip(f"java runtime unavailable; the java fsync proof needs it: {reason}")
    jdk_bin = Path(_working_jdk_bin())
    java_dir = ROOT / "backends" / "java"
    work = tmp_path_factory.mktemp("crashproof_java")
    gen = work / "revl"
    gen.mkdir()
    (gen / "Components.java").write_text(
        _load_emitter("java").emit(_crashproof_ir("java"), "revl", record=True),
        encoding="utf-8")
    out = work / "classes"
    out.mkdir()
    stubs = [str(p) for p in (java_dir / "stubs").rglob("*.java")]
    compiled = subprocess.run(
        [str(jdk_bin / "javac"), "--release", JAVAC_RELEASE, "-d", str(out), *stubs,
         str(gen / "Components.java"),
         str(java_dir / "scenarios" / "crashproof" / "CrashProducer.java")],
        capture_output=True, text=True)
    if compiled.returncode != 0:
        pytest.fail(f"compiling the java crash producer failed:\n{compiled.stderr}")
    return str(out), str(jdk_bin / "java")


def _run_java(producer, extra: dict) -> subprocess.CompletedProcess:
    classpath, java = producer
    env = {"REVL_WAL": UNSYNCABLE, "PATH": "/usr/bin:/bin", **extra}
    return subprocess.run([java, "-cp", classpath, "CrashProducer"],
                          env=env, capture_output=True, text=True)


def test_java_failed_fsync_stops_the_run(java_producer):
    proc = _run_java(java_producer, {})
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert FSYNC_FAILED in proc.stderr


def test_java_opt_out_runs_and_warns(java_producer):
    proc = _run_java(java_producer, OPT_OUT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REVL_WAL_ALLOW_UNSYNCED=1 disables the WAL fsync guarantee" in proc.stderr


# ---- wasm: the py host drain, fault-injected through os.fsync ---------------


def _fail_fsync(_fd):
    raise OSError("injected fsync failure")


def test_wasm_drain_raises_on_failed_fsync(tmp_path, monkeypatch):
    monkeypatch.delenv("REVL_WAL_ALLOW_UNSYNCED", raising=False)
    monkeypatch.setattr(run_wasm.os, "fsync", _fail_fsync)
    with open(tmp_path / "run.wal", "w", encoding="utf-8") as handle:
        with pytest.raises(run_wasm.WALDurabilityError, match=FSYNC_FAILED):
            run_wasm.write_wal_record(handle, run_wasm.wal_header())


def test_wasm_drain_opt_out_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(run_wasm.os, "fsync", _fail_fsync)
    with open(tmp_path / "run.wal", "w", encoding="utf-8") as handle:
        run_wasm.write_wal_record(handle, run_wasm.wal_header(), allow_unsynced=True)
        monkeypatch.setenv("REVL_WAL_ALLOW_UNSYNCED", "1")
        run_wasm.write_wal_record(handle, run_wasm.wal_header())


def test_wasm_run_never_starts_the_harness_on_a_failed_header_fsync(
        tmp_path, monkeypatch, capsys):
    """`run_wasm` stamps the header before it starts the harness process; a
    header that cannot be made durable must stop the run there, so no host
    body runs."""
    monkeypatch.setenv("REVL_WAL", str(tmp_path / "run.wal"))
    monkeypatch.delenv("REVL_WAL_ALLOW_UNSYNCED", raising=False)
    monkeypatch.setattr(run_wasm, "_load_order", lambda ir: ["Crasher"])
    monkeypatch.setattr(run_wasm, "_wasm_emitter", lambda: None)
    monkeypatch.setattr(run_wasm, "_emit_modules",
                        lambda ir, record=False, emit_module=None: {"Crasher": "(module)"})
    monkeypatch.setattr("revl.least_authority.enforce_wasm_least_authority",
                        lambda ir, policy, modules: None)
    monkeypatch.setattr(run_wasm, "wasm_runtime_reason", lambda: None)
    # the byte-level import check needs a real interpreter and real modules
    # (issue #3189); this test is about the WAL header, so it is out of scope
    monkeypatch.setattr(run_wasm, "_check_emitted_imports",
                        lambda ir, modules: None)
    monkeypatch.setattr(run_wasm.os, "fsync", _fail_fsync)
    started = []
    monkeypatch.setattr(run_wasm.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or pytest.fail("harness started"))
    rc = run_wasm.run_wasm({}, {}, [], once=True)
    assert rc == 1
    assert started == []
    assert FSYNC_FAILED in capsys.readouterr().err


class _FakeStdin:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text

    def flush(self):
        pass

    def close(self):
        pass


class _FakeHarness:
    """Stands in for the harness process: reads the run token from stdin and
    prints one token-tagged WAL frame, then the proof lines of a clean run."""

    def __init__(self, *args, **kwargs):
        self.stdin = _FakeStdin()
        self.killed = False
        self.waited = False

    @property
    def stdout(self):
        token = self.stdin.text.strip()
        frame = {"seq": 0, "receiver": "beginRow", "method": "deleteRow",
                 "witness": "row#1"}
        yield f"[wal#{token}] {json.dumps(frame)}\n"
        for word in ("UP", "NO-RESIDUE", "DOWN"):
            yield f"[run#{token}] {word}\n"

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def poll(self):
        return 0


def _wasm_run_fixture(tmp_path, monkeypatch, fail_after: int):
    """Wire `run_wasm` to a fake harness and fail every fsync after the first
    `fail_after` succeed."""
    wal = tmp_path / "run.wal"
    monkeypatch.setenv("REVL_WAL", str(wal))
    monkeypatch.delenv("REVL_WAL_ALLOW_UNSYNCED", raising=False)
    monkeypatch.setattr(run_wasm, "_load_order", lambda ir: ["Crasher"])
    monkeypatch.setattr(run_wasm, "_wasm_emitter", lambda: None)
    monkeypatch.setattr(run_wasm, "_emit_modules",
                        lambda ir, record=False, emit_module=None: {"Crasher": "(module)"})
    monkeypatch.setattr("revl.least_authority.enforce_wasm_least_authority",
                        lambda ir, policy, modules: None)
    monkeypatch.setattr(run_wasm, "wasm_runtime_reason", lambda: None)
    # out of scope here (issue #3189): the import check needs a real
    # interpreter, and the fake harness has no process group to kill
    monkeypatch.setattr(run_wasm, "_check_emitted_imports",
                        lambda ir, modules: None)
    monkeypatch.setattr(run_wasm, "_kill_process_group",
                        lambda proc: proc.kill())
    calls = []
    real_fsync = os.fsync

    def fsync(fd):
        calls.append(fd)
        if len(calls) > fail_after:
            raise OSError("injected fsync failure")
        real_fsync(fd)
    monkeypatch.setattr(run_wasm.os, "fsync", fsync)
    harnesses = []

    def popen(*args, **kwargs):
        harnesses.append(_FakeHarness())
        return harnesses[-1]
    monkeypatch.setattr(run_wasm.subprocess, "Popen", popen)
    return wal, harnesses


def _records(wal: Path) -> list[str]:
    return [json.loads(line)["record"]
            for line in wal.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_wasm_run_stops_the_harness_on_a_failed_descriptor_fsync(
        tmp_path, monkeypatch, capsys):
    wal, harnesses = _wasm_run_fixture(tmp_path, monkeypatch, fail_after=1)
    rc = run_wasm.run_wasm({}, {}, [], once=True)
    assert rc == 1
    assert len(harnesses) == 1 and harnesses[0].killed
    assert FSYNC_FAILED in capsys.readouterr().err
    # the descriptor whose fsync failed is cut back out of the file
    assert _records(wal) == ["header"]


def test_wasm_run_fails_when_the_commit_markers_are_not_durable(
        tmp_path, monkeypatch, capsys):
    """A clean harness run whose discharge marker cannot be fsynced is a failed
    run, not a committed one."""
    wal, harnesses = _wasm_run_fixture(tmp_path, monkeypatch, fail_after=2)
    rc = run_wasm.run_wasm({}, {}, [], once=True)
    assert rc == 1
    assert len(harnesses) == 1 and not harnesses[0].killed
    err = capsys.readouterr().err
    assert FSYNC_FAILED in err and "commit markers are not durable" in err
    # the discharge marker's fsync is the one that failed: it is cut back out
    # of the file (issue #2345), as the py writer does, and nothing follows it
    assert _records(wal) == ["header", "discharge-descriptor"]


def test_wasm_drain_write_failure_is_a_durability_error():
    class Full:
        def write(self, _text):
            raise OSError(28, "No space left on device")

    with pytest.raises(run_wasm.WALDurabilityError, match="write-ahead log write failed"):
        run_wasm.write_wal_record(Full(), run_wasm.wal_header())
