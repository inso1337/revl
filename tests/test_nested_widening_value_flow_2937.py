"""Issue #2937: a value whose type nests a widening, flowing into a slot.

`compatible` recurses into type arguments, so the checker accepted a
`List[Int]` value where a `List[Float]` is declared (and `Opt[Int]` for
`Opt[Float]`, a record or a generic record the same way, `List[Int32]` for
`List[Int]`). The `"widen"` marker sits on one node and cannot reach an
element, so nothing converted the elements: python and TypeScript ran the
program (TypeScript with `bigint` elements where `number` is declared), go
failed to build (`[]int64` as `[]float64`) and java failed in javac
(`List<Long>` as `List<Double>`).

The rule (docs/arithmetic.md, "A value that already carries its type"): at a
call argument, a `let`, an assignment, a `return`, a record field, a list
element and a constructor payload, a bare scalar widens and a constructor
written at the site converts its own parts (#2938), but a value that carries a
nested widening in its declared type is refused at check time, so no tier
reaches it. Converting would copy every value that crosses a call.

The agreement test runs each program on every tier this machine has and
asserts that the checker refuses it, so no tier runs it; on base python and
TypeScript run the program and go and java do not build it.
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

REFUSAL = "only a bare value widens"

TYPES = """
type Row = { i: Int, f: Float }
type Box[T] = { v: T }
type Shape = Dot(Float) | Line(List[Float])
type Wrap[T] = W(T) | Nothing
"""

REFUSED = {
    "the issue's List[Int] argument": """
fn total(xs: List[Float]) -> Float {
  var s = 0.0
  for (x of xs) { s = s + x }
  return s
}
fn main() -> Float {
  let ns: List[Int] = [1, 2]
  return total(ns)
}
test "sums to 3.0" { assert main() == 3.0 }
""",
    "the issue's list read through a call": """
pub fn h(xs: List[Float]) -> Float { return xs[0] / 2.0 }
pub fn k(ys: List[Int]) -> Float { return h(ys) }
test "halves" { assert k([3]) == 1.5 }
""",
    "an Opt[Int] argument": """
pub fn g(o: Opt[Float]) -> Bool { return o == Some(3.0) }
pub fn k(p: Opt[Int]) -> Bool { return g(p) }
test "t" { assert k(Some(3)) }
""",
    "an Int value injected into an Opt[Float]": """
pub fn g(o: Opt[Float]) -> Bool { return o == Some(3.0) }
pub fn k(n: Int) -> Bool { return g(n) }
test "t" { assert k(3) }
""",
    "a record argument": TYPES + """
pub fn r(b: Box[Float]) -> Float { return b.v }
pub fn k(b: Box[Int]) -> Float { return r(b) }
test "t" { assert k({ v: 3 }) == 3.0 }
""",
    "a generic record holding a list": TYPES + """
pub fn r(b: Box[List[Float]]) -> Float { return b.v[0] }
pub fn k(b: Box[List[Int]]) -> Float { return r(b) }
test "t" { assert k({ v: [3] }) == 3.0 }
""",
    "an Int32 list into an Int list": """
pub fn s(xs: List[Int]) -> Int { return xs[0] }
pub fn k(ys: List[Int32]) -> Int { return s(ys) }
test "t" { assert k([3.to_int32()]) == 3 }
""",
    "nested lists": """
pub fn s(xs: List[List[Float]]) -> Float { return xs[0][0] }
pub fn k(ys: List[List[Int]]) -> Float { return s(ys) }
test "t" { assert k([[3]]) == 3.0 }
""",
    "a list value inside a list literal": """
pub fn s(xs: List[List[Float]]) -> Float { return xs[0][0] }
pub fn k(ys: List[Int]) -> Float { return s([ys]) }
test "t" { assert k([3]) == 3.0 }
""",
    "a list value inside Some": """
pub fn s(o: Opt[List[Float]]) -> Bool { return o == Some([3.0]) }
pub fn k(ys: List[Int]) -> Bool { return s(Some(ys)) }
test "t" { assert k([3]) }
""",
    "a let": """
pub fn k(ys: List[Int]) -> Float {
  let xs: List[Float] = ys
  return xs[0]
}
test "t" { assert k([3]) == 3.0 }
""",
    "a return": """
pub fn k(ys: List[Int]) -> List[Float] { return ys }
test "t" { assert k([3]) == [3.0] }
""",
    "an assignment": """
pub fn k(ys: List[Int]) -> Float {
  var xs: List[Float] = [1.0]
  xs = ys
  return xs[0]
}
test "t" { assert k([3]) == 3.0 }
""",
    "a record field": TYPES + """
pub fn k(ys: List[Int]) -> Box[List[Float]] { return { v: ys } }
test "t" { assert k([3]).v[0] == 3.0 }
""",
    "a variant payload": TYPES + """
pub fn k(ys: List[Int]) -> Shape { return Line(ys) }
test "t" { assert k([3]) == Line([3.0]) }
""",
    "a Result value": """
pub fn k(ys: List[Int]) -> Result[List[Float], Str] { return Ok(ys) }
test "t" { assert k([3]) == Ok([3.0]) }
""",
    "an Opt[Int] returned as Opt[Float]": """
pub fn k(o: Opt[Int]) -> Opt[Float] { return o }
test "t" { assert k(Some(3)) == Some(3.0) }
""",
    "a generic function argument": """
pub fn pick[T](a: List[T], b: List[Float]) -> List[Float] { return b }
pub fn k(ys: List[Int]) -> List[Float] { return pick(ys, ys) }
test "t" { assert k([3]) == [3.0] }
""",
    "a generic variant argument": TYPES + """
pub fn u(w: Wrap[List[Float]]) -> Bool { return true }
pub fn k(ys: List[Int]) -> Bool { return u(W(ys)) }
test "t" { assert k([3]) }
""",
    "a function value argument": """
pub fn k(ys: List[Int]) -> Float {
  let g: (List[Float]) -> Float = xs => xs[0]
  return g(ys)
}
test "t" { assert k([3]) == 3.0 }
""",
}

# What the rule must keep: a bare scalar widens, a constructor written at the
# site is not a value (its parts are judged on their own), and a value of the
# slot's own type is untouched.
KEPT = {
    "a bare Int into a Float parameter": """
pub fn f(x: Float) -> Float { return x }
pub fn k(n: Int) -> Float { return f(n) }
""",
    "a list literal argument": """
pub fn t(xs: List[Float]) -> Float { return xs[0] }
pub fn k() -> Float { return t([1, 2]) }
""",
    "a nested list literal argument": """
pub fn t(xs: List[List[Float]]) -> Float { return xs[0][0] }
pub fn k() -> Float { return t([[1, 2]]) }
""",
    "an empty list argument": """
pub fn t(xs: List[Float]) -> Float { return 1.0 }
pub fn k() -> Float { return t([]) }
""",
    "Some of an Int literal": """
pub fn t(o: Opt[Float]) -> Float { return 1.0 }
pub fn k() -> Float { return t(Some(3)) }
""",
    "Some of an Int value": """
pub fn t(o: Opt[Float]) -> Float { return 1.0 }
pub fn k(n: Int) -> Float { return t(Some(n)) }
""",
    "an if of list literals": """
pub fn t(xs: List[Float]) -> Float { return xs[0] }
pub fn k(c: Bool) -> Float { return t(if (c) { [1] } else { [2] }) }
""",
    "a record literal argument": TYPES + """
pub fn t(r: Row) -> Float { return r.f }
pub fn k(i: Int) -> Float { return t({ i: i, f: 3 }) }
""",
    "a variant payload literal": TYPES + """
pub fn k() -> Shape { return Line([1, 2]) }
""",
    "a value of the slot's own type": """
pub fn t(xs: List[Float]) -> Float { return xs[0] }
pub fn k(ys: List[Float]) -> Float { let zs: List[Float] = ys
  return t(zs) }
""",
    "a list of Int into a list of Int": """
pub fn t(xs: List[Int]) -> Int { return xs[0] }
pub fn k(ys: List[Int]) -> Int { return t(ys) }
""",
}

FAST_TIERS = ("ts", "go")
SLOW_TIERS = ("rust", "java")


def _outcome(tier: str, source: str) -> str:
    """`refused` (the checker said no), `pass`, `fail` or `skip`."""
    try:
        ir = compile_source(source, "value_flow.rvl")
    except RevlError as exc:
        return "refused" if REFUSAL in str(exc) else f"refused: {exc}"
    status, message = RUNNERS[tier](ir)
    return status if status != "skip" else f"skip: {message}"


def _agrees(tier: str, name: str) -> None:
    got = _outcome(tier, REFUSED[name])
    if got.startswith("skip"):
        pytest.skip(f"{tier}: {got}")
    assert got == "refused", (
        f"{name!r}: python and {tier} must not run a value that needs its "
        f"elements converted; {tier} answered {got!r}")


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_value_flow_is_refused_by_name(name: str):
    with pytest.raises(RevlError) as info:
        compile_source(REFUSED[name], "value_flow.rvl")
    err = info.value
    assert REFUSAL in str(err), str(err)
    assert err.code == "T1" and err.category == "type-mismatch"
    assert err.hint


@pytest.mark.parametrize("name", sorted(REFUSED))
@pytest.mark.parametrize("tier", ("py",) + FAST_TIERS)
def test_value_flow_agrees_on_every_tier(tier: str, name: str):
    _agrees(tier, name)


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(REFUSED))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_value_flow_agrees_on_every_tier_slow(tier: str, name: str):
    _agrees(tier, name)


@pytest.mark.parametrize("name", sorted(KEPT))
def test_constructors_and_bare_values_still_check(name: str):
    compile_source(KEPT[name], "value_flow.rvl")


def test_the_inner_pair_is_named():
    with pytest.raises(RevlError) as info:
        compile_source(REFUSED["the issue's List[Int] argument"],
                       "value_flow.rvl")
    text = str(info.value)
    assert "argument 1 of `total(...)`" in text
    assert "expects `List[Float]`, got `List[Int]`" in text
    assert "the `Int` inside is not widened to `Float`" in text
    with pytest.raises(RevlError) as info:
        compile_source(REFUSED["an Int32 list into an Int list"],
                       "value_flow.rvl")
    assert "the `Int32` inside is not widened to `Int`" in str(info.value)
