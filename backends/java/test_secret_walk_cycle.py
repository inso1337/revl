"""The java tier's declared-secret registry walk terminates on a cyclic value.

`revlRememberSecret` descends a declared value's container graph, the java half
of the py tier's `register_secret_tree`. A value whose container graph re-enters
itself -- a parent pointer, a self-referential record, a list that holds itself
-- drove the walk until the JVM's stack ran out, and that arrives as a
`StackOverflowError`: an `Error`, not an `Exception`, so the tier's seam-failure
shapes (which catch `Exception`) never converted it into a structured failure.

What is proved here, by COMPILING the emitted registry block and RUNNING it:
a cyclic `Object[]`, a self-referential `List`, a self-referential `Map` and an
`Optional` over the list all terminate, and the canary they hold is still
redacted afterwards. Asserting on what the run printed, never on the fact that
a redaction function was called.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import javac_gate  # noqa: E402

# `import emit` collides with the other backends' emitters when the whole
# repository is on `sys.path` in one pytest process, so load it by path under a
# name of its own.
_spec = importlib.util.spec_from_file_location("revl_java_emit_walk", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

# Long enough that an exact match means something, and not a substring of
# anything else the run prints.
CANARY = "JAVA-CYCLE-CANARY-421-F6"
REDACTED_SECRET = "<redacted:secret>"

MAIN = """
    public static void main(String[] args) {
        java.util.List<Object> list = new java.util.ArrayList<>();
        list.add("%s");
        list.add(list);
        java.util.Map<String, Object> map = new java.util.LinkedHashMap<>();
        map.put("self", map);
        map.put("leaf", "%s");
        Object deep = "%s";
        for (int i = 0; i < 40; i++) {
            java.util.List<Object> wrap = new java.util.ArrayList<>();
            wrap.add(deep);
            deep = wrap;
        }
        revlMarkSecret(list, map, java.util.Optional.of(list), deep);
        System.out.println(revlRedactText("host said %s"));
    }
""" % (CANARY, CANARY, CANARY, CANARY)

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)


@needs_jdk
def test_the_registry_walk_terminates_on_a_cyclic_value():
    source = "class Components {\n" + "\n".join(emit._emit_secret_registry()) + "\n" + MAIN + "}\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Components.java"
        path.write_text(source, encoding="utf-8")
        compiled = subprocess.run(
            [javac_gate.JAVAC, "-d", tmp, str(path)], capture_output=True, text=True
        )
        assert compiled.returncode == 0, compiled.stderr
        run = subprocess.run(
            [javac_gate.JAVA, "-cp", tmp, "Components"], capture_output=True, text=True
        )
    assert run.returncode == 0, run.stderr
    assert CANARY not in run.stdout, run.stdout
    assert REDACTED_SECRET in run.stdout, run.stdout
