"""The java placement runner on the real cordis4j runtime SERVES (issue #1581).

WHAT WAS WRONG. `revl run --placement` runs java processes on
`RealPlacementRunner` whenever real cordis4j classes are present
(`REVL_CORDIS4J_CLASSES`, set in CI's backend-java job). That runner was
consumer-only: it proxied required keys and ran probes, but bound no socket. A
java process other processes depended on printed `UP` and answered no one; its
consumer saw `java.net.SocketException: No such file or directory`. Step 1
(PR #1583) refused such a placement at plan time.

WHAT IT DOES NOW. The runner binds the serve socket and runs an accept loop.
Each connection hands its call to the main thread (cordis4j is single-threaded)
through the event queue the peer-death monitor already uses. The main thread
checks the served-key and declared-method allowlists, resolves the key (the
shared realm first, then the one isolating component context that provides
it), runs the method with the crossing recorded in flight, and replies. The
plan-time refusal is gone.

It also prints `DOWN` on a clean stop. Its shutdown hook used to offer STOP and
return at once, and the JVM halts when its hooks return, so the main thread was
killed mid-teardown and the conductor reported `teardown HALTED`. That failed
`test_on_real_cordis4j_a_java_process_that_serves_nothing_still_runs` in CI on
PR #1583.

Every test that needs the real classes runs in backend-java, where CI builds
them. The stub-runtime control runs wherever a JDK is.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _path in (ROOT / "src", HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import javac_gate  # noqa: E402

needs_jdk = pytest.mark.skipif(javac_gate.JAVA is None, reason=javac_gate.NO_JDK)
_REAL = os.environ.get("REVL_CORDIS4J_CLASSES", "")
needs_real = pytest.mark.skipif(
    not (_REAL and Path(_REAL).is_dir()),
    reason="needs real cordis4j classes (REVL_CORDIS4J_CLASSES); CI's backend-java job builds them")

SOURCE = """\
service S {
  fn bump(x: Int) -> Int
  fn at(i: Int) -> Int
}
component P provides s: S {
  provide s {
    fn bump(x) { return x + 1 }
    fn at(i) {
      let xs = [10, 20]
      return xs[i]
    }
  }
}
service Ops { fn run(x: Int) -> Int }
component C requires s: S provides ops: Ops {
  provide ops { fn run(x) { return s.bump(x) * 10 } }
}
"""

TWO_PROCESSES = """\
[processes.provider]
backend = "java"
components = ["P"]

[processes.consumer]
backend = "java"
components = ["C"]
probe = ["ops.run(41)"]
"""

ONE_PROCESS = """\
[processes.both]
backend = "java"
components = ["P", "C"]
probe = ["ops.run(41)"]
"""

# The provider isolates its key in a realm. On real cordis4j a realm cannot be
# read by its label (docs/contract-errata.md, "cordis4j global-realm
# divergence"), so the runner serves it from the context the component
# isolated into.
ISOLATED = SOURCE.replace(
    "component P provides s: S {\n", 'component P provides s: S {\n  isolate s in realm("wa")\n')

# Two components provide the same key, each in its own realm, in one process.
TWO_REALMS = """\
service Kv { fn who(x: Int) -> Int }
component StoreA provides kv: Kv {
  isolate kv in realm("tenant_a")
  provide kv { fn who(x) { return x + 1 } }
}
component StoreB provides kv: Kv {
  isolate kv in realm("tenant_b")
  provide kv { fn who(x) { return x + 2 } }
}
"""

TWO_REALMS_PLACEMENT = """\
[processes.stores]
backend = "java"
components = ["StoreA", "StoreB"]
probe = ["kv.who(1)"]
"""


def _env(classes: str) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), REVL_CORDIS4J_CLASSES=classes)
    if javac_gate.JAVA:
        env["JAVA_HOME"] = str(Path(javac_gate.JAVA).parents[1])
    return env


def _run(tmp_path: Path, placement: str, classes: str,
         source: str = SOURCE) -> subprocess.CompletedProcess:
    (tmp_path / "p.rvl").write_text(source, encoding="utf-8")
    (tmp_path / "p.toml").write_text(placement, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", str(tmp_path / "p.rvl"),
         "--placement", str(tmp_path / "p.toml"), "--once"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900,
        env=_env(classes), cwd=ROOT)


def _served_and_clean(ran: subprocess.CompletedProcess) -> str:
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    assert "placement refused" not in out, out
    assert "real cordis4j" in out, out
    assert "[provider] serve" in out, out
    assert "ops.run(41)" in out and "=> 420" in out, out
    assert "SocketException" not in out, out
    assert "[provider] DOWN" in out and "[consumer] DOWN" in out, out
    return out


# --------------------------------------------------------------------------
# through `revl run --placement`
# --------------------------------------------------------------------------

@needs_jdk
@needs_real
def test_on_real_cordis4j_a_java_provider_serves_a_java_consumer(tmp_path):
    """The two-process java↔java placement crosses the seam and answers 420.
    On the base this placement was refused at plan time, and before that the
    provider printed UP and the consumer hit the missing socket."""
    _served_and_clean(_run(tmp_path, TWO_PROCESSES, _REAL))


@needs_jdk
@needs_real
def test_on_real_cordis4j_an_isolated_provider_is_served_from_its_realm(tmp_path):
    """The served key lives only in the context the provider isolated into;
    the shared realm does not hold it."""
    _served_and_clean(_run(tmp_path, TWO_PROCESSES, _REAL, ISOLATED))


@needs_jdk
@needs_real
def test_on_real_cordis4j_a_key_in_two_realms_is_refused_by_name(tmp_path):
    ran = _run(tmp_path, TWO_REALMS_PLACEMENT, _REAL, TWO_REALMS)
    out = ran.stdout + ran.stderr
    assert "kv.who(1)" in out, out
    assert ("key 'kv' is provided in 2 realms (`StoreA` in realm `tenant_a`, "
            "`StoreB` in realm `tenant_b`); a call names a key, not a realm") in out, out
    assert "=> 2" not in out and "=> 3" not in out, out


@needs_jdk
@needs_real
def test_on_real_cordis4j_a_java_process_that_serves_nothing_still_runs(tmp_path):
    """Unaffected by serving, and now it also says DOWN on the clean stop."""
    ran = _run(tmp_path, ONE_PROCESS, _REAL)
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    assert "real cordis4j" in out, out
    assert "ops.run(41)" in out and "=> 420" in out, out
    assert "[both] DOWN" in out, out


@needs_jdk
def test_the_stub_runtime_still_serves_a_java_provider(tmp_path):
    out = _run(tmp_path, TWO_PROCESSES, "").stdout
    assert "=> 420" in out, out


# --------------------------------------------------------------------------
# the runner on its own, with a raw client on its socket
# --------------------------------------------------------------------------

class _Provider:
    """`RealPlacementRunner` serving `s` from a hand-written spec, built the way
    the conductor builds it, and a line reader on its stdout."""

    def __init__(self, tmp_path: Path, latch: Path | None = None):
        from revl import compile_source  # noqa: PLC0415
        from revl import placement  # noqa: PLC0415

        ir = compile_source(SOURCE, "p.rvl")
        jdk_bin = str(Path(javac_gate.JAVA).parent)
        out = placement._build_java_real(ir, tmp_path, jdk_bin, _REAL)
        # a unix socket path is capped near 104 bytes, and pytest's tmp_path on
        # macOS is longer than that, so the socket lives in a short dir
        self.sockdir = tempfile.mkdtemp(prefix="r1581", dir="/tmp")
        self.sock = os.path.join(self.sockdir, "s.sock")
        spec = {
            "name": "provider", "module": "revl.Components", "components": ["P"],
            "ifaces": {"s": "revl.Components$S"},
            "serve": {"socket": self.sock, "keys": ["s"], "methods": {"s": ["at", "bump"]}},
        }
        if latch is not None:
            spec["estopLatch"] = str(latch)
        (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(Path(jdk_bin) / "java"), "-cp", f"{_REAL}{os.pathsep}{out}",
             "RealPlacementRunner", str(tmp_path / "spec.json")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        self.wait_for("[provider] UP")

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def wait_for(self, text: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(text in line for line in self.lines):
                return
            if self.proc.poll() is not None and not any(text in l for l in self.lines):
                time.sleep(0.2)
                if not any(text in l for l in self.lines):
                    break
            time.sleep(0.05)
        raise AssertionError(f"never saw {text!r}:\n" + "\n".join(self.lines))

    def call(self, key: str, method: str, args: list) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(30)
            conn.connect(self.sock)
            conn.sendall((json.dumps({"key": key, "method": method, "args": args}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        shutil.rmtree(self.sockdir, ignore_errors=True)


@pytest.fixture
def provider(tmp_path):
    p = _Provider(tmp_path)
    yield p
    p.close()


@needs_jdk
@needs_real
def test_the_runner_answers_a_declared_call(provider):
    assert provider.call("s", "bump", [41]) == {"ok": True, "value": 42}


@needs_jdk
@needs_real
def test_the_runner_answers_calls_from_concurrent_connections(provider):
    """Many connection threads, one main thread: every call is answered and
    each answer is its own."""
    results: dict[int, dict] = {}

    def one(i: int):
        results[i] = provider.call("s", "bump", [i])

    threads = [threading.Thread(target=one, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results == {i: {"ok": True, "value": i + 1} for i in range(16)}


@needs_jdk
@needs_real
def test_a_key_this_process_does_not_serve_is_refused(provider):
    assert provider.call("nope", "bump", [1]) == {
        "ok": False, "error": "key 'nope' is not exported by this process"}


@needs_jdk
@needs_real
def test_a_method_the_service_does_not_declare_is_refused(provider):
    """`hashCode` is a method of the java object the key resolves to, and not
    an operation the service declares, so the allowlist refuses it before any
    reflection runs."""
    assert provider.call("s", "hashCode", []) == {
        "ok": False, "error": "method 'hashCode' is not exported for key 's' (exported: at, bump)"}


@needs_jdk
@needs_real
def test_a_provider_failure_is_a_reply_not_a_crash(provider):
    reply = provider.call("s", "at", [7])
    assert reply["ok"] is False
    assert "Exception" in reply["error"], reply
    # and the runner still serves
    assert provider.call("s", "at", [1]) == {"ok": True, "value": 20}


@needs_jdk
@needs_real
def test_a_clean_stop_says_down_and_removes_the_socket(provider):
    provider.call("s", "bump", [1])
    provider.proc.terminate()
    # 143 is the JVM's own status for a SIGTERM it handled with its hooks
    assert provider.proc.wait(timeout=60) in (0, 143), "\n".join(provider.lines)
    provider.wait_for("[provider] DOWN", timeout=5)
    assert not Path(provider.sock).exists()


@needs_jdk
@needs_real
def test_an_armed_latch_halts_a_serving_runner_without_down(tmp_path):
    latch = tmp_path / "estop.latch"
    p = _Provider(tmp_path, latch=latch)
    try:
        assert p.call("s", "bump", [1]) == {"ok": True, "value": 2}
        latch.write_text(json.dumps({"by": "test", "reason": "drill"}), encoding="utf-8")
        assert p.proc.wait(timeout=60) != 0
        p.wait_for("[provider] HALTED", timeout=5)
        assert not any("[provider] DOWN" in line for line in p.lines), p.lines
    finally:
        p.close()
