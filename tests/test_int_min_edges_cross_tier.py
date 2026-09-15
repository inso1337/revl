"""`Int.MIN` at the edge of every integer operation, executed on all six tiers.

Roadmap item 458 / issue #721, the cross-tier divergence hunt, numeric family.

`tests/test_cross_tier_execution.py` already pins that `Int` is bounded and
that overflow TRAPS, and it already pins the named divisions against negative
operands. Both of those probes stop short of the same place: the one dividend
whose negation is not representable. `Int.MIN` is `-2^63`, `-Int.MIN` is `2^63`,
and `Int.MAX` is `2^63 - 1` — so every host primitive written as "negate and
recurse" is PARTIAL there, and every one of them fails silently, because two's
complement negation of `Int.MIN` gives `Int.MIN` back rather than raising.

Measured on this branch's parent, one document per row, six tiers each:

| probe                       | py   | ts   | go       | rust  | java     | wasm     |
|-----------------------------|------|------|----------|-------|----------|----------|
| `Int.MIN * -1`              | trap | trap | **MIN**  | trap  | trap     | trap     |
| `Int.MIN.div_euclid(-1)`    | trap | trap | **MIN**  | trap  | **MIN**  | **MIN**  |
| `Int.MIN.div_floor(-1)`     | trap | trap | trap     | trap  | **MIN**  | trap     |
| `(-7).div_euclid(Int.MIN)`  | 1    | 1    | **-1**   | 1     | **-1**   | **-1**   |
| `(-5).mod(Int.MIN)`         | ok   | ok   | ok       | ok    | **wrong**| ok       |
| `Int.MIN.mod(-1)`           | 0    | 0    | 0        | **panic** | 0    | 0        |
| `checked_div_*(Int.MIN,-1)` | Err  | Err  | Err      | **panic** | Err  | **trap** |

Every bolded cell is one of three shapes:

  * `p / b != a` cannot see `Int.MIN * -1`. Go DEFINES `Int.MIN / -1` as
    `Int.MIN` rather than trapping, so the product wraps to `Int.MIN`, the
    readback equals the multiplicand, and the check passes.
  * `div_euclid` was written `b > 0 ? div_floor(a, b) : -div_floor(a, -b)` on
    three tiers. BOTH negations are partial: `-b` wraps for `b == Int.MIN` (so
    the quotient against `Int.MIN` came back negated) and the leading `-` wraps
    for `b == -1` (so a quotient of `2^63` came back as `Int.MIN`). One
    expression, a wrong answer and a missing trap.
  * `Math.abs(Long.MIN_VALUE)` is `Long.MIN_VALUE`, still negative, so java's
    `mod` ran `floorMod` against a NEGATIVE modulus. `mod` is documented never
    to be negative (docs/arithmetic.md); against `Int.MIN` it was.

And two in the other direction, which are faults where a value is defined
rather than wrong answers, but diverge just as much: rust's `rem_euclid`
computes `self % rhs` first, and `Int.MIN % -1` overflows, so `Int.MIN.mod(-1)`
PANICKED where the Euclidean remainder against `|-1| = 1` is 0 for every
dividend; and rust and wasm let the `checked_*` forms fault at `Int.MIN / -1`,
where the other four answer `Err("revl: Int overflow")` as a value. A
`checked_*` that can still fault is not checked.

WHICH TIERS EXECUTE. py, ts, go and **wasm** run by default — all four are
fast, and wasm is in the default set here deliberately: it carries a real i64
`Int` for these operations, it had two of the seven defects, and
`tests/test_cross_tier_execution.py` names it in no tier tuple at all, so this
family had never been measured on it. rust and java sit behind
`REVL_CROSS_TIER_SLOW` with the rest of the matrix (cargo and javac), and every
one of them also has a cheap STATIC guard below, because a `*_slow` skip means
UNMEASURED and has never meant passing.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

# py, ts, go and wasm are all fast enough to run by default; rust (cargo) and
# java (javac + JVM) are not. Same split as the rest of the matrix, with wasm
# promoted into the default set for the reason the module docstring gives.
FAST_TIERS = ("py", "ts", "go", "wasm")
SLOW_TIERS = ("rust", "java")
ALL_TIERS = FAST_TIERS + SLOW_TIERS

# Int.MIN is built rather than written: the lexer takes `9223372036854775807`
# (Int.MAX) and unary minus is `0 - x`, so `Int.MIN` is one step past the
# largest literal. Every operand travels through a `pub fn` so no tier's
# constant folder can answer the question at compile time — go folds untyped
# constants in arbitrary precision, and a folded answer is not the answer the
# emitted arithmetic gives.
_PRELUDE = """
pub fn imin() -> Int { return 0 - 9223372036854775807 - 1 }
pub fn mone() -> Int { return 0 - 1 }
pub fn mfive() -> Int { return 0 - 5 }
pub fn five() -> Int { return 5 }
"""

# Each of these has NO representable answer: the true value is 2^63, one past
# Int.MAX. The probe asserts only that the operation produced SOME value
# (`d() == d()` is trivially true of any Int), so a tier that passes has
# silently answered — wrapped — and a tier that faults fails it. That is the
# inversion the assertion is written for: `assert status == "fail"`.
OVERFLOW_AT_MIN = {
    "multiplication": "imin() * mone()",
    "div_trunc": "imin().div_trunc(mone())",
    "div_floor": "imin().div_floor(mone())",
    "div_euclid": "imin().div_euclid(mone())",
    "unary minus": "-(imin())",
    "subtraction": "imin() - 1",
}

# The divisor no tier may negate. Each answer here is a VALUE, and it is the
# value python — the reference — computes with unbounded integers: `div_euclid`
# against a negative `b` is `-(a // -b)` over the rationals, and `mod` against
# `b` is `a % |b|`, which for `b == Int.MIN` is `a mod 2^63` and always fits.
MIN_AS_DIVISOR = _PRELUDE + """
test "div_euclid of Int.MIN by Int.MIN"   { assert imin().div_euclid(imin()) == 1 }
test "div_euclid of a negative by Int.MIN" { assert mfive().div_euclid(imin()) == 1 }
test "div_euclid of a positive by Int.MIN" { assert five().div_euclid(imin()) == 0 }
test "div_trunc of Int.MIN by Int.MIN"    { assert imin().div_trunc(imin()) == 1 }
test "div_floor of Int.MIN by Int.MIN"    { assert imin().div_floor(imin()) == 1 }
test "mod of a negative by Int.MIN"       { assert mfive().mod(imin()) == 9223372036854775803 }
test "mod of a positive by Int.MIN"       { assert five().mod(imin()) == 5 }
test "mod of Int.MIN by -1 is zero"       { assert imin().mod(mone()) == 0 }
test "mod of Int.MIN by Int.MIN is zero"  { assert imin().mod(imin()) == 0 }
test "rem of Int.MIN by -1 is zero"       { assert imin() % mone() == 0 }
"""

# The total forms are total, including here. `Int.MIN / -1` has no quotient, so
# the three that answer a quotient answer Err — the same reason string every
# tier spells — and `checked_mod`, whose answer at that divisor IS defined,
# answers Ok(0).
CHECKED_AT_MIN = _PRELUDE + """
test "checked_div_trunc is Err"  { assert match imin().checked_div_trunc(mone()) { Ok(v) => v != v, Err(e) => e == "revl: Int overflow" } }
test "checked_div_floor is Err"  { assert match imin().checked_div_floor(mone()) { Ok(v) => v != v, Err(e) => e == "revl: Int overflow" } }
test "checked_div_euclid is Err" { assert match imin().checked_div_euclid(mone()) { Ok(v) => v != v, Err(e) => e == "revl: Int overflow" } }
test "checked_mod is Ok(0)"      { assert match imin().checked_mod(mone()) { Ok(v) => v == 0, Err(e) => e != e } }
test "checked_div_euclid by Int.MIN" { assert match mfive().checked_div_euclid(imin()) { Ok(v) => v == 1, Err(e) => e != e } }
test "checked_mod by Int.MIN"        { assert match mfive().checked_mod(imin()) { Ok(v) => v == 9223372036854775803, Err(e) => e != e } }
"""

# The NON-VACUITY control. Every operand is in range and no negation is at the
# edge, so this passes on both sides of the fix — it is here to prove the three
# probes above are measuring the Int.MIN edge and not "the named divisions are
# broken", and to catch a fix that bought the edge by breaking the middle.
IN_RANGE_CONTROL = """
test "rem takes the sign of the dividend"  { assert (0 - 7) % 3 == 0 - 1 }
test "div_trunc rounds toward zero"        { assert (0 - 7).div_trunc(2) == 0 - 3 }
test "div_floor rounds toward -infinity"   { assert (0 - 7).div_floor(2) == 0 - 4 }
test "div_euclid, negative dividend"       { assert (0 - 7).div_euclid(2) == 0 - 4 }
test "div_euclid, negative divisor"        { assert (0 - 7).div_euclid(0 - 2) == 4 }
test "div_euclid, positive over negative"  { assert 7.div_euclid(0 - 2) == 0 - 3 }
test "div_euclid, positive over positive"  { assert 7.div_euclid(2) == 3 }
test "mod is never negative"               { assert (0 - 7).mod(3) == 2 }
test "mod ignores the divisor's sign"      { assert 7.mod(0 - 3) == 1 }
test "multiplication in range"             { assert 1000000 * 1000000 == 1000000000000 }
"""

_SLOW = pytest.mark.skipif(
    not os.environ.get("REVL_CROSS_TIER_SLOW"),
    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")


def _run(tier: str, source: str):
    return RUNNERS[tier](compile_source(source, "int_min_edges.rvl"))


def _emit(backend: str, source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        f"int_min_edges_{backend}", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    emitted = module.emit(compile_source(source, "int_min_edges.rvl"))
    return emitted if isinstance(emitted, str) else str(emitted)


def _overflow_doc(expression: str) -> str:
    return (_PRELUDE
            + f"pub fn d() -> Int {{ return {expression} }}\n"
            + 'test "the operation answered at all" { assert d() == d() }\n')


def _assert_traps(tier: str, name: str, expression: str) -> None:
    status, message = _run(tier, _overflow_doc(expression))
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier}: {name} at Int.MIN answered a value ({status}) — the true "
        f"result is 2^63, one past Int.MAX, so it wrapped: {message}")


@pytest.mark.parametrize("name,expression", sorted(OVERFLOW_AT_MIN.items()))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_no_tier_invents_a_value_for_2_to_the_63(tier: str, name: str, expression: str):
    _assert_traps(tier, name, expression)


@_SLOW
@pytest.mark.parametrize("name,expression", sorted(OVERFLOW_AT_MIN.items()))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_no_tier_invents_a_value_for_2_to_the_63_slow(tier: str, name: str, expression: str):
    _assert_traps(tier, name, expression)


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_int_min_as_a_divisor_agrees(tier: str):
    status, message = _run(tier, MIN_AS_DIVISOR)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@_SLOW
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_int_min_as_a_divisor_agrees_slow(tier: str):
    status, message = _run(tier, MIN_AS_DIVISOR)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_the_total_forms_stay_total_at_int_min(tier: str):
    status, message = _run(tier, CHECKED_AT_MIN)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@_SLOW
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_the_total_forms_stay_total_at_int_min_slow(tier: str):
    status, message = _run(tier, CHECKED_AT_MIN)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_in_range_named_division_still_agrees(tier: str):
    """The control: in-range operands, no edge anywhere. Passes on both sides
    of the fix, so a green run of the probes above is about `Int.MIN` and not
    about the named divisions in general."""
    status, message = _run(tier, IN_RANGE_CONTROL)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@_SLOW
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_in_range_named_division_still_agrees_slow(tier: str):
    status, message = _run(tier, IN_RANGE_CONTROL)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


# ------------------------------------------------------- cheap static guards
#
# A `*_slow` skip means UNMEASURED, so each executed claim above that a slow
# tier carries gets a static reading of the emitted text as well. These cost
# nothing and run everywhere.

def test_go_multiply_names_the_case_its_readback_cannot_see():
    """`p/b != a` is the standard overflow check and it is blind at one input:
    Go DEFINES `Int.MIN / -1` as `Int.MIN`, so for `Int.MIN * -1` the wrapped
    product reads back as the multiplicand and the check passes."""
    emitted = _emit("go", _overflow_doc("imin() * mone()"))
    assert "func revlMul(a, b int64) int64 {" in emitted
    guard = "if a == (-9223372036854775807 - 1) && b == -1 {"
    head = emitted[emitted.index("func revlMul("):]
    assert guard in head[:head.index("\n}")], head[:400]


def test_no_tier_lowers_div_euclid_by_negating_the_divisor():
    """The shape that carried the defect on three tiers at once. `-x` is
    partial in two's complement, so an emitter that reaches for it inside
    `div_euclid` has both a wrong answer at `b == Int.MIN` and a missing trap
    at `b == -1`."""
    doc = _PRELUDE + 'pub fn d() -> Int { return mfive().div_euclid(imin()) }\n'
    assert "-revlDivFloor(a, -b)" not in _emit("go", doc)
    assert "-Math.floorDiv" not in _emit("java", doc)
    wat = _emit("wasm", doc)
    body = wat[wat.index("(func $int_div_euclid"):]
    body = body[:body.index("(func ", 1)]
    assert "call $int_div_floor" not in body, body
    assert "(i64.sub (i64.const 0)" not in body, body


def test_java_routes_the_named_divisions_through_total_helpers():
    """`Math.floorDiv(Long.MIN_VALUE, -1)` is documented to overflow and return
    `Long.MIN_VALUE`, and `Math.abs(Long.MIN_VALUE)` is negative. Both are
    reached only through helpers now, and the quotient helpers truncate with
    `Math.divideExact`, which throws on that one input."""
    doc = _PRELUDE + (
        "pub fn a() -> Int { return mfive().div_floor(imin()) }\n"
        "pub fn b() -> Int { return mfive().div_euclid(imin()) }\n"
        "pub fn c() -> Int { return mfive().mod(imin()) }\n")
    emitted = _emit("java", doc)
    assert "private static long revlDivFloor(long a, long b) {" in emitted
    assert "private static long revlDivEuclid(long a, long b) {" in emitted
    assert "private static long revlMod(long a, long b) {" in emitted
    assert emitted.count("Math.divideExact(a, b)") == 2, emitted
    assert "if (b == Long.MIN_VALUE)" in emitted


def test_java_checked_helpers_reuse_the_same_bodies():
    """The total forms had their own copy of the two partial expressions, so
    fixing the faulting forms alone would have left the same two answers
    reachable through `checked_*`."""
    doc = _PRELUDE + (
        "pub fn a() -> Result[Int, Str] { return mfive().checked_div_euclid(imin()) }\n"
        "pub fn b() -> Result[Int, Str] { return mfive().checked_mod(imin()) }\n")
    emitted = _emit("java", doc)
    assert "new RevlResult.Ok<>(revlDivEuclid(a, b))" in emitted
    assert "new RevlResult.Ok<>(revlMod(a, b))" in emitted
    # and the helpers they call travel with them, even though this document
    # spells no *faulting* division at all
    assert "private static long revlDivEuclid(long a, long b) {" in emitted
    assert "private static long revlMod(long a, long b) {" in emitted


@pytest.mark.parametrize("backend,needle", [
    # rust's own `Int.MIN` guard reads as the source-level constant; wasm's Err
    # payload is a pooled string, and this tier's *traps* carry no payload at
    # all, so the reason appearing in the module at all IS the checked arm.
    ("rust", "a == i64::MIN && b == -1"),
    ("wasm", "revl: Int overflow"),
])
def test_the_checked_forms_answer_overflow_as_a_value(backend: str, needle: str):
    """rust and wasm both faulted at `Int.MIN / -1` inside a `checked_*`: rust
    through `/` and `div_euclid`, wasm through `i64.div_s`. The other four
    tiers answered `Err`, and a `checked_*` that can still fault is not
    checked. `checked_mod` is the exception in both directions — its answer at
    that divisor is 0, so it must NOT carry the overflow arm."""
    quotient = _PRELUDE + (
        "pub fn d() -> Result[Int, Str] { return imin().checked_div_trunc(mone()) }\n")
    remainder = _PRELUDE + (
        "pub fn d() -> Result[Int, Str] { return imin().checked_mod(mone()) }\n")
    assert needle in _emit(backend, quotient)
    assert needle not in _emit(backend, remainder)


def test_rust_mod_does_not_go_through_an_overflowing_remainder():
    """`i64::rem_euclid` computes `self % rhs` first, and `Int.MIN % -1`
    overflows — so rust PANICKED on `Int.MIN.mod(-1)`, whose answer is 0 on
    every other tier."""
    doc = _PRELUDE + "pub fn d() -> Int { return imin().mod(mone()) }\n"
    emitted = _emit("rust", doc)
    assert "if b == -1 { 0 }" in emitted, emitted


def test_rust_remainder_and_negation_keep_the_bound_in_a_release_build():
    """Two rust operators were the bare host ones. `%` PANICS at
    `Int.MIN % -1`, whose answer is 0 on the other five tiers, because it
    computes the quotient on the way. And unary minus only checks in a DEBUG
    profile, so `-Int.MIN` kept the trap under `cargo test` and would have
    wrapped in a release build — the exact reason the `+`/`-`/`*` arms beside
    it already reach for the `checked_*` family. py, ts, go and java all impose
    the bound on negation unconditionally."""
    doc = _PRELUDE + (
        "pub fn a() -> Int { return imin() % mone() }\n"
        "pub fn b() -> Int { return -(imin()) }\n")
    emitted = _emit("rust", doc)
    assert ".wrapping_rem(" in emitted, emitted
    assert '.checked_neg().expect("revl: Int overflow")' in emitted, emitted


def test_every_tier_spells_the_overflow_reason_the_same_way():
    """One guarantee should not read as six different bugs — the same rule
    `revl: Int overflow` and the map-miss reason already carry."""
    doc = _PRELUDE + (
        "pub fn d() -> Result[Int, Str] { return imin().checked_div_trunc(mone()) }\n")
    for backend in ("python", "typescript", "go", "rust", "java", "wasm"):
        assert "revl: Int overflow" in _emit(backend, doc), backend
