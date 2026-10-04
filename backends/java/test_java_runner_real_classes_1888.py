"""`revl test --backend java` runs against REAL cordis4j classes when
`REVL_CORDIS4J_CLASSES` names them (issue #1888).

The runner compiled the plugin with the real classes on `-cp`, then compiled
and ran `RunRevlTests` with only its output directory. The real classes are
not copied there (the stubs are, so the stub path hid it), so the JVM stopped
on `NoClassDefFoundError: io/cordis4j/core/Plugin` before any test ran.

With the classpath fixed, a second difference showed: a lifecycle `call` read
the provision with the type-only `get(<Svc>.class)`. The stubs answered that
for a keyed provision; real cordis4j does not (`NoSuchServiceException`), so
the emitted lookup now names the key the provider registered,
`get(ServiceKey.of(<Svc>.class, "<key>"))`.

CI's `backend-java` job runs `pytest backends/java/` with the variable set,
so the end-to-end test below runs there; elsewhere it skips, and the
classpath test runs everywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
# tests/ is APPENDED, so its modules resolve only names nothing earlier on
# sys.path provides; `_load_by_path` is the one wanted here.
if str(ROOT / "tests") not in sys.path:
    sys.path.append(str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402

# by path, under names nothing else binds (every backend has an `emit.py`)
javac_gate = load_by_path("javac_gate", HERE / "javac_gate.py")
emit = load_by_path("revl_java_emit_1888", HERE / "emit.py").emit

from revl import test as revl_test  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

REAL = os.environ.get("REVL_CORDIS4J_CLASSES")

SOURCE = """
service Counter { fn bump(x: Int) -> Int }
component C provides counter: Counter {
  provide counter { fn bump(x) { return x + 1 } }
}
lifecycle test "bump then unload" {
  load C
  let n = call counter.bump(41)
  assert n == 42
  unload C
  assert no_residue
}
"""


@pytest.mark.skipif(javac_gate.JAVAC is None or not REAL,
                    reason="needs a JDK and REVL_CORDIS4J_CLASSES (compiled "
                           "cordis4j-core classes), as CI's backend-java job has")
def test_a_lifecycle_test_runs_on_the_real_cordis4j_classes():
    status, detail = revl_test.RUNNERS["java"](compile_source(SOURCE, "c.rvl"))
    assert status == "pass", detail
    assert "1 lifecycle test(s)" in detail


def test_every_runner_step_has_the_real_classes_on_its_classpath(monkeypatch,
                                                                 tmp_path):
    """The three javac/java steps, captured: each has the real classes on its
    `-cp`. Runs with no JDK and no cordis4j: the toolchain and the processes
    are stand-ins, so this pins the commands, not the JVM."""
    real = str(tmp_path / "cordis4j-classes")
    monkeypatch.setenv("REVL_CORDIS4J_CLASSES", real)
    monkeypatch.setattr(revl_test, "_java_toolchain", lambda: ("javac", "java"))
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        stdout = "REVL_TESTS_OK\n" if argv[0] == "java" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(revl_test.subprocess, "run", fake_run)
    status, detail = revl_test.RUNNERS["java"](compile_source(SOURCE, "c.rvl"))
    assert status == "pass", detail
    steps = [c for c in calls if c[0] in ("javac", "java")]
    assert [c[0] for c in steps] == ["javac", "javac", "java"]
    for argv in steps:
        cp = argv[argv.index("-cp") + 1].split(os.pathsep)
        assert real in cp, argv


def test_a_lifecycle_call_reads_the_provision_by_its_key():
    code = emit(compile_source(SOURCE, "c.rvl"))
    assert '_revlRoot.get(ServiceKey.of(Counter.class, "counter")).bump(41L)' in code
    assert "_revlRoot.get(Counter.class)" not in code
