"""A builtin scalar written with type arguments (issue #2963).

The checker refused the wrong number of type arguments on `List` and, since
#2947, on a declared type, but `Int[Str]` type-checked: `check_type_wellformed`
read the head as `Int` and dropped the written arguments, so the annotation was
accepted and the bad spelling only surfaced when it met a plain `Int`. A scalar
takes no type arguments, so the application is refused where the annotation is
read, in the `List` message's shape.

Every scalar name the checker treats as builtin data (`_CONFIG_DATA_SCALARS`)
is covered, and so are the four written positions the issue names: a parameter,
a return type, a `let` annotation and a record field.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.typecheck import _CONFIG_DATA_SCALARS  # noqa: E402

SCALARS = sorted(_CONFIG_DATA_SCALARS)


def refusal(scalar: str, count: int, written: str) -> str:
    return f"`{scalar}` takes 0 type argument(s), got {count} (`{written}`)"


# The four positions the issue names, each with `{t}` where the written type
# goes.
POSITIONS = {
    "parameter": "pub fn f(x: {t}) -> Int {{ return 1 }}\n",
    "return": "pub fn f() -> {t} {{ return 1 }}\n",
    "let annotation": (
        "pub fn f() -> Int {{\n  let x: {t} = 1\n  return 1\n}}\n"),
    "record field": "type R = {{ x: {t} }}\npub fn f() -> Int {{ return 1 }}\n",
}


@pytest.mark.parametrize("scalar", SCALARS)
def test_every_scalar_with_an_argument_is_refused(scalar):
    written = f"{scalar}[Str]"
    with pytest.raises(RevlError) as info:
        compile_source(f"pub fn f(x: {written}) -> Int {{ return 1 }}\n",
                       "scalar_type_args.rvl")
    assert refusal(scalar, 1, written) in str(info.value)


@pytest.mark.parametrize("position", sorted(POSITIONS))
def test_refused_in_every_written_position(position):
    written = "Int[Str]"
    with pytest.raises(RevlError) as info:
        compile_source(POSITIONS[position].format(t=written),
                       "scalar_type_args.rvl")
    assert refusal("Int", 1, written) in str(info.value)


def test_the_message_names_the_scalar_count_and_written_type():
    with pytest.raises(RevlError) as info:
        compile_source("pub fn f(x: Int[Str, Bool]) -> Int { return 1 }\n",
                       "scalar_type_args.rvl")
    assert refusal("Int", 2, "Int[Str, Bool]") in str(info.value)
    assert "no type arguments" in info.value.hint


def test_a_nested_scalar_application_is_refused():
    with pytest.raises(RevlError) as info:
        compile_source("pub fn f(xs: List[Int[Str]]) -> Int { return 1 }\n",
                       "scalar_type_args.rvl")
    assert refusal("Int", 1, "Int[Str]") in str(info.value)


def test_an_alias_right_hand_side_is_refused():
    with pytest.raises(RevlError) as info:
        compile_source("type A = Int[Str]\n"
                       "pub fn f() -> Int { return 1 }\n",
                       "scalar_type_args.rvl")
    assert refusal("Int", 1, "Int[Str]") in str(info.value)


@pytest.mark.parametrize("scalar", SCALARS)
def test_a_bare_scalar_is_still_a_type(scalar):
    compile_source(f"pub fn f(x: {scalar}) -> Int {{ return 1 }}\n",
                   "scalar_type_args.rvl")


def test_a_declaration_reusing_a_scalar_name_does_not_admit_arguments():
    # the checker reads a builtin head as the builtin whatever the program
    # declares; the same rule keeps a bare `type Opt = ...` from turning a
    # written `Opt[Int]` into the declared variant (test_type_arity_2942)
    with pytest.raises(RevlError) as info:
        compile_source("type Int[T] = { v: T }\n"
                       "pub fn f(x: Int[Str]) -> Str { return x.v }\n",
                       "scalar_type_args.rvl")
    assert refusal("Int", 1, "Int[Str]") in str(info.value)
