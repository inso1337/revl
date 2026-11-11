"""Float `/` on the python tier keeps its nonzero case inline (issue #3408).

Every `/` was emitted as a call, `_revl_div(a, b)`, so a game that divides 3300
times a frame paid a python frame 3300 times. The nonzero case is now written
inline and the call stays as the tail for a zero divisor, so the IEEE answer is
still built in exactly one place. An operand that is not a name or a literal is
bound once: evaluated once, and the dividend before the divisor, as the call
did.

Every test here goes through `_emit`, which refuses the old shape (a `return`
that IS the call), so each one fails on a tree without the change. The
behavioural half (the answers, the zero divisor, the evaluation count and order)
is what the change must not move; the shape is what it moves.
"""

import math
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

PY = backend_emitter("python")

SOURCE = """
pub fn names(a: Float, b: Float) -> Float { return a / b }
pub fn lit(a: Float) -> Float { return a / 2.0 }
pub fn zero_lit(a: Float) -> Float { return a / 0.0 }
pub fn lit_over(b: Float) -> Float { return 1.0 / b }
pub fn cplx_name(a: Float, b: Float, c: Float) -> Float { return a * b / c }
pub fn name_cplx(a: Float, b: Float, c: Float) -> Float { return a / (b * c) }
pub fn cplx_cplx(a: Float, b: Float, c: Float, d: Float) -> Float { return (a - b) / (c - d) }
pub fn chain(a: Float, b: Float, c: Float) -> Float { return a / b / c }
pub fn chain_deep(a: Float, b: Float) -> Float { return a * b / (a - b) / (b + 1.0) / (a + 1.0) }
pub fn ints(a: Int, b: Int) -> Float { return a / b }
pub fn num() -> Float { var s = 0.0 for (i of [1, 2, 3]) { s = s + 2.0 } return s }
pub fn den() -> Float { var s = 0.0 for (i of [1, 2, 3]) { s = s + 1.0 } return s }
pub fn call_call() -> Float { return num() / den() }
pub fn call_name(b: Float) -> Float { return num() / b }
pub fn name_call(a: Float) -> Float { return a / den() }
pub fn call_lit() -> Float { return num() / 2.0 }
pub fn call_chain() -> Float { return num() / den() / den() }
"""

GRID = [0.0, -0.0, 1.5, -2.5, 7.0, math.inf, -math.inf, math.nan]


def _emit() -> str:
    code = PY.emit(compile_source(SOURCE, "inline_div_3408.rvl"))
    for line in code.splitlines():
        # the old shape: a `return` that IS the call (the helper's own `def`
        # and its tail inside the conditional do not start this way)
        # (a literal zero divisor is decided at emit time and keeps the call)
        if line.strip() == "return _revl_div(a, 0.0)":
            continue
        assert not line.strip().startswith("return _revl_div("), line
    return code


def _module(code: str) -> dict:
    ns: dict = {}
    exec(compile(code, "inline_div_3408.py", "exec"), ns)
    return ns


def _same(x: float, y: float) -> bool:
    if math.isnan(x) or math.isnan(y):
        return math.isnan(x) and math.isnan(y)
    return x == y and math.copysign(1.0, x) == math.copysign(1.0, y)


def test_a_division_of_two_names_is_inline_with_the_call_as_its_tail():
    code = _emit()
    assert "return (a / b if b else _revl_div(a, b))" in code


def test_a_nonzero_literal_divisor_needs_no_test_and_a_zero_one_keeps_the_call():
    code = _emit()
    assert "return (a / 2.0)" in code
    assert "return _revl_div(a, 0.0)" in code


def test_an_evaluated_operand_is_bound_not_repeated_in_a_chain():
    code = _emit()
    deep = code.split("def chain_deep")[1].split("\ndef ")[0]
    # one binding per evaluated operand: the dividend text appears once per
    # level it is written, not doubled at each level
    assert len(deep) < 1500, len(deep)
    assert deep.count("_revl_dn :=") >= 2


@pytest.mark.parametrize("a", GRID)
@pytest.mark.parametrize("b", GRID)
def test_every_shape_answers_what_the_helper_answers(a, b):
    ns = _module(_emit())
    div = ns["_revl_div"]
    c, d = 2.5, 0.5
    cases = {
        "names": (a, b, div(a, b)),
        "lit": (a, div(a, 2.0)),
        "zero_lit": (a, div(a, 0.0)),
        "lit_over": (b, div(1.0, b)),
        "cplx_name": (a, c, b, div(a * c, b)),
        "name_cplx": (a, b, c, div(a, b * c)),
        "cplx_cplx": (a, b, c, d, div(a - b, c - d)),
        "chain": (a, b, c, div(div(a, b), c)),
    }
    for fn, spec in cases.items():
        *args, want = spec
        try:
            got = ns[fn](*args)
        except Exception as e:  # a trap must be the same as the call's
            raise AssertionError(f"{fn}{tuple(args)} raised {e!r}")
        assert _same(got, want), (fn, args, got, want)


def test_the_zero_divisor_values_are_ieee():
    ns = _module(_emit())
    assert ns["names"](1.0, 0.0) == math.inf
    assert ns["names"](-1.0, 0.0) == -math.inf
    assert ns["names"](1.0, -0.0) == -math.inf
    assert math.isnan(ns["names"](0.0, 0.0))
    assert math.isnan(ns["names"](math.nan, 0.0))
    assert ns["cplx_cplx"](3.0, 1.0, 2.0, 2.0) == math.inf
    assert ns["name_cplx"](-3.0, 0.0, 1.0) == -math.inf
    assert ns["ints"](7, 2) == 3.5
    assert ns["ints"](1, 0) == math.inf


@pytest.mark.parametrize("fn,args,expect", [
    ("call_call", (), ["num", "den"]),
    ("call_name", (3.0,), ["num"]),
    ("name_call", (6.0,), ["den"]),
    ("call_lit", (), ["num"]),
    ("call_chain", (), ["num", "den", "den"]),
])
def test_an_operand_that_is_a_call_runs_once_and_in_order(fn, args, expect):
    ns = _module(_emit())
    log: list = []
    real_num, real_den = ns["num"], ns["den"]
    ns["num"] = lambda: (log.append("num"), real_num())[1]
    ns["den"] = lambda: (log.append("den"), real_den())[1]
    got = ns[fn](*args)
    assert log == expect
    assert got == {"call_call": 2.0, "call_name": 2.0, "name_call": 2.0,
                   "call_lit": 3.0, "call_chain": 0.6666666666666666}[fn]


def test_a_zero_divisor_that_is_a_call_still_runs_each_operand_once():
    ns = _module(_emit())
    log: list = []
    ns["num"] = lambda: (log.append("num"), 6.0)[1]
    ns["den"] = lambda: (log.append("den"), 0.0)[1]
    assert ns["call_call"]() == math.inf
    assert log == ["num", "den"]
