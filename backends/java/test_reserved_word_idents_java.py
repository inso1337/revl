"""Roadmap item 165: a valid revl identifier that collides with a *Java*
reserved word (`class`, `new`, `int`, `default`, …) no longer crashes the Java
emitter — it is deterministically renamed (A3 append-`_`, the same scheme
`_fn_name` already uses for callables) at the declaration site AND every use
site, so the emitted class is valid Java.

`class` is a legal revl identifier (not a revl keyword), so
`fn f(class: Str) -> Str { return class }` type-checks and lowers, then used to
die here with `parameter name identifier collides with Java/reserved name`.

Every emission here goes through the javac gate (`javac_gate.compile_check`,
issue #154), so a string assertion is a claim about a program javac accepted —
which matters more here than anywhere else in the tier: the rename rule's real
failure mode is a COLLISION (`double` and the equally legal `double_` both
landing on `double_`), and a substring match on `final var double_ =` cannot
tell one declaration from two. javac rejects the duplicate local outright. One
test goes further and EXECUTES the class, proving decl and use agree on the JVM.
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

# the tier's one toolchain resolver + stub-compile helper
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import javac_gate  # noqa: E402

JAVA, JAVAC, STUB_SOURCES = javac_gate.JAVA, javac_gate.JAVAC, javac_gate.STUB_SOURCES

_spec = importlib.util.spec_from_file_location("revl_java_emit_rw", HERE / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)


def _emit(source: str) -> str:
    """Emit, and prove the emitted class compiles before asserting on it."""
    return javac_gate.compile_check(emit.emit(compile_source(source)),
                                    "reserved-word rename")


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
    out = _emit(PROGRAM)
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


# --------------------------------------------------------------------------
# Injectivity. `_mangle`/`_fn_name` were "append `_` while reserved": a pure
# function of the name but NOT injective — `double` and the equally legal revl
# identifier `double_` both landed on `double_`. javac catches the duplicate
# local loudly; the python tier silently captures on the same shape, so the
# rule is injective on every tier now and `_check_fn_name_collisions` becomes a
# belt-and-braces guard rather than the only line of defence.
# --------------------------------------------------------------------------


def test_renames_are_injective_over_the_reserved_ladder():
    for word in sorted(emit._JAVA_RESERVED):
        ladder = [word, word + "_", word + "__", word + "___"]
        for rename in (emit._mangle, emit._fn_name):
            images = [rename(n) for n in ladder]
            assert len(set(images)) == len(ladder), (
                f"{word!r} ladder collapsed under {rename.__name__}: {images}"
            )
            assert not set(images) & emit._JAVA_RESERVED


def test_keyword_local_does_not_collide_with_its_underscore_twin():
    out = _emit(
        'pub fn probe() -> Str {\n'
        '  let double = "PUBLIC-VALUE"\n'
        '  let double_ = "SEKRIT-CANARY-416"\n'
        '  return double\n'
        '}\n'
    )
    assert 'final var double_ = "PUBLIC-VALUE";' in out
    assert 'final var double__ = "SEKRIT-CANARY-416";' in out
    assert "return double_;" in out
    assert out.count("final var double_ =") == 1


def test_top_level_fn_pair_stays_two_methods():
    out = _emit(
        'pub fn double() -> Str { return "PUBLIC-VALUE" }\n'
        'pub fn double_() -> Str { return "SEKRIT-CANARY-416" }\n'
    )
    assert out.count("String double_()") == 1
    assert out.count("String double__()") == 1


def test_record_field_pair_stays_two_fields():
    out = _emit(
        "type Box = { double: Str, double_: Str }\n"
        "fn mk(a: Str, b: Str) -> Box { return { double: a, double_: b } }\n"
        "fn r1(b: Box) -> Str { return b.double }\n"
        "fn r2(b: Box) -> Str { return b.double_ }\n"
    )
    assert "public final String double_;" in out
    assert "public final String double__;" in out
    assert "return b.double_;" in out
    assert "return b.double__;" in out


def test_ordinary_reserved_rename_is_unchanged():
    """False-positive guard: one `_`, not two, when there is no twin."""
    out = _emit(
        "pub fn f(class: Str) -> Str { let new = class\n  return new }")
    assert "String class_" in out
    assert "final var new_ = class_;" in out
    assert "class__" not in out and "new__" not in out


def test_non_reserved_underscore_names_are_untouched():
    out = _emit(
        "pub fn g(value_: Str) -> Str { let out_ = value_\n  return out_ }")
    assert "String value_" in out
    assert "final var out_ = value_;" in out
    assert "value__" not in out and "out__" not in out
