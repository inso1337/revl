"""The python emitter stays the python emitter when a session also collects
another backend's tests.

Every backend directory ships a module named `emit.py`. pytest's default
import mode puts the directory of each test module it collects at the front of
`sys.path`, so a session that collected `backends/java/` after
`tests/test_crash_recovery.py` and `tests/test_phase1_bracket_fault.py` had
`backends/java` ahead of `backends/python`. revl's own `import emit`
("insert the python directory if it is absent") then loaded the JAVA emitter,
emitted java source and executed it as python, or reported java's refusal as
"the py emitter refused" (issue #1449: 13 failures, on main as well).

`revl._paths.python_backend_emitter()` puts the python directory first and
refuses an `emit` from any other file. This runs the order that failed. The
java tests are collected, which is what moves `sys.path`, and deselected,
because the collision needs only their directory and their javac runs cost
minutes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JAVA = "backends/java/test_reserved_word_idents_java.py"
VICTIMS = ("tests/test_phase1_bracket_fault.py", "tests/test_crash_recovery.py")


@pytest.mark.skipif(importlib.util.find_spec("cordis") is None,
                    reason="both victims drive a live cordis-py composition")
def test_the_python_suites_pass_after_the_java_directory_was_collected(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", f"--basetemp={tmp_path / 'run'}",
         *VICTIMS, JAVA, "--deselect", JAVA],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    assert proc.returncode == 0 and "failed" not in tail and " passed" in tail, (
        proc.stdout[-4000:] + proc.stderr[-2000:])
    assert "deselected" in tail, f"the java module was not collected: {tail}"


def test_a_foreign_emit_is_refused_rather_than_used(monkeypatch):
    """The loud half. When `emit` is already some other backend's emitter, a
    bare import would hand it on; `python_backend_emitter` refuses."""
    from revl._paths import backends_root, python_backend_emitter

    foreign = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
        "emit", backends_root() / "java" / "emit.py"))
    monkeypatch.setitem(sys.modules, "emit", foreign)
    with pytest.raises(ImportError, match="not the python backend's emitter"):
        python_backend_emitter()
