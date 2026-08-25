"""Roadmap item 165: a valid revl identifier that collides with a *Java*
reserved word (`class`, `new`, `int`, `default`, …) no longer crashes the Java
emitter — it is deterministically renamed (A3 append-`_`, the same scheme
`_fn_name` already uses for callables) at the declaration site AND every use
site, so the emitted class is valid Java.

`class` is a legal revl identifier (not a revl keyword), so
`fn f(class: Str) -> Str { return class }` type-checks and lowers, then used to
die here with `parameter name identifier collides with Java/reserved name`.

The string assertions run everywhere; the javac/java gate compiles and EXECUTES
the emitted class, proving decl and use agree on the JVM (skips cleanly when no
JDK is installed, mirroring the rest of this suite).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

# reuse the sibling suite's toolchain probe + stub-compile helper
from test_emit_java import JAVA, JAVAC, STUB_SOURCES  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_java_emit_rw", HERE / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)


PROGRAM = """
type Box = { class: Str, default: Str }
fn probe(class: Str, new: Str) -> Str {
  let int = class
  return int
}
fn unbox(b: Box) -> Str { return b.class }
pub fn go(x: Str) -> Str { return probe(x, x) }
"""


def test_mangle_is_pure_and_free():
    assert emit._mangle("class") == "class_"
    assert emit._mangle("int") == "int_"
    assert emit._mangle("value") == "value"      # identity off the keyword set
    assert emit._mangle("class") not in emit._JAVA_RESERVED


def test_keyword_decls_and_uses_are_consistent():
    out = emit.emit(compile_source(PROGRAM))
    # record fields (declaration + constructor params)
    assert "public final String class_;" in out
    assert "public final String default_;" in out
    # param + local + return
    assert "public static String probe(String class_, String new_) {" in out
    assert "final var int_ = class_;" in out
    assert "return int_;" in out
    # field access + cross-fn call
    assert "return b.class_;" in out
    assert "return probe(x, x);" in out
    # no bare reserved word leaked as an identifier
    for bad in ("String class,", "(String class ", "var int =", "b.class;"):
        assert bad not in out


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason="no working JDK")
def test_java_runs_keyword_named_function(tmp_path):
    """Compile the emitted class against the stubs and EXECUTE `go` — proving
    the keyword renames are consistent on the JVM."""
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(emit.emit(compile_source(PROGRAM)), encoding="utf-8")
    driver = tmp_path / "RunReserved.java"
    driver.write_text(
        "public class RunReserved {\n"
        "  public static void main(String[] a) {\n"
        "    String r = revl.Components.go(\"payload\");\n"
        "    if (!r.equals(\"payload\")) throw new AssertionError(r);\n"
        "    System.out.println(\"RESERVED_WORDS_OK\");\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    compile_all = subprocess.run(
        [JAVAC, "--release", "21", "-d", str(out)]
        + [str(s) for s in STUB_SOURCES]
        + [str(pkg / "Components.java"), str(driver)],
        capture_output=True, text=True, timeout=600,
    )
    assert compile_all.returncode == 0, compile_all.stderr
    run = subprocess.run(
        [JAVA, "-cp", str(out), "RunReserved"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "RESERVED_WORDS_OK" in run.stdout
