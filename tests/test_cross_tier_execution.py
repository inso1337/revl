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

DIVERGENCES = {
    # `Int` is 64-bit on every tier that can express it — python imposes the
    # bound, rust/java/go/wasm are all 64-bit — and TypeScript is the one that
    # cannot: it maps Int to f64, exact only to 2^53, so it loses precision
    # silently here and needs BigInt rather than a type annotation. This is the
    # one remaining open decision recorded in docs/arithmetic.md.
    "Int keeps precision past 2^53": (
        'test "p" { assert 9007199254740993 - 9007199254740992 == 1 }',
        {"py": "pass", "ts": "fail", "rust": "pass", "go": "pass", "java": "pass"},
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


def test_typescript_guards_the_divisor():
    """The guard travels with the module, and every named integer operation
    routes through it rather than through a bare `/`."""
    emitted = _emit("typescript", INTEGER_ARITHMETIC)
    assert "function revlNonZero" in emitted
    for helper in ("revlDivTrunc", "revlDivFloor", "revlDivEuclid", "revlMod"):
        assert f"function {helper}" in emitted, helper
    assert "revl: division by zero" in emitted


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
# One tier cannot express this yet and is pinned rather than pretended:
# typescript maps Int to `number` (f64), exact only to 2^53, so it cannot
# represent a 64-bit Int at all. It needs BigInt, and that is recorded in
# docs/arithmetic.md.
#
# wasm was the other one, and no longer is: its emitter was i32 throughout,
# and `Int` values there are i64 now with `$int_add`/`$int_sub`/`$int_mul`
# testing for overflow and trapping. It has no in-language test runner (see
# `revl.test.run_wasm` — `test` blocks are host-side on that tier), so it
# cannot join the executed matrix above; the equivalent claims are executed
# against real wasmtime in `backends/wasm/test_v3_emit.py`
# (`test_v3_int_is_64_bit_on_wasmtime`,
# `test_v3_int_overflow_traps_on_wasmtime`,
# `test_v3_named_integer_arithmetic_runs_on_wasmtime`). The static guard below
# keeps that from being a claim nobody checks from here.

INT_IN_RANGE = """
test "small arithmetic"   { assert 2 + 2 == 4 }
test "near the bound"     { assert 9223372036854775807 - 1 == 9223372036854775806 }
test "multiplication"     { assert 1000000 * 1000000 == 1000000000000 }
test "negative bound"     { assert (0 - 9223372036854775807) + 1 == 0 - 9223372036854775806 }
"""

INT_OVERFLOW = """
pub fn big() -> Int { return 9223372036854775807 }
pub fn one() -> Int { return 1 }
test "overflow must not produce a value" { assert big() + one() == 0 }
"""

# tiers that can represent a 64-bit Int today
BOUNDED_TIERS = ("py", "go")
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
                            ("java", "Math.addExact")):
        emitted = _emit(backend, INT_OVERFLOW)
        assert needle in emitted, (backend, emitted[:400])


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
