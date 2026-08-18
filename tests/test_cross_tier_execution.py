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

FAST_TIERS = ("py", "ts")
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
    # python's `%` still floors (-7 % 3 == 2) where rust, java and JS truncate
    # (== -1). Unlike division this is NOT settled: §0 says `%` should mean
    # what TypeScript means (truncated), so python is the outlier to move —
    # but that changes existing programs, so it stays pinned until decided.
    # Anyone who wants a defined answer today has `mod` (docs/arithmetic.md).
    "modulo truncates toward zero": (
        'test "m" { assert (0 - 7) % 3 == 0 - 1 }',
        {"py": "fail", "ts": "pass", "rust": "pass"},
    ),
    # TS numbers are f64, so Int arithmetic silently loses precision past 2^53.
    # Unlike the others this needs BigInt, not a type annotation.
    "Int keeps precision past 2^53": (
        'test "p" { assert 9007199254740993 - 9007199254740992 == 1 }',
        {"py": "pass", "ts": "fail", "rust": "pass"},
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
