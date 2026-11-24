"""Issue #3496: when an `Int` is expected and the expression is a `/` of two
`Int` operands, the refusal's hint names the integer-quotient spellings
(`a.div_floor(b)`, also `div_trunc`, `div_euclid`). The hint rides the
hint field, not the message, so the gate/reference census (which compares
code+message) is untouched.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

DIV_HINT = ("`/` on two `Int`s gives a `Float` — the integer quotient is "
            "`a.div_floor(b)` (also `div_trunc`, `div_euclid`)")


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "d.rvl")
    return excinfo.value


def test_int_over_int_into_an_int_return_names_div_floor():
    err = _refusal("fn half(n: Int) -> Int { return n / 2 }\n")
    assert "this function's return expects `Int`, got `Float`" in err.message
    assert DIV_HINT == err.hint


def test_int_over_int_into_an_int_let_names_div_floor():
    err = _refusal('test "t" {\n  let n: Int = 7 / 2\n  assert n == 3\n}\n')
    assert "`let n: Int` expects `Int`, got `Float`" in err.message
    assert DIV_HINT == err.hint


def test_a_float_operand_gets_no_div_hint():
    err = _refusal("fn f(n: Int) -> Int { return (n + 0.5) / 2 }\n")
    assert "expects `Int`, got `Float`" in err.message
    assert err.hint != DIV_HINT


def test_a_non_int_expected_type_gets_no_div_hint():
    err = _refusal("fn f(n: Int) -> Str { return n / 2 }\n")
    assert "expects `Str`, got `Float`" in err.message
    assert err.hint != DIV_HINT


def test_int_over_int_into_a_float_position_still_compiles():
    ir = compile_source("fn f(n: Int) -> Float { return n / 2 }\n", "d.rvl")
    assert ir is not None
