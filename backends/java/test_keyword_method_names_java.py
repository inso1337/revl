"""A provided service method named after a Java reserved word, on the java
tier (issue #1512, the java half of what #1474 fixed on py).

WHAT WAS WRONG. Measured on main, one program per name over Java's keywords,
literals and contextual keywords (70 names): the frontend refuses 15 (they are
revl keywords too); of the 55 it admits, `revl test --backend java` passed 10,
the contextual keywords Java still accepts as method names. The other 45 split:

  * the service interface wrote the contract name verbatim (`long class(long
    x);`, which javac rejects);
  * the provider renamed the method (`class_`) and then looked the RENAMED
    spelling up in the service table, so every parameter type fell back to
    `Object` and the emitter refused the program ("type name identifier
    collides with Java/reserved name: 'Object'");
  * `_` was not escaped at all, and javac rejects it as an identifier.

And across a placement seam the bridge speaks the contract name, so a program
that compiled still failed there: the runner looked `class` up reflectively on
an interface that declares `class_`, and a proxy sent `class_` to a peer that
exports `class`.

WHAT IS EMITTED NOW. `_method_name` is the one place the Java spelling is
decided, used by the interface, the provider, a routed require's router, a call
through a required service and a lifecycle `call`; every service-table lookup
keeps the contract name. The same function generates `revlMethodName` /
`revlContractName`, which the placement runners bind to reflectively and
translate through at the seam.

The end-to-end tests need a JDK and skip without one. A skip is not a pass:
CI's `backend-java` job provisions a JDK.
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

# By path, under names nothing else binds to another file: every backend
# directory has an `emit.py`, so this tier's emitter is never the bare `emit`
# (issue #1449).
javac_gate = load_by_path("javac_gate", HERE / "javac_gate.py")
emit = load_by_path("revl_java_emit_kw", HERE / "emit.py").emit

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)

#: Java's reserved keywords (JLS 3.9, `_` included), its three literals, and
#: its contextual keywords that can spell an identifier.
JAVA_WORDS = sorted({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "final", "finally", "float", "for", "goto", "if", "implements",
    "import", "instanceof", "int", "interface", "long", "native", "new",
    "package", "private", "protected", "public", "return", "short", "static",
    "strictfp", "super", "switch", "synchronized", "this", "throw", "throws",
    "transient", "try", "void", "volatile", "while", "_",
    "true", "false", "null",
    "exports", "module", "open", "opens", "permits", "provides", "record",
    "requires", "sealed", "to", "transitive", "uses", "var", "when", "with",
    "yield",
})

#: The names the revl frontend itself refuses: they never reach an emitter.
#: Pinned, so the admitted set is exactly the rest.
FRONTEND_REFUSED = {
    "assert", "break", "continue", "else", "false", "for", "if", "null",
    "provides", "requires", "return", "true", "var", "while", "with",
}

ADMITTED = [name for name in JAVA_WORDS if name not in FRONTEND_REFUSED]


def _provider(names) -> str:
    sigs = "\n".join(f"  fn {name}(x: Int) -> Int" for name in names)
    bodies = "\n".join(f"    fn {name}(x) {{ return x + {i} }}"
                       for i, name in enumerate(names, 1))
    return (f"service S {{\n{sigs}\n}}\n"
            f"component P provides s: S {{\n  provide s {{\n{bodies}\n  }}\n}}\n")


def _program(names) -> str:
    """Every name provided by `P`, called through a required service by `C`,
    and called directly by a lifecycle `call`, each with a distinct answer."""
    total = " + ".join(f"s.{name}(x)" for name in names)
    calls = "\n".join(f"  let r{i} = call s.{name}(41)\n  assert r{i} == {41 + i}"
                      for i, name in enumerate(names, 1))
    expected = sum(41 + i for i in range(1, len(names) + 1))
    return (_provider(names)
            + "service Ops { fn run(x: Int) -> Int }\n"
            + "component C requires s: S provides ops: Ops {\n"
            + f"  provide ops {{ fn run(x) {{ return {total} }} }}\n}}\n"
            + 'lifecycle test "reserved method names" {\n  load P\n  load C\n'
            + calls + "\n"
            + f"  let all = call ops.run(41)\n  assert all == {expected}\n"
            + "  unload C\n  unload P\n  assert no_residue\n}\n")


@pytest.mark.parametrize("name", sorted(FRONTEND_REFUSED))
def test_a_revl_keyword_is_refused_by_the_frontend_not_the_emitter(name):
    with pytest.raises(RevlError):
        compile_source(_provider([name]), "kw.rvl")


@pytest.mark.parametrize("name", ADMITTED)
def test_every_admitted_name_emits(name):
    """Emission, and javac where a JDK is present: on main 44 of these were
    refused by the emitter and `_` reached javac unescaped."""
    javac_gate.compile_check(emit(compile_source(_program([name]), "kw.rvl")),
                             f"method named {name!r}")


@needs_jdk
def test_every_admitted_name_is_provided_and_called_end_to_end():
    # Runs on the real cordis4j classes where REVL_CORDIS4J_CLASSES is set
    # (CI's backend-java job) and on the in-repo stubs otherwise (issue #1888).
    status, message = RUNNERS["java"](compile_source(_program(ADMITTED), "kw.rvl"))
    assert status == "pass", message


def test_the_java_spelling_is_one_function_at_every_site():
    code = emit(compile_source(_program(["class"]), "kw.rvl"))
    assert "long class_(long x);" in code                     # the interface
    assert "public long class_(long x) {" in code             # the provider
    assert "this.s.class_(x)" in code                         # a required-service call
    assert '_revlRoot.get(ServiceKey.of(S.class, "s")).class_(41L)' in code  # a lifecycle `call`
    assert 'case "class" -> "class_";' in code                # the seam's table
    assert 'case "class_" -> "class";' in code


def test_a_name_that_needs_no_rename_is_emitted_as_before():
    code = emit(compile_source(_program(["bump"]), "plain.rvl"))
    assert "long bump(long x);" in code
    assert "s.bump(x)" in code
    assert "revlMethodName" not in code and "revlContractName" not in code


_SEAM = """\
[processes.provider]
backend = "java"
components = ["P"]

[processes.consumer]
backend = "java"
components = ["C"]
probe = ["ops.run(41)", "s.class(41)"]
"""


@needs_jdk
def test_a_keyword_method_crosses_a_placement_seam_by_its_contract_name(tmp_path):
    """Both directions of the bridge: the consumer's proxy sends the contract
    name (`class`, not `class_`), and the provider's runner resolves it to the
    declared `class_`. On main the program did not emit; with only the emitter
    fixed, the probe answered `no method class/1`."""
    source = tmp_path / "kw_seam.rvl"
    source.write_text(
        "service S { fn class(x: Int) -> Int }\n"
        "component P provides s: S { provide s { fn class(x) { return x + 1 } } }\n"
        "service Ops { fn run(x: Int) -> Int }\n"
        "component C requires s: S provides ops: Ops {\n"
        "  provide ops { fn run(x) { return s.class(x) * 10 } }\n}\n",
        encoding="utf-8")
    placement = tmp_path / "seam.toml"
    placement.write_text(_SEAM, encoding="utf-8")
    # The in-repo stub runner, even where real cordis4j classes are present
    # (CI): RealPlacementRunner is consumer-only and serves no key, so a java
    # provider process there answers nothing. Both runners translate the
    # method name through the same table.
    env = dict(os.environ, JAVA_HOME=str(Path(javac_gate.JAVA).parents[1]),
               REVL_CORDIS4J_CLASSES="")
    ran = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(source), "--placement", str(placement),
         "--once"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=900, env=env, cwd=ROOT)
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    assert "ERROR" not in out, out
    assert "ops.run(41)     | => 420" in out, out
    assert "s.class(41)     | => 42" in out, out
