"""`/` and `%` on `Float`: five cross-tier divergences (item 458, issue #721).

docs/arithmetic.md specifies both operators. `/` is IEEE true division, where a
zero divisor has a VALUE (±infinity, or NaN at 0/0) rather than a fault, and `%`
is the truncated remainder, which on `Float` is C `fmod`. The Int half of that
page is executed on all six tiers by `tests/test_cross_tier_execution.py` and
`tests/test_int_min_edges_cross_tier.py`. The FLOAT half was not, and five
things were wrong under it — two on python, the reference tier, and three on go.

MEASURED before the fix, over a corpus of 1415 probe programs (see the PR):

    1. `NaN / 0.0`                py **Infinity**   ts/go/rust/java NaN
       `NaN / -0.0`               py **-Infinity**  ts/go/rust/java NaN

       `_revl_div` answered the zero-divisor branch by reading the sign off the
       dividend. `a == 0` is false for a NaN, so a NaN fell through to it and
       came back an infinity. IEEE: NaN / anything is NaN.

    2. `7.0 % 0.0`                py **ZeroDivisionError**  ts/rust/java NaN

       `%` on Float routed through `_revl_rem`, the Int helper, and inherited
       python's own raise. IEEE `fmod(x, 0)` is NaN — the same step out of line
       python's `/` used to take at a zero divisor, and closed the same way.
       The Int `%` must keep faulting there, so the two are separate helpers
       now.

    3. `(-0.0) % 1.0`             py **+0.0**  ts/rust/java -0.0

       `abs(a) % abs(b) if a >= 0` — and `-0.0 >= 0` is true, so the one
       dividend whose sign `abs` destroys took the positive branch. Observable
       as `1.0 / r`: Infinity against -Infinity.

    4. `a % b` on two Floats      go **the emitted package does not build**

       `invalid operation: operator % not defined on ... (variable of type
       float64)`. Go has no `%` on floats at all; `math.Mod` is the fmod the
       other tiers compute.

    5. a Float LITERAL in arithmetic on go was a Go CONSTANT

       Go folds constant expressions exactly, and a Go constant has neither a
       signed zero nor an infinity, so BOTH halves went wrong:

         `(0.0 - 1.0) * 0.0`   go **+0**, every other tier -0.0
         `1e308 * 10.0`        go **a compile error** ("constant 1e+309 of type
                               float64 overflows float64"), every other tier
                               +Infinity — which is what docs/arithmetic.md
                               says: "1e308 * 10.0 still overflows to infinity
                               at runtime on every tier".

       Typing the literal `float64(..)` stopped the arbitrary-precision UNTYPED
       fold that made `0.1 + 0.2` exactly 0.3, which is what that wrap was
       added for. It did not stop the TYPED fold. A literal goes through the
       `revlF` identity call now, the same move `revlDiv` already makes for
       `/`, and a call is not a constant expression.

wasm is the sixth tier and it REFUSES `Float` by name (`type 'Float' is not
lowerable`), which is the documented limit of that emitter rather than a
silent approximation; `test_wasm_refuses_float_by_name` pins that it stays a
named refusal.

NON-VACUITY. On the tree without the fix, 16 of these fail and 21 pass. The
three that pass on BOTH trees are the controls:
  * `test_the_truncated_float_remainder_is_unchanged` on py and ts — the sign
    rule `%` has everywhere else did not move. (Its go case fails before the
    fix for divergence 4, because that tier had no Float `%` to be right
    about.)
  * `test_an_int_remainder_at_a_zero_divisor_still_faults` — the Float fix did
    not make the Int `%` total, which is the way this is most easily got wrong.
  * `test_float_literal_arithmetic_is_ieee_not_exact` — `0.1 + 0.2` is still
    the IEEE sum on go and not the exactly-folded 0.3, the property the
    `float64(..)` wrap bought and the `revlF` call has to keep.
"""

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
from revl.test import RUNNERS  # noqa: E402

#: wasm is absent on purpose: it refuses `Float` by name, which
#: `test_wasm_refuses_float_by_name` pins instead of executing.
FLOAT_TIERS = ("py", "ts", "go")
SLOW_TIERS = ("rust", "java")
ALL_TIERS = ("py", "ts", "go", "wasm")

#: Every operand reaches its operator through a `let`, so no host constant
#: folder sees a literal pair and the probe measures the RUNTIME rule. That is
#: not decoration here: divergence 5 IS a constant fold, and writing
#: `1.0 / ((0.0 - 1.0) * 0.0)` inline would have measured go's folder rather
#: than go's arithmetic.
NAN_OVER_ZERO = """
pub fn nan() -> Float { let z = 0.0 return z / z }
pub fn probe() -> Str { let z = 0.0 return (nan() / z).to_str() }
test "NaN over zero is NaN" { assert probe() == "NaN" }
"""

NAN_OVER_NEGATIVE_ZERO = """
pub fn nan() -> Float { let z = 0.0 return z / z }
pub fn nz() -> Float { let a = 0.0 let b = 1.0 return (a - b) * a }
pub fn probe() -> Str { return (nan() / nz()).to_str() }
test "NaN over negative zero is NaN" { assert probe() == "NaN" }
"""

REMAINDER_AT_ZERO = """
pub fn probe() -> Str { let z = 0.0 let a = 7.0 return (a % z).to_str() }
test "a Float remainder at a zero divisor is NaN" { assert probe() == "NaN" }
"""

NEGATIVE_REMAINDER_AT_ZERO = """
pub fn probe() -> Str { let z = 0.0 let a = 0.0 - 7.0 return (a % z).to_str() }
test "a negative dividend at a zero divisor is NaN" { assert probe() == "NaN" }
"""

#: `to_str` renders -0.0 as "0" on every tier (the canonical Float text pinned
#: by tests/test_cross_tier_execution.py), so the SIGN has to be read through a
#: division, which is where it is observable at all.
ZERO_REMAINDER_KEEPS_ITS_SIGN = """
pub fn nz() -> Float { let a = 0.0 let b = 1.0 return (a - b) * a }
pub fn probe() -> Str { let one = 1.0 return (one / (nz() % one)).to_str() }
test "a -0.0 dividend keeps its sign" { assert probe() == "-Infinity" }
"""

NEGATIVE_ZERO_KEEPS_ITS_SIGN = """
pub fn nz() -> Float { return (0.0 - 1.0) * 0.0 }
pub fn probe() -> Str { return (1.0 / nz()).to_str() }
test "a computed negative zero keeps its sign" { assert probe() == "-Infinity" }
"""

FLOAT_OVERFLOW_IS_INFINITY = """
pub fn big() -> Float { return 1e308 * 10.0 }
pub fn probe() -> Str { return big().to_str() }
test "float overflow is an IEEE infinity" { assert probe() == "Infinity" }
"""

DIVERGENCES = {
    "NaN over zero": NAN_OVER_ZERO,
    "NaN over negative zero": NAN_OVER_NEGATIVE_ZERO,
    "a Float remainder at a zero divisor": REMAINDER_AT_ZERO,
    "a negative dividend at a zero divisor": NEGATIVE_REMAINDER_AT_ZERO,
    "a zero remainder keeps its sign": ZERO_REMAINDER_KEEPS_ITS_SIGN,
    "a computed negative zero keeps its sign": NEGATIVE_ZERO_KEEPS_ITS_SIGN,
    "float overflow is an infinity": FLOAT_OVERFLOW_IS_INFINITY,
}

# ---------------------------------------------------------------- controls

TRUNCATED_REMAINDER = """
pub fn rem(a: Float, b: Float) -> Str { return (a % b).to_str() }
test "positive"        { assert rem(7.0, 3.0) == "1" }
test "negative left"   { assert rem(0.0 - 7.0, 3.0) == "-1" }
test "negative right"  { assert rem(7.0, 0.0 - 3.0) == "1" }
test "both negative"   { assert rem(0.0 - 7.0, 0.0 - 3.0) == "-1" }
test "an infinite divisor leaves the dividend" { assert rem(7.0, 1e308 * 10.0) == "7" }
"""

INT_REMAINDER_AT_ZERO = """
pub fn probe() -> Str { let z = 0 let a = 7 return (a % z).to_str() }
test "an Int remainder at a zero divisor must not answer a value" {
  assert probe() == probe()
}
"""

IEEE_NOT_EXACT = """
pub fn probe() -> Str { return (0.1 + 0.2).to_str() }
test "float literal arithmetic is IEEE, not exact" {
  assert probe() == "0.30000000000000004"
}
"""


def _run(tier: str, source: str):
    return RUNNERS[tier](compile_source(source, "float_div_458.rvl"))


def _emit(backend: str, source: str) -> str:
    emitted = backend_emitter(backend).emit(compile_source(source))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


# ---------------------------------------------------------------- executed

@pytest.mark.parametrize("name", sorted(DIVERGENCES))
@pytest.mark.parametrize("tier", FLOAT_TIERS)
def test_the_float_edges_agree_with_python(tier: str, name: str):
    status, message = _run(tier, DIVERGENCES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} on {name}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(DIVERGENCES))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_the_float_edges_agree_with_python_slow(tier: str, name: str):
    status, message = _run(tier, DIVERGENCES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} on {name}: {message}"


@pytest.mark.parametrize("tier", FLOAT_TIERS)
def test_the_truncated_float_remainder_is_unchanged(tier: str):
    """NON-VACUITY: `%` still takes the sign of the DIVIDEND on a Float, which
    is the rule docs/arithmetic.md publishes and the pairing law rests on."""
    status, message = _run(tier, TRUNCATED_REMAINDER)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_an_int_remainder_at_a_zero_divisor_still_faults(tier: str):
    """NON-VACUITY, the edge this is most easily got wrong on: giving `%` on
    Float an IEEE answer at zero must not make the INT `%` total there.

    `probe() == probe()` is true of any value a tier invents, so this test
    PASSES on a tier that returns and FAILS only on one that faults — the
    assertion below is therefore that the document FAILS."""
    status, message = _run(tier, INT_REMAINDER_AT_ZERO)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier} answered a VALUE for `7 % 0` ({status}). Integer division has "
        "no value at zero and every tier faults; docs/arithmetic.md.")


@pytest.mark.parametrize("tier", FLOAT_TIERS)
def test_float_literal_arithmetic_is_ieee_not_exact(tier: str):
    """NON-VACUITY: go's float literals moved from `float64(..)` to `revlF(..)`,
    and the property the first spelling bought has to survive the second.

    Go evaluates constant expressions exactly, so an UNTYPED `0.1 + 0.2` folds
    to exactly 0.3 — not IEEE 754 binary64. Through a call it is an ordinary
    runtime addition and answers what every other tier answers."""
    status, message = _run(tier, IEEE_NOT_EXACT)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


# ------------------------------------------------------------------ static
#
# The half that runs with no node and no go toolchain: a tier whose runner
# skips is UNMEASURED, and unmeasured has never meant right.

def test_go_lowers_a_float_remainder_through_math_mod():
    emitted = _emit("go", "pub fn r(a: Float, b: Float) -> Float { return a % b }")
    assert "math.Mod(a, b)" in emitted, (
        "the go Float remainder is back to a bare `%`, which is not an "
        "operation Go has on float64 — the emitted package does not build")
    assert '"math"' in emitted, "math.Mod needs the import beside it"


def test_go_emits_float_literals_through_a_call():
    emitted = _emit("go", "pub fn s(a: Float) -> Float { return a * 0.5 + 1.25 }")
    assert "func revlF(v float64) float64 { return v }" in emitted
    assert "revlF(0.5)" in emitted and "revlF(1.25)" in emitted, (
        "a Float literal is a Go CONSTANT again; the fold around it has no "
        "signed zero and no infinity")
    assert "float64(0.5)" not in emitted


def test_go_widens_an_int_literal_through_the_same_call():
    """`float64(2)` is a constant too, so the `widen` marker over a literal
    goes through `revlF` rather than the conversion."""
    emitted = _emit("go", "pub fn w() -> Float { return 2 }")
    assert "revlF(2)" in emitted, emitted
    assert "float64(2)" not in emitted


def test_python_builds_the_ieee_float_remainder():
    emitted = _emit("python", "pub fn r(a: Float, b: Float) -> Float { return a % b }")
    assert "def _revl_frem(a, b):" in emitted
    assert "return float('nan')" in emitted, (
        "the Float remainder is back on the Int helper, which inherits "
        "python's ZeroDivisionError where IEEE fmod answers NaN")
    # and the Int one is untouched by that
    int_rem = _emit("python", "pub fn r(a: Int, b: Int) -> Int { return a % b }")
    assert "def _revl_rem(a, b):" in int_rem
    assert "_revl_frem" not in int_rem


def test_python_answers_nan_for_a_nan_dividend():
    emitted = _emit("python", "pub fn d(a: Float, b: Float) -> Float { return a / b }")
    assert "    if a != a:" in emitted, (
        "`_revl_div` reads the sign off the dividend at a zero divisor, and a "
        "NaN is neither zero nor signed — it came back an infinity")


def test_wasm_refuses_float_by_name():
    """The sixth tier cannot match, and fails CLOSED: a named refusal at emit
    time, not a silent approximation at runtime (docs/arithmetic.md)."""
    with pytest.raises(ValueError) as excinfo:
        _emit("wasm", "pub fn r(a: Float, b: Float) -> Float { return a % b }")
    assert "'Float' is not lowerable" in str(excinfo.value), excinfo.value
