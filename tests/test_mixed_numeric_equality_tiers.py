"""Issue #2715: an `Int`/`Float` mix NESTED inside a compared value.

A bare mixed operand (`x == 3.0` with `x: Int`) widens: the frontend marks the
`Int` operand with `"widen": "Float"` and every tier compares numbers
(tests/test_cross_tier_execution.py, probe "Int and Float equality agree").

The same mix one level down did not. `compatible` recurses elementwise, so the
checker accepted `List[Int] == List[Float]`, `Opt[Int] == Opt[Float]`, a record
literal with an `Int` in a `Float` field, a generic record instantiated both
ways, and `Opt[Float] == Int` through the `Opt` injection. No marker can reach
an element, so the tiers disagreed: python answered true, TypeScript compared
`3n` with `3` inside `revlEq` and answered false, go did not build (`[]int64`
against `[]float64`).

The rule (docs/arithmetic.md, "`Int` widens into `Float`, and the IR now says
where", paragraph "Only a bare operand widens"): only a bare scalar operand of
`==`/`!=` widens. A mix between the two operand TYPES, inside a list, an
option, a record or a type argument, is refused at check time, so no tier
reaches it. The agreement test below runs each program on every tier this
machine has and compares its outcome with python's, so it fails on base
wherever a tier answered differently.

What this does NOT cover: an `Int` put into a `Float` slot BEFORE the
comparison (`let xs: List[Float] = [1, 2]`, `A(3)` for `A(Float)`, `Some(3)`
into an `Opt[Float]` parameter). Both operands are then `Float`-typed, the
checker has nothing to refuse, and the typed slot did not convert the nested
`Int`. That is issue #2937; its shapes are the strict xfail rows at the end,
so they flip to a failure (XPASS) when it lands.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

REFUSAL = "is not widened"

NESTED = {
    "list parameter, Int on the left": """
pub fn f(a: List[Int], b: List[Float]) -> Bool { return a == b }
test "t" { assert f([1, 2], [1.0, 2.0]) }
""",
    "list parameter, Int on the right": """
pub fn f(a: List[Float], b: List[Int]) -> Bool { return a == b }
test "t" { assert f([1.0, 2.0], [1, 2]) }
""",
    "list parameter under !=": """
pub fn f(a: List[Int], b: List[Float]) -> Bool { return a != b }
test "t" { assert !f([1, 2], [1.0, 2.0]) }
""",
    "list literals": """
test "t" { assert [1, 2] == [1.0, 2.0] }
""",
    "nested lists": """
pub fn f(a: List[List[Int]], b: List[List[Float]]) -> Bool { return a == b }
test "t" { assert f([[1]], [[1.0]]) }
""",
    "option parameters": """
pub fn f(a: Opt[Int], b: Opt[Float]) -> Bool { return a == b }
test "t" { assert f(Some(3), Some(3.0)) }
""",
    "option literals": """
test "t" { assert Some(3) == Some(3.0) }
""",
    "an Int injected into an Opt[Float]": """
pub fn f(o: Opt[Float]) -> Bool { return o == 3 }
test "t" { assert f(Some(3.0)) }
""",
    "an Int injected into an Opt[Float], flipped": """
pub fn f(o: Opt[Float]) -> Bool { return 3 != o }
test "t" { assert !f(Some(3.0)) }
""",
    "a record literal with an Int in a Float field": """
type Row = { i: Int, f: Float }
pub fn f(r: Row) -> Bool { return r == { i: 3, f: 3 } }
test "t" { assert f({ i: 3, f: 3.0 }) }
""",
    "a generic record instantiated both ways": """
type Box[T] = { v: T }
pub fn f(a: Box[Int], b: Box[Float]) -> Bool { return a == b }
test "t" { assert f({ v: 3 }, { v: 3.0 }) }
""",
    "Int32 elements against Int elements": """
pub fn f(a: List[Int32], b: List[Int]) -> Bool { return a == b }
test "t" { assert f([3.to_int32()], [3]) }
""",
}

# Comparisons the rule must keep: a bare mixed scalar widens, and a composite
# of ONE element type compares structurally as before.
KEPT = {
    "a bare Int against a Float": "pub fn f(x: Int) -> Bool { return x == 3.0 }",
    "a bare Float against an Int": "pub fn f(x: Int) -> Bool { return 3.0 != x }",
    "a record field read out": (
        "type Row = { i: Int, f: Float }\n"
        "pub fn f(r: Row) -> Bool { return r.i == r.f }"),
    "a list element read out":
        "pub fn f(xs: List[Int]) -> Bool { return xs[0] == 3.0 }",
    "same-type lists": "pub fn f(a: List[Int]) -> Bool { return a == [1, 2] }",
    "same-type options": "pub fn f(a: Opt[Int]) -> Bool { return a == Some(3) }",
    "an Int injected into an Opt[Int]":
        "pub fn f(a: Opt[Int]) -> Bool { return a == 3 }",
    "a Float record literal": (
        "type Row = { i: Int, f: Float }\n"
        "pub fn f(r: Row) -> Bool { return r == { i: 3, f: 3.0 } }"),
}

FAST_TIERS = ("ts", "go")
SLOW_TIERS = ("rust", "java")


def _outcome(tier: str, source: str) -> str:
    """`refused` (the checker said no), `pass`, `fail` or `skip`."""
    try:
        ir = compile_source(source, "mixed_eq.rvl")
    except RevlError as exc:
        return "refused" if REFUSAL in str(exc) else f"refused: {exc}"
    status, message = RUNNERS[tier](ir)
    return status if status != "skip" else f"skip: {message}"


_PY: dict[str, str] = {}


def _agrees_with_python(tier: str, name: str) -> None:
    if name not in _PY:
        _PY[name] = _outcome("py", NESTED[name])
    got = _outcome(tier, NESTED[name])
    if got.startswith("skip"):
        pytest.skip(f"{tier}: {got}")
    assert got == _PY[name], (
        f"{name!r}: python answered {_PY[name]!r}, {tier} answered {got!r}; "
        "a nested Int/Float equality must mean one thing on every tier")


@pytest.mark.parametrize("name", sorted(NESTED))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_nested_mixed_equality_agrees_with_python(tier: str, name: str):
    _agrees_with_python(tier, name)


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(NESTED))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_nested_mixed_equality_agrees_with_python_slow(tier: str, name: str):
    _agrees_with_python(tier, name)


@pytest.mark.parametrize("name", sorted(NESTED))
def test_nested_mixed_equality_is_refused_by_name(name: str):
    with pytest.raises(RevlError) as info:
        compile_source(NESTED[name], "mixed_eq.rvl")
    err = info.value
    assert REFUSAL in str(err), str(err)
    assert err.code == "T1" and err.category == "type-mismatch"
    assert err.hint and "bare" in err.hint


@pytest.mark.parametrize("name", sorted(KEPT))
def test_bare_and_same_type_equality_still_checks(name: str):
    compile_source(KEPT[name], "mixed_eq.rvl")


# Issue #2937: the typed-slot widening. Each program is ACCEPTED (both operands
# are `Float`-typed) and diverges because the slot kept a nested `Int`. The
# set is the tiers measured to disagree with python on origin/main (py, ts, go
# on 2026-10-25); a strict xfail turns into a failure once #2937 fixes them.
TYPED_SLOT = {
    "a List[Float] let holding Int literals": ("""
pub fn f() -> Bool {
  let xs: List[Float] = [1, 2]
  return xs == [1.0, 2.0]
}
test "t" { assert f() }
""", {"ts", "go"}),
    "a Float variant payload built from an Int": ("""
type S = A(Float) | B
test "t" { assert A(3) == A(3.0) }
""", {"ts"}),
    "a Row argument with an Int in its Float field": ("""
type Row = { i: Int, f: Float }
pub fn f(r: Row) -> Bool { return r == { i: 3, f: 3.0 } }
test "t" { assert f({ i: 3, f: 3 }) }
""", {"ts"}),
    "an Opt[Float] argument built from Some(3)": ("""
pub fn g(o: Opt[Float]) -> Bool { return o == Some(3.0) }
test "t" { assert g(Some(3)) }
""", {"ts"}),
    "a List[Float] argument built from [3]": ("""
pub fn g(xs: List[Float]) -> Bool { return xs == [3.0] }
test "t" { assert g([3]) }
""", {"ts"}),
}


def _typed_slot_params():
    for name in sorted(TYPED_SLOT):
        for tier in FAST_TIERS:
            marks = ()
            if tier in TYPED_SLOT[name][1]:
                marks = pytest.mark.xfail(
                    strict=True,
                    reason="issue #2937: a typed slot does not convert a "
                           "nested Int")
            yield pytest.param(tier, name, marks=marks, id=f"{tier}-{name}")


@pytest.mark.parametrize("tier, name", list(_typed_slot_params()))
def test_typed_slot_nested_int_agrees_with_python(tier: str, name: str):
    source = TYPED_SLOT[name][0]
    want = _outcome("py", source)
    got = _outcome(tier, source)
    if got.startswith("skip"):
        pytest.skip(f"{tier}: {got}")
    assert want == "pass", f"{name!r}: python answered {want!r}"
    assert got == want, (
        f"{name!r}: python answered {want!r}, {tier} answered {got!r}")
