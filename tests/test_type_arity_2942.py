"""A declared type given the wrong number of type arguments (issue #2942).

The builtin generic heads had their arity checked (`List[Int, Int]` is
refused), but a type the program declares did not: `Mb[Int]` for
`type Mb = Jj(Int) | Nn`, or `Box[Int, Int]` for `type Box[T] = { v: T }`,
type-checked, and a match over the applied spelling missing `Nn` was admitted
because nothing in the type table has that shape. Each is now refused where it
is written, with the builtin refusal's message shape.

A declared generic written with no arguments (`Box`) is read by the checker
today and is left as it is; see `revl.type_arity`.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.parser import parse_text  # noqa: E402
from revl.type_arity import check_declared_type_arity  # noqa: E402

MB = "type Mb = Jj(Int) | Nn\n"
BOX = "type Box[T] = { v: T }\n"
PAIR = "type Pair[A, B] = { a: A, b: B }\n"

ZERO = "`Mb` takes 0 type argument(s), got 1 (`Mb[Int]`)"

REFUSED = {
    # the issue's program: a match missing `Nn` over the applied spelling
    "issue program": (MB + """
pub fn f(m: Mb[Int]) -> Int {
  return match m { Jj(x) => x }
}
""", ZERO),
    "parameter": (MB + """
pub fn f(m: Mb[Int]) -> Int {
  return match m { Jj(x) => x, Nn => 0 }
}
""", ZERO),
    "return": (MB + """
pub fn f() -> Mb[Int] { return Nn }
""", ZERO),
    "let annotation": (MB + """
pub fn f() -> Int {
  let m: Mb[Int] = Nn
  return 1
}
""", ZERO),
    "record field": (MB + """
type R = { m: Mb[Int] }
pub fn f() -> Int { return 1 }
""", ZERO),
    "variant payload": (MB + """
type W = Wr(Mb[Int]) | Em
pub fn f() -> Int { return 1 }
""", ZERO),
    "nested in a builtin": (MB + """
pub fn f(ms: List[Mb[Int]]) -> Int { return 1 }
""", "`Mb` takes 0 type argument(s), got 1 (`Mb[Int]`)"),
    "inside a function type": (MB + """
pub fn f(g: (Mb[Int]) -> Int) -> Int { return 1 }
""", ZERO),
    "alias right-hand side": (MB + """
type Al = Mb[Int]
pub fn f() -> Int { return 1 }
""", ZERO),
    "generic too many": (BOX + """
pub fn f(b: Box[Int, Int]) -> Int { return 1 }
""", "`Box` takes 1 type argument(s), got 2 (`Box[Int, Int]`)"),
    "generic too few": (PAIR + """
pub fn f(p: Pair[Int]) -> Int { return 1 }
""", "`Pair` takes 2 type argument(s), got 1 (`Pair[Int]`)"),
    "generic too many, nested": (BOX + """
pub fn f(bs: Opt[List[Box[Int, Str]]]) -> Int { return 1 }
""", "`Box` takes 1 type argument(s), got 2 (`Box[Int, Str]`)"),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_wrong_type_argument_count_is_refused(name):
    src, message = REFUSED[name]
    with pytest.raises(RevlError) as info:
        compile_source(src, "arity.rvl")
    assert message in str(info.value)


def test_zero_parameter_hint_says_it_takes_none():
    src, _ = REFUSED["parameter"]
    with pytest.raises(RevlError) as info:
        compile_source(src, "arity.rvl")
    assert "declared with no type parameters" in info.value.hint


ADMITTED = {
    # a declared generic written bare is read by the checker today; whether
    # it is a type is a separate design question, left as it is
    "generic written bare": BOX + """
pub fn f(b: Box) -> Int { return 1 }
""",
    "zero-parameter type written bare": MB + """
pub fn f(m: Mb) -> Int {
  return match m { Jj(x) => x, Nn => 0 }
}
""",
    "generic applied to its arity": BOX + PAIR + """
pub fn f(b: Box[Int], p: Pair[Int, Str], bs: List[Box[Str]]) -> Int {
  return b.v
}
""",
}


@pytest.mark.parametrize("name", sorted(ADMITTED))
def test_right_type_argument_count_still_compiles(name):
    compile_source(ADMITTED[name], "arity.rvl")


def test_type_parameter_shadows_a_declared_name():
    # inside `W` (and `g`), `Mb` is the declaration's own type parameter, not
    # the top-level `Mb`, so its arguments are not counted against `Mb`'s
    prog = parse_text(MB + """
type W[Mb] = { v: Mb[Int] }
fn g[Mb](x: Mb[Str]) -> Int { return 1 }
""", "arity.rvl")
    check_declared_type_arity(prog, "arity.rvl")
    with pytest.raises(RevlError):
        check_declared_type_arity(
            parse_text(MB + "type V[T] = { v: Mb[T] }\n", "arity.rvl"),
            "arity.rvl")


def test_imported_declaration_is_refused_in_its_own_file(tmp_path):
    # the multi-file path (`compile_files`), with the annotation in the
    # imported module: the refusal names lib.rvl and its line, not the root
    (tmp_path / "lib.rvl").write_text(
        "pub type Pb[T] = { v: T }\n"
        "\n"
        "pub fn bad(p: Pb[Int, Int]) -> Int {\n"
        "  return 1\n"
        "}\n")
    root = tmp_path / "root.rvl"
    root.write_text('use "./lib.rvl" { Pb, bad }\n'
                    "\n"
                    "pub fn f() -> Int { return 1 }\n")
    with pytest.raises(RevlError) as info:
        compile_files([str(root)])
    first = str(info.value).splitlines()[0]
    assert "`Pb` takes 1 type argument(s), got 2 (`Pb[Int, Int]`)" in first
    assert "lib.rvl:3:" in first


def test_parameterised_alias_keeps_its_own_refusal():
    # an alias may not declare parameters; that refusal names the real fault,
    # so its uses are not counted against the parameters it wrongly declares
    with pytest.raises(RevlError) as info:
        compile_source(BOX + "type A[T] = Box[T]\n"
                       "pub fn f(a: A[Int, Int]) -> Int { return 1 }\n",
                       "arity.rvl")
    assert "type alias `A` cannot declare type parameters" in str(info.value)


def test_builtin_head_left_to_its_own_check():
    # a declaration reusing a builtin head's name does not move that head
    # onto this check: `Opt[Int]` is the builtin here, as it was before
    compile_source("type Opt = Aa | Bb\n"
                   "pub fn f(x: Opt[Int]) -> Int { return 1 }\n", "arity.rvl")


def test_builtin_arity_message_unchanged():
    with pytest.raises(RevlError) as info:
        compile_source("pub fn f(b: List[Int, Int]) -> Int { return 1 }\n",
                       "arity.rvl")
    assert "`List` takes 1 type argument(s), got 2 (`List[Int, Int]`)" in str(
        info.value)
