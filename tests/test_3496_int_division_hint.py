"""An `Int`-expected position that gets a `/` of two `Int`s names the integer quotient (#3496).

`/` on two `Int` operands is a `Float` (docs/arithmetic.md), so `return n / 2`
in an `Int`-returning fn is refused with T1 — and the refusal used to stop at
"expects `Int`, got `Float`", leaving the author to discover `div_floor` by
reading the stdlib (two authors porting Python tests did exactly that). When
the mismatching expression is that division, the refusal's hint now names the
integer-quotient forms. Positions that legitimately want a `Float`, and
refusals of other shapes, are unchanged.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

DIV_HINT = "`/` on two `Int`s gives a `Float`; the integer quotient is `a.div_floor(b)` (also `div_trunc`, `div_euclid`)"


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as err:
        compile_source(source, "d.rvl")
    return err.value


def test_return_of_int_division_in_int_fn_names_the_quotient():
    e = _refusal("fn half(n: Int) -> Int { return n / 2 }\n")
    assert e.code == "T1"
    assert "expects `Int`, got `Float`" in str(e)
    assert DIV_HINT in (e.hint or "")


def test_let_with_declared_int_names_the_quotient():
    e = _refusal("fn f() -> Int { let n: Int = 7 / 2 return n }\n")
    assert e.code == "T1"
    assert DIV_HINT in (e.hint or "")


def test_parenthesised_division_names_the_quotient():
    e = _refusal("fn f(a: Int, b: Int) -> Int { return (a / b) }\n")
    assert DIV_HINT in (e.hint or "")


def test_float_returning_division_is_accepted_unchanged():
    compile_source("fn f() -> Float { return 7 / 2 }\n", "d.rvl")


def test_a_float_operand_does_not_trigger_the_hint():
    e = _refusal("fn f(a: Float, b: Int) -> Int { return a / b }\n")
    assert e.code == "T1"
    assert "div_floor" not in (e.hint or "")
    e = _refusal("fn f(a: Int, b: Float) -> Int { return a / b }\n")
    assert "div_floor" not in (e.hint or "")


def test_an_unrelated_mismatch_keeps_its_hint_unchanged():
    e = _refusal("fn f(n: Int) -> Int { return n.to_str() }\n")
    assert e.code == "T1"
    assert "expects `Int`, got `Str`" in str(e)
    assert "div_floor" not in (e.hint or "")
