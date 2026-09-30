"""A java provider on the real cordis4j runtime is refused at plan time
(issue #1581, step 1: fail closed).

WHAT WAS WRONG. `revl run --placement` runs java processes on
`RealPlacementRunner` whenever real cordis4j classes are present
(`REVL_CORDIS4J_CLASSES`, set in CI's backend-java job). That runner is
consumer-only: it proxies required keys and runs probes, but binds no socket
and serves nothing. A java process that other processes depended on booted,
printed `UP` and answered no one. Its consumer saw only
`java.net.SocketException: No such file or directory`, measured in CI run
36688386681, and the conductor reported a halted teardown. Nothing said the
provider could not serve.

WHAT IT DOES NOW. `placement.java_real_serve_refusal` refuses the placement
before any process is spawned, naming each java process and the keys it would
have to serve. A java process that serves nothing still runs, and the in-repo
stub runtime (`PlacementRunner`, which serves) is unaffected.

The refusal is decided by which runtime is SELECTED, not by what it does, so
the first test selects it with any existing directory (and any JDK standing in
for the JDK 21 selection also asks for) and runs wherever a JDK is. The last two need the real classes and run in backend-java.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
service S { fn bump(x: Int) -> Int }
component P provides s: S { provide s { fn bump(x) { return x + 1 } } }
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


def _run(tmp_path: Path, placement: str, classes: str,
         any_jdk_as_21: bool = False) -> subprocess.CompletedProcess:
    (tmp_path / "p.rvl").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "p.toml").write_text(placement, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), REVL_CORDIS4J_CLASSES=classes)
    if javac_gate.JAVA:
        env["JAVA_HOME"] = str(Path(javac_gate.JAVA).parents[1])
        if any_jdk_as_21:
            # selection also asks for a JDK 21 (`placement._find_jdk21`); the
            # refusal comes before any compile, so any working JDK stands in
            env["JAVA21_HOME"] = env["JAVA_HOME"]
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", str(tmp_path / "p.rvl"),
         "--placement", str(tmp_path / "p.toml"), "--once"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900,
        env=env, cwd=ROOT)


def _assert_refused(ran: subprocess.CompletedProcess) -> None:
    out = ran.stdout + ran.stderr
    assert ran.returncode != 0, out
    assert "placement refused: the real cordis4j runtime is selected" in out, out
    assert "process 'provider' (java) must serve 's'" in out, out
    assert "] UP" not in out, out            # nothing was spawned, nothing said UP
    assert "consumer" not in out.split("placement refused", 1)[1].split("\n")[1], out


@needs_jdk
def test_selecting_the_real_runtime_refuses_a_java_provider_before_spawning(tmp_path):
    """Any existing directory selects the real runtime; the refusal comes
    before any build or spawn, so the directory need hold nothing."""
    classes = tmp_path / "classes"
    classes.mkdir()
    _assert_refused(_run(tmp_path, TWO_PROCESSES, str(classes), any_jdk_as_21=True))


@needs_jdk
@needs_real
def test_on_real_cordis4j_a_java_provider_is_refused_and_never_says_up(tmp_path):
    _assert_refused(_run(tmp_path, TWO_PROCESSES, _REAL))


@needs_jdk
@needs_real
def test_on_real_cordis4j_a_java_process_that_serves_nothing_still_runs(tmp_path):
    ran = _run(tmp_path, ONE_PROCESS, _REAL)
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    assert "placement refused" not in out, out
    assert "real cordis4j" in out, out
    assert "ops.run(41)" in out and "=> 420" in out, out


@needs_jdk
def test_the_stub_runtime_still_serves_a_java_provider(tmp_path):
    ran = _run(tmp_path, TWO_PROCESSES, "")
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    assert "placement refused" not in out, out
    assert "=> 420" in out, out
