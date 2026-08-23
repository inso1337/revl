"""One source, every tier, actually *run* — the semantic floor.

tests/test_cross_tier.py holds the portability floor by checking that every
emitter accepts a construct. That catches a tier which refuses; it cannot
catch a tier which accepts and then means something different. Structural
equality was exactly that hole: `{a: 1} == {a: 1}` emitted cleanly on all
five tiers, was true on python and **false** on TypeScript (JS `===` is
identity for objects), and did not compile at all on rust (no `PartialEq`
derive). syntax-2.0 §3.4 promises one structural equality and says "no
backend can diverge", so this was a silent wrong answer against an explicit
guarantee.

The lesson is the project's own recurring one, one level up: "the emitter did
not raise" never implied "the code is right", and "every emitter agreed on a
shape" never implied "every tier agrees on a value".

python and TypeScript execute here because both are fast. rust and java are
gated behind REVL_CROSS_TIER_SLOW=1 (cargo and javac make the default suite
minutes rather than seconds); their regression guards below are static and
cheap, and CI runs the full matrix.
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

# Each probe is a semantic claim the language makes, written so that a tier
# which merely *compiles* it still fails when it means the wrong thing.
PROBES = {
    "structural equality": """
type Row = { id: Int, name: Str }
pub fn rec_eq() -> Bool { return { id: 1, name: "a" } == { id: 1, name: "a" } }
pub fn rec_ne() -> Bool { return { id: 1, name: "a" } == { id: 2, name: "a" } }
pub fn list_eq() -> Bool { return [1, 2] == [1, 2] }
pub fn list_ne() -> Bool { return [1, 2] == [2, 1] }
pub fn nested() -> Bool { return [{ id: 1, name: "a" }] == [{ id: 1, name: "a" }] }

test "records compare by value" { assert rec_eq() }
test "different records are not equal" { assert !rec_ne() }
test "lists compare by value" { assert list_eq() }
test "list order matters" { assert !list_ne() }
test "nesting composes" { assert nested() }
test "inequality is the negation" { assert { id: 1, name: "a" } != { id: 2, name: "a" } }
""",

    # The former pinned divergence "Int widens into a Float position": revl
    # widens `Int` into `Float` implicitly (`compatible("Float", "Int")`), so
    # `ident(3)` for `ident: (Float) -> Float` is a legal program — and until
    # the coercion was marked in the IR it split the tiers three ways (rust
    # E0308, TypeScript a wrong answer via `3n === 3`, host-rule absorption
    # elsewhere; docs/arithmetic.md). The frontend now marks every coercion
    # site (`"widen": "Float"`) and each backend emits the conversion, so this
    # probe must *pass* on every tier that can run it.
    "int widens into a float position": """
pub fn ident(x: Float) -> Float { return x }
pub fn widen_let() -> Float { let x: Float = 3 return x }
pub fn widen_return(x: Int) -> Float { return x }
test "call argument widens" { assert ident(3) == 3.0 }
test "annotated let widens" { assert widen_let() == 3.0 }
test "return widens" { assert widen_return(4) == 4.0 }
""",
}

# go joins the fast set: the v3 tier is dependency-free Go, so `go test`
# needs no network and runs in about a second — unlike cargo, which resolves
# cordis-rs from the index.
FAST_TIERS = ("py", "ts", "go")
SLOW_TIERS = ("rust", "java")


def _run(tier: str, source: str) -> tuple[str, str]:
    return RUNNERS[tier](compile_source(source, "cross_tier_exec.rvl"))


@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_probe_executes_identically(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} failed {name!r}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_probe_executes_identically_slow(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} failed {name!r}: {message}"


# --------------------------------------------------- cheap static guards

def _emit(backend: str, source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        f"emit_{backend}_xtierexec", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source, "cross_tier_exec.rvl"))


def test_typescript_equality_is_not_js_identity():
    """`===` on two records is `false` in JavaScript. The emitted code must
    not contain it for an equality the language says is structural."""
    emitted = _emit("typescript", PROBES["structural equality"])
    assert "revlEq(" in emitted
    assert "function revlEq" in emitted, "the helper must travel with the module"


def test_rust_records_derive_partial_eq():
    """Without the derive, rustc refuses `==` outright (E0369) and legal revl
    simply does not build on this tier."""
    emitted = _emit("rust", PROBES["structural equality"])
    assert "PartialEq" in emitted
    for line in emitted.splitlines():
        if line.startswith("pub struct ") or line.startswith("pub enum "):
            break
    assert "#[derive(Clone, Debug, PartialEq" in emitted


def test_java_equality_goes_through_objects_equals():
    """Java records have a structural `equals`; `==` would be identity. This
    tier was already correct and should stay that way."""
    emitted = _emit("java", PROBES["structural equality"])
    assert "java.util.Objects.equals(" in emitted


# ------------------------------------------------- widening is emitted, not absorbed

def test_widening_is_marked_in_the_ir():
    """The coercion site itself must be visible in the document: the marker is
    what lets every backend emit the same conversion for `ident(3)`."""
    ir = compile_source(
        'pub fn ident(x: Float) -> Float { return x }\n'
        'test "w" { assert ident(3) == 3.0 }',
        "widen.rvl")
    call = ir["functions"][0]
    assert call["name"] == "ident"
    # the marker lives on the argument node of the *test* body's call; find it
    def find_widened(node):
        if isinstance(node, dict):
            if node.get("widen") == "Float":
                return node
            for v in node.values():
                found = find_widened(v)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = find_widened(v)
                if found is not None:
                    return found
        return None
    assert find_widened(ir) is not None, (
        "an Int literal flowing into a Float parameter must carry the "
        '`"widen": "Float"` marker (docs/arithmetic.md)')


@pytest.mark.parametrize("backend, conversion", [
    ("python", "ident(float(3))"),
    ("typescript", "ident(Number(3n))"),
    ("rust", "ident((3i64 as f64))"),
    ("go", "ident(float64(3))"),
    ("java", "ident(((double) (3L)))"),
])
def test_every_tier_emits_the_conversion(backend, conversion):
    """The point of the marker: no tier absorbs the widening behind a host
    rule any more — rust used to refuse (E0308) and TypeScript computed the
    wrong answer (`3n === 3` is false). Each emitter spells the conversion in
    its own host syntax, and each spelling is pinned here."""
    source = ('pub fn ident(x: Float) -> Float { return x }\n'
              'test "w" { assert ident(3) == 3.0 }')
    assert conversion in _emit(backend, source), (
        f"{backend} must emit the Int -> Float conversion at the marked "
        "coercion site (docs/arithmetic.md)")


# ------------------------------------------------- pinned arithmetic divergences
#
# These are NOT the intended semantics. They are the current per-tier behaviour,
# recorded so it cannot drift silently — the same baseline discipline
# tests/test_conformance_validate.py uses: the test fails on new breakage *and*
# on a pinned case that starts passing, so the list can only change deliberately.
#
# The root cause is one missing piece of information: an IR `bin` node carries
# `op`, `left` and `right` and no type, so no backend can tell `Int / Int` from
# `Float / Float`. Closing these properly means annotating the node — a change
# to the IR contract, not a backend patch. See docs/contract-errata.md.

# "Int keeps precision past 2^53" used to live here, pinned `ts: "fail"`
# because that tier mapped `Int` to `number` — an f64, exact only to 2^53, on
# which `9007199254740993 - 9007199254740992` is `0`. TypeScript now maps `Int`
# to `bigint`, so it is asserted positively in INT_IN_RANGE below instead of
# being pinned. An entry leaves this table only by the tier conforming.

DIVERGENCES = {
    # ~~"Int widens into a Float position"~~ **Closed.** The frontend now
    # marks every implicit Int -> Float coercion site in the IR (`"widen":
    # "Float"` on the coerced node — additive, like `operands`, no ir_version
    # bump) and each backend emits the conversion (`float(3)`, `Number(3n)`,
    # `(3i64 as f64)`, `float64(3)`, `((double) (3L))`), so rust no longer
    # refuses with E0308 and TypeScript no longer computes the wrong answer.
    # Asserted positively in WIDENING below; docs/arithmetic.md has the write-up.
    # ~~"negating Int.MIN does not trap on every tier"~~ **Closed.** go now
    # negates through `revlSub(0, x)`, java through `Math.negateExact`, ts
    # through `revlI64(-x)` — so every bounded tier faults on `-Int.MIN` exactly
    # as it does on any other Int overflow. Asserted positively below
    # (test_negation_of_int_min_traps + the per-tier emit checks).
    #
    # **Open — found during review, unpinned before now.** `Int.MIN / -1` has
    # quotient 2^63, which does not fit i64: integer division overflows at
    # exactly this one input (mod is fine, `Int.MIN % -1 == 0`). rust panics,
    # wasm's `i64.div_s` traps, and typescript re-imposes the bound via
    # `revlI64` on the quotient — all correct. But **python computes the
    # arbitrary-precision 2^63** (out of the range it imposes everywhere else)
    # and **go and java wrap to Int.MIN** (host `/`). Both the faulting div
    # forms (div_trunc/floor/euclid) and the checked ones share the gap. Close
    # it the way unary minus was closed — bound the quotient (trap for the
    # faulting forms, Err for the checked). Pinned here so it cannot drift.
    "Int.MIN / -1 does not trap on every tier": (
        'pub fn lo() -> Int { return 0 - 9223372036854775807 - 1 }\n'
        'pub fn f(a: Int, b: Int) -> Int { return a.div_trunc(b) }\n'
        'test "pinned" { assert f(lo(), 0 - 1) == lo() }',
        {"py": "fail", "ts": "fail", "rust": "fail", "go": "pass", "java": "pass"},
    ),
}


def _observed(tier: str, source: str) -> str:
    status, message = _run(tier, source)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    return status


@pytest.mark.parametrize("name", sorted(DIVERGENCES))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_pinned_divergence_has_not_drifted(tier: str, name: str):
    source, pinned = DIVERGENCES[name]
    observed = _observed(tier, source)
    assert observed == pinned[tier], (
        f"{tier} now {observed}es {name!r}, pinned as {pinned[tier]}. "
        "If this was fixed on purpose, update DIVERGENCES and the entry in "
        "docs/contract-errata.md rather than loosening the test."
    )


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(DIVERGENCES))
def test_pinned_divergence_has_not_drifted_rust(name: str):
    source, pinned = DIVERGENCES[name]
    observed = _observed("rust", source)
    assert observed == pinned["rust"], (
        f"rust now {observed}es {name!r}, pinned as {pinned['rust']}")


def test_str_ordering_agrees_everywhere():
    """Not every operator diverges — `<` on Str is lexicographic by code point
    on all three, including the case boundary. Recorded so the divergence list
    above is not mistaken for 'arithmetic is broken generally'."""
    source = 'test "a" { assert "a" < "b" }\ntest "b" { assert "Z" < "a" }'
    for tier in FAST_TIERS:
        status, message = _run(tier, source)
        if status == "skip":
            continue
        assert status == "pass", f"{tier}: {message}"


# ---------------------------------------------- named integer arithmetic
#
# `/` is true division on every tier now (§0: it is spelled as TypeScript
# spells it, so it means what TypeScript means). Integer division has its own
# names, and each has ONE definition that every tier computes rather than
# inheriting its host's convention. The negatives are the whole point — that
# is where C, python and the mathematics all disagree.

INTEGER_ARITHMETIC = """
test "rem takes the sign of the dividend"    { assert (0 - 7) % 3 == 0 - 1 }
test "rem with a negative divisor"           { assert 7 % (0 - 3) == 1 }
test "rem with both negative"                { assert (0 - 7) % (0 - 3) == 0 - 1 }
test "div_trunc rounds toward zero"      { assert (0 - 7).div_trunc(2) == 0 - 3 }
test "div_trunc on positives"            { assert 7.div_trunc(2) == 3 }
test "div_floor rounds toward -infinity" { assert (0 - 7).div_floor(2) == 0 - 4 }
test "div_euclid, negative dividend"     { assert (0 - 7).div_euclid(2) == 0 - 4 }
test "div_euclid, negative divisor"      { assert (0 - 7).div_euclid(0 - 2) == 4 }
test "mod is never negative"             { assert (0 - 7).mod(3) == 2 }
test "mod ignores the divisor's sign"    { assert 7.mod(0 - 3) == 1 }
test "mod on positives"                  { assert 7.mod(3) == 1 }
"""


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_named_integer_arithmetic_agrees(tier: str):
    status, message = _run(tier, INTEGER_ARITHMETIC)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_named_integer_arithmetic_agrees_slow(tier: str):
    status, message = _run(tier, INTEGER_ARITHMETIC)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


def test_true_division_yields_float_everywhere():
    """`Int / Int` is Float, so declaring the result `Int` is a type error.
    This is the fix for what used to be a soundness break: the checker said
    `Int` and two of three tiers produced 3.5."""
    from revl.errors import RevlError
    with pytest.raises(RevlError) as failure:
        compile_source("pub fn d() -> Int { return 7 / 2 }", "div.rvl")
    assert "Float" in str(failure.value)
    compile_source("pub fn d() -> Float { return 7 / 2 }", "div.rvl")


# ------------------------------------------------- division by zero
#
# Integer division and modulo have no value at zero. A *literal* zero is
# refused by the checker (examples/rejections/arith_zero_divisor.rvl); a
# computed one has to fault at runtime, and the point is that every tier
# faults rather than one of them inventing a value. TypeScript used to return
# Infinity/NaN here — a value where the checker had declared `Int`, the same
# class of unsoundness as lowering structural `==` to JS `===`.

ZERO_DIVISOR = """
pub fn zero() -> Int { return 0 }
test "integer division by zero has no value" { assert 7.div_trunc(zero()) == 0 }
"""


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_integer_division_by_zero_faults(tier: str):
    status, message = _run(tier, ZERO_DIVISOR)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier} did not fault on a zero divisor ({status}) — a tier that "
        f"returns a value here is unsound, not lenient: {message}")


# ------------------------------------------------- the total division forms
#
# The faulting operations have a total, value-returning counterpart:
# `checked_div_trunc` / `checked_div_floor` / `checked_div_euclid` /
# `checked_mod` return `Result[Int, Str]` — Ok(quotient), or Err(reason) at a
# zero divisor instead of the fault. `fail` cannot serve here (it is a
# component construct and is refused in a pure fn), so the error travels as a
# value (docs/arithmetic.md). The rounding convention is the *same* one each
# named operation already specifies — only the zero-divisor behaviour differs.
#
# A literal zero divisor is refused for the faulting operations
# (arith_zero_divisor.rvl) and deliberately accepted for these: handling it
# is the entire point.

CHECKED_DIVISION = """
pub fn qt(a: Int, b: Int) -> Result[Int, Str] { return a.checked_div_trunc(b) }
pub fn qf(a: Int, b: Int) -> Result[Int, Str] { return a.checked_div_floor(b) }
pub fn qe(a: Int, b: Int) -> Result[Int, Str] { return a.checked_div_euclid(b) }
pub fn qm(a: Int, b: Int) -> Result[Int, Str] { return a.checked_mod(b) }
pub fn qt_is(a: Int, b: Int, want: Int, iserr: Bool) -> Bool {
  return match qt(a, b) { Ok(v) => !iserr && v == want, Err(e) => iserr && e == "revl: division by zero" }
}
pub fn qf_is(a: Int, b: Int, want: Int, iserr: Bool) -> Bool {
  return match qf(a, b) { Ok(v) => !iserr && v == want, Err(e) => iserr && e == "revl: division by zero" }
}
pub fn qe_is(a: Int, b: Int, want: Int, iserr: Bool) -> Bool {
  return match qe(a, b) { Ok(v) => !iserr && v == want, Err(e) => iserr && e == "revl: division by zero" }
}
pub fn qm_is(a: Int, b: Int, want: Int, iserr: Bool) -> Bool {
  return match qm(a, b) { Ok(v) => !iserr && v == want, Err(e) => iserr && e == "revl: division by zero" }
}
test "checked trunc rounds toward zero"   { assert qt_is(7, 2, 3, false) }
test "checked trunc on negatives"         { assert qt_is(0 - 7, 2, 0 - 3, false) }
test "checked trunc yields Err at zero"   { assert qt_is(7, 0, 0, true) }
test "checked floor rounds toward -inf"   { assert qf_is(0 - 7, 2, 0 - 4, false) }
test "checked floor yields Err at zero"   { assert qf_is(5, 0, 0, true) }
test "checked euclid, both negative"      { assert qe_is(0 - 7, 0 - 2, 4, false) }
test "checked euclid yields Err at zero"  { assert qe_is(5, 0, 0, true) }
test "checked mod is never negative"      { assert qm_is(0 - 7, 3, 2, false) }
test "checked mod yields Err at zero"     { assert qm_is(5, 0, 0, true) }
"""


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_checked_division_returns_values(tier: str):
    status, message = _run(tier, CHECKED_DIVISION)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_checked_division_returns_values_slow(tier: str):
    status, message = _run(tier, CHECKED_DIVISION)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


def test_literal_zero_divisor_accepted_for_checked_forms():
    """The checker refuses `x.mod(0)` (arith_zero_divisor.rvl) because it is
    not a program anyone meant to write. Passing a literal zero to the total
    form IS such a program — handling it is what it is for."""
    compile_source(
        "pub fn d(b: Int) -> Result[Int, Str] { return 7.checked_div_trunc(b) }\n"
        "pub fn z() -> Result[Int, Str] { return 7.checked_mod(0) }\n",
        "checked_lit_zero.rvl")


def test_typescript_guards_the_divisor():
    """The guard travels with the module, and every named integer operation
    routes through it rather than through a bare `/`."""
    emitted = _emit("typescript", INTEGER_ARITHMETIC)
    assert "function revlNonZero" in emitted
    for helper in ("revlDivTrunc", "revlDivFloor", "revlDivEuclid", "revlMod"):
        assert f"function {helper}" in emitted, helper
    assert "revl: division by zero" in emitted


def test_checked_division_lowers_on_every_tier():
    """The total forms produce a Result value on each tier through that
    tier's Result representation: tagged objects on TS, std Result on rust,
    RevlResult on java, RevlOk/RevlErr on go, tagged cells on wasm."""
    for helper in ("revlCheckedDivTrunc", "revlCheckedDivFloor",
                   "revlCheckedDivEuclid", "revlCheckedMod"):
        assert f"function {helper}" in _emit("typescript", CHECKED_DIVISION), helper
    assert 'Err::<i64, String>("revl: division by zero".to_string())' in \
        _emit("rust", CHECKED_DIVISION)
    java = _emit("java", CHECKED_DIVISION)
    assert "revlCheckedDivTrunc" in java and "RevlResult<Long, String>" in java
    go = _emit("go", CHECKED_DIVISION)
    assert "RevlOk[int64, string]" in go and "RevlErr[int64, string]" in go
    wasm = _emit("wasm", CHECKED_DIVISION)
    wat = wasm["functions"] if isinstance(wasm, dict) else wasm
    assert "(i64.eqz" in wat and "revl: division by zero" in wat


# --------------------------------------------------------- the pairing law
#
# Every remainder pairs with a division, and the pair satisfies
# (a div b) * b + (a rem b) == a. Publishing the law and testing it is what
# makes the choice of convention checkable rather than a matter of taste:
#
#   div_trunc  pairs with  %      (truncated, sign of the dividend)
#   div_euclid pairs with  mod    (Euclidean, remainder always >= 0)
#
# `div_floor` has no remainder partner in the surface, deliberately: what
# people reach for floored `%` to get is index safety, and `mod` gives that
# strictly better — non-negative for *either* sign of the divisor.

PAIRING_LAW = "\n".join(
    [f'test "trunc/rem {a} {b}" '
     f'{{ assert ({a}).div_trunc({b}) * ({b}) + ({a}) % ({b}) == ({a}) }}'
     for a, b in [("7", "3"), ("0 - 7", "3"), ("7", "0 - 3"), ("0 - 7", "0 - 3"),
                  ("9", "3"), ("0 - 9", "3"), ("1", "7"), ("0 - 1", "7")]]
    + [f'test "euclid/mod {a} {b}" '
       f'{{ assert ({a}).div_euclid({b}) * ({b}) + ({a}).mod({b}) == ({a}) }}'
       for a, b in [("7", "3"), ("0 - 7", "3"), ("7", "0 - 3"), ("0 - 7", "0 - 3"),
                    ("9", "3"), ("0 - 9", "3"), ("1", "7"), ("0 - 1", "7")]]
)


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_division_remainder_pairing_law(tier: str):
    status, message = _run(tier, PAIRING_LAW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} breaks the pairing law: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_division_remainder_pairing_law_slow(tier: str):
    status, message = _run(tier, PAIRING_LAW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} breaks the pairing law: {message}"


# ------------------------------------------------------------ IEEE 754 Float
#
# `Float` is IEEE 754 binary64. Every host implements it natively, so the
# commitment costs nothing to honour — but it has to be *tested*, because two
# tiers did not honour it: python raised where IEEE says +/-infinity, and rust
# rendered `Int / Int` as integer division against an f64 the checker had
# declared.
#
# Two consequences worth stating in a test rather than only in prose: `==` on
# Float is IEEE, so NaN is not equal to itself (which is why an assertion
# cannot go through vitest's `toStrictEqual`), and `<` is therefore a partial
# order.

IEEE_FLOAT = """
test "true division is exact"        { assert 7 / 2 == 3.5 }
test "float literals and arithmetic" { assert 1.5 + 2.5 == 4.0 }
test "binary floating point"         { assert 0.1 + 0.2 != 0.3 }
test "NaN is not equal to itself"    { assert 0.0 / 0.0 != 0.0 / 0.0 }
test "division by zero is infinite"  { assert 1.0 / 0.0 > 1.0e308 }
test "negative infinity"             { assert (0.0 - 1.0) / 0.0 < (0.0 - 1.0e308) }
test "negative zero equals zero"     { assert 0.0 - 0.0 == 0.0 }
test "exponent literals"             { assert 1.5e3 == 1500.0 }
"""


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_ieee_float_semantics(tier: str):
    status, message = _run(tier, IEEE_FLOAT)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} is not IEEE 754: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_ieee_float_semantics_slow(tier: str):
    status, message = _run(tier, IEEE_FLOAT)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} is not IEEE 754: {message}"


def test_wasm_refuses_float_by_name():
    """The wasm tier lowers Int/Bool and nothing else numeric, so `Float` is a
    *deliberate* limit — it must refuse with a reason rather than emit
    something that quietly is not IEEE.
    """
    from revl.errors import RevlError
    import importlib.util as _il
    spec = _il.spec_from_file_location("emit_wasm_ieee", ROOT / "backends" / "wasm" / "emit.py")
    module = _il.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises((RevlError, Exception)) as failure:
        module.emit(compile_source("pub fn f() -> Float { return 1.5 }", "f.rvl"))
    assert "Float" in str(failure.value)


def test_go_equality_is_not_native_comparison():
    """Go `==` is a *compile error* on slices ("slice can only be compared to
    nil"), so a record holding a List cannot use it at all, and comparable
    structs compare field-wise rather than structurally through references.
    Non-scalars route through revlEq; scalars keep the native operator so the
    common case costs nothing."""
    emitted = _emit("go", PROBES["structural equality"])
    assert "func revlEq(a, b any) bool" in emitted, emitted
    assert "reflect.DeepEqual" in emitted


def test_go_float_literals_are_typed():
    """Go folds *untyped constant* arithmetic at arbitrary precision, so a bare
    `0.1 + 0.2` equals exactly `0.3` at compile time — not IEEE 754 binary64.
    Typing each literal forces ordinary float64 arithmetic."""
    emitted = _emit("go", IEEE_FLOAT)
    assert "float64(0.1)" in emitted, emitted


def test_go_true_division_goes_through_a_function():
    """Go rejects a *constant* `1.0 / 0.0` at compile time where IEEE defines
    +Inf, and `/` on two int64 is integer division. The helper is both fixes."""
    emitted = _emit("go", IEEE_FLOAT)
    assert "func revlDiv(a, b float64) float64" in emitted


# ------------------------------------------------- Int is bounded, and traps
#
# `Int` is 64-bit two's complement and overflow TRAPS. Silent wraparound is
# precisely the failure mode revl exists to remove — a guarantee that holds
# only in debug builds (rust's default) is not a guarantee.
#
# One tier cannot express this yet and is recorded rather than pretended:
#   * wasm is i32 throughout the emitter — narrower than every other tier.
#     WebAssembly has native i64; the i32 is expedience, not a constraint.
# It is recorded in docs/arithmetic.md with the port it needs. typescript used
# to sit beside it (Int was `number`, an f64 exact only to 2^53); it now maps
# Int to `bigint` and imposes the 64-bit bound the way python does, so it joins
# BOUNDED_TIERS below.

INT_IN_RANGE = """
test "small arithmetic"   { assert 2 + 2 == 4 }
test "near the bound"     { assert 9223372036854775807 - 1 == 9223372036854775806 }
test "multiplication"     { assert 1000000 * 1000000 == 1000000000000 }
test "negative bound"     { assert (0 - 9223372036854775807) + 1 == 0 - 9223372036854775806 }
test "precision past 2^53" { assert 9007199254740993 - 9007199254740992 == 1 }
test "negation in range"  { assert -(0 - 42) == 42 }
"""

INT_OVERFLOW = """
pub fn big() -> Int { return 9223372036854775807 }
pub fn one() -> Int { return 1 }
test "overflow must not produce a value" { assert big() + one() == 0 }
"""

# tiers that can represent a 64-bit Int today
BOUNDED_TIERS = ("py", "ts", "go")
BOUNDED_SLOW = ("rust", "java")


@pytest.mark.parametrize("tier", BOUNDED_TIERS)
def test_int_in_range_arithmetic(tier: str):
    status, message = _run(tier, INT_IN_RANGE)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", BOUNDED_TIERS)
def test_int_overflow_traps(tier: str):
    """A tier that *returns* here has silently wrapped, which is the whole
    thing this guarantee exists to prevent."""
    status, message = _run(tier, INT_OVERFLOW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier} did not trap on Int overflow ({status}) — it wrapped or grew: "
        f"{message}")


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", BOUNDED_SLOW)
def test_int_overflow_traps_slow(tier: str):
    status, message = _run(tier, INT_OVERFLOW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", f"{tier} did not trap on Int overflow: {message}"


def test_every_bounded_tier_uses_the_same_trap_message():
    """One guarantee should not read as three different bugs."""
    for backend, needle in (("rust", 'expect("revl: Int overflow")'),
                            ("go", 'panic("revl: Int overflow")'),
                            ("python", "revl: Int overflow"),
                            ("typescript", "throw new RangeError('revl: Int overflow')"),
                            ("java", "Math.addExact")):
        emitted = _emit(backend, INT_OVERFLOW)
        assert needle in emitted, (backend, emitted[:400])


# ------------------------------------------------- unary minus re-imposes the bound
#
# Negation is `0 - x`, and `0 - Int.MIN` overflows — so `-x` on an Int is
# arithmetic and must fault at the edge like every other operation. python
# imposed the bound on `+`, `-` and `*` but let unary minus lower to the
# host's operator, so `-Int.MIN` came back as 2^63: out of the very range
# python imposes elsewhere, silently. It now goes through `_revl_i64`.
# The tiers that do NOT trap are pinned in DIVERGENCES below.

NEG_INT_MIN = """
pub fn lo() -> Int { return 0 - 9223372036854775807 - 1 }
pub fn neg(x: Int) -> Int { return -x }
test "negating Int.MIN must not produce a value" { assert neg(lo()) == 0 }
"""


def test_python_negation_of_int_min_traps(capsys):
    """python is arbitrary precision, so it *imposes* the bound rather than
    detects it — and negation used to slip past that imposition."""
    status, message = _run("py", NEG_INT_MIN)
    if status == "skip":
        pytest.skip(message)
    assert status == "fail", f"python returned a value for -Int.MIN: {message}"
    # the runner reports the per-test fault on stdout, not in its summary
    assert "revl: Int overflow" in capsys.readouterr().out


def test_python_emits_int_negation_through_the_bound():
    """The static half of the guarantee: an Int negation must render through
    the bound helper, not the host's unary minus."""
    emitted = _emit("python", NEG_INT_MIN)
    assert "_revl_i64(-" in emitted


@pytest.mark.parametrize("tier", BOUNDED_TIERS)
def test_negation_of_int_min_traps(tier: str):
    """`-Int.MIN` overflows, so every bounded tier must fault rather than wrap
    or grow past the range. go/java/ts used to be the exceptions (DIVERGENCES);
    they now emit the checked form."""
    status, message = _run(tier, NEG_INT_MIN)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier} did not trap on -Int.MIN ({status}) — it wrapped or grew: "
        f"{message}")


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", BOUNDED_SLOW)
def test_negation_of_int_min_traps_slow(tier: str):
    status, message = _run(tier, NEG_INT_MIN)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", f"{tier} did not trap on -Int.MIN: {message}"


def test_bounded_tiers_emit_checked_int_negation():
    """The static half for go/java/ts: an Int negation renders through the
    tier's checked mechanism, never the host's wrapping unary minus."""
    assert "revlSub(0, " in _emit("go", NEG_INT_MIN)
    assert "Math.negateExact(" in _emit("java", NEG_INT_MIN)
    assert "revlI64(-" in _emit("typescript", NEG_INT_MIN)


def test_wasm_int_is_i64_and_checks_the_bound():
    """wasm is a bounded tier now, so the emitted module must carry the check.

    It is the one tier that cannot carry the *message*: a wasm trap has no
    payload, so `unreachable` is the whole fault. That is a property of the
    instruction set, and it is why this tier is asserted here on the shape of
    the check rather than on the text — with the behaviour itself executed on
    real wasmtime in `backends/wasm/test_v3_emit.py`."""
    spec = importlib.util.spec_from_file_location(
        "emit_wasm_bounded", ROOT / "backends" / "wasm" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    emitted = module.emit(compile_source(
        "pub fn big() -> Int { return 9223372036854775807 }\n"
        "pub fn plus(a: Int, b: Int) -> Int { return a + b }\n",
        "cross_tier_exec.rvl"))["functions"]
    # the literal is a 64-bit value, not a 32-bit one
    assert "(i64.const 9223372036854775807)" in emitted
    assert '(param $p_a i64) (param $p_b i64) (result i64)' in emitted
    # `+` does not lower to a bare add: it goes through the checked helper,
    # and the helper faults rather than wrapping
    assert "(call $int_add)" in emitted
    assert "(func $int_add (param $a i64) (param $b i64) (result i64)" in emitted
    assert "unreachable" in emitted
    assert "(i32.add)" not in emitted
