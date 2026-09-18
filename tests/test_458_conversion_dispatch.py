"""Conversion and parsing across the six tiers: four cross-tier defects (#721).

The seam is every conversion revl actually exposes — `Str.to_int()` (the FR-9
parse, docs/stdlib-2.0.md §Str.to_int), `Int32.to_int()` (the widen),
`Int.to_int32()` (the checked narrow), `Int.to_str()` and `Float.to_str()`
(docs/stdlib-2.0.md §Int.to_str), `charCodeAt`/`codepoint_at`, the implicit
Int->Float widen and the `${…}` rendering of each — crossed with the edges the
host libraries disagree on: a non-numeric string, a magnitude past `Int.MAX`,
leading `+`/whitespace/underscores/`0x`, an empty string, `Int.MIN`, the ES
exponent thresholds, `-0.0`, and both round trips.

MEASURED over 909 probe programs, each run on the five non-reference tiers
against the py answer: 4545 tier verdicts. After the fixes below: ts and rust
execute all 909, go 829 (80 named refusals, the four ASCII classifiers it does
not lower), java the same, wasm 706 (203 named refusals — every Float position
and the same classifiers), and NOTHING disagrees. Plus 239 optional-chaining
probes (130 spelled with `match`, 109 with `??`, 99 of those also on go) and 36
`to_int32` overflow probes. FOUR things disagreed, all of them below.
Everything else agreed, including every `Str.to_int` spelling the doc
enumerates, every Float rendering threshold, both round trips, and the
six-tier fault on a `to_int32` out of range.

    1. `"18446744073709551616".to_int()`   go **Some(0)**, every other tier None
       `"18446744073709551617".to_int()`   go **Some(1)**
       `"92233720368547758080".to_int()`   go **Some(0)**

       `revlParseInt` accumulates `n = n*10 + d` in a uint64 and tested the
       bound AFTER the step. uint64 arithmetic wraps: at n == 2**63 the next
       `n*10` is exactly 5*2**64 and comes back 0, so a magnitude that wrapped
       under the limit was accepted and answered a VALUE. 152 of the 313 parse
       inputs hit it. The fix is the pre-check `n > (lim-d)/10` the wasm helper
       already made, which admits `n*10+d` exactly when that value is <= lim.

    2. `let x: Float = 0.5` on wasm     **the emitted module does not validate**

       "type mismatch: expected i32, found f64", from wasmtime at load. Every
       local on that tier is an i32 except an `Int`, and a Float value is an
       f64. A Float parameter, return, comparison, equality, list element,
       variant payload and `.to_str()` are each refused BY NAME there; the
       binding was the one Float position that emitted instead — even for a
       program that never read it. It is the same named refusal now
       (docs/wasm-capabilities.md). An interpolated Float EXPRESSION that is
       never bound (`` `${3.0}` ``) still lowers, inside the documented
       `$f64_to_str` fence.

    3. `?.to_int()` / `?.to_str()` / `?.to_int32()` on ts  **TypeError at run time**

       `o?.to_int is not a function`. The ts optcall path emitted
       `payload?.m(..)` verbatim, and 14 of the 29 builtin forms are not JS
       methods: length, codepoint_at, to_int, to_str, to_int32, the four named
       divisions, the two checked forms and the four ASCII classifiers. 59 of
       99 optcall probes failed on that tier. It renders through `_ts_builtin`
       now — the table a plain `.m(..)` already used.

       Same node, same cause, two more tiers:
         * py answered **'3.0'** for `Opt[Float]?.to_str()`, where every other
           route to that conversion answers `'3'`, and raised
           **NameError: _REVL_I64_MIN** for `?.div_trunc()`/`?.div_floor()`/
           `?.div_euclid()` and **_revl_i32** for `?.to_int32()` — the
           preamble gate read only `to_int` off an optcall node. That is the
           REFERENCE tier: a document whose only bounded-Int use is through
           `?.` did not run at all.
         * go emitted `revlListLen` for an `Opt[Str]?.length()` and **the
           package did not build**; `concat`, `slice`, `indexOf` and `to_str`
           took their List/Int branch for the same reason (the synthetic
           receiver node carried no type).

       rust, java and wasm refuse `?.` by name ("optional chaining (`?.`) is
       not yet lowerable on the … tier"), which is why they had nothing wrong
       to say.

WHAT DID NOT MOVE, so the negative result is on the record: `Str.to_int` agrees
on all six tiers for empty, `"-"`, `"+7"`, `" 42"`, `"42\\n"`, `"12a"`, `"1_0"`,
`"0x10"`, `"1e3"`, non-ASCII digits (`"٤٢"`, `"４２"`, `"²"`), `"007"`, both
`Int.MIN` spellings and every magnitude around 2**63; `Float.to_str` agrees on
every ES threshold probed (1e21, 1e-7, -0.0, 5e-324, NaN, ±Infinity,
9007199254740993.0) and byte-for-byte with `${…}`; `Int.to_str` agrees at
`Int.MIN`/`Int.MAX`; `to_int32` faults on all six tiers out of range; and both
round trips hold everywhere.

NON-VACUITY. On the tree without these fixes 15 of this file's tests fail and
28 pass; the 28 are the controls, and they pass on BOTH trees. The failures are
go on the overflow family and its static shape, wasm on both binding refusals,
py/ts/go on all four optcall documents, and the four static dispatch checks.
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

ALL_TIERS = ("py", "ts", "go", "wasm")
SLOW_TIERS = ("rust", "java")

#: `?.` is refused by name on rust, java and wasm, so the optional-chaining
#: probes run where it lowers. go is included through the `??` spelling only —
#: a `match` over an optcall result is a separate, named go refusal
#: ("cannot resolve match case 'Some'").
OPT_TIERS = ("py", "ts")


def _run(tier: str, source: str):
    return RUNNERS[tier](compile_source(source, "conv_458.rvl"))


def _emit(backend: str, source: str) -> str:
    emitted = backend_emitter(backend).emit(compile_source(source))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


# ------------------------------------------------ 1. the parse overflow edge

#: Each magnitude is written as a literal STRING, which is the whole point: the
#: value never exists as an `Int`, so nothing but the parse can decide it.
PARSE_OVERFLOW = """
pub fn show(s: Str) -> Str {
  return match s.to_int() { Some(v) => v.to_str(), None => "None" }
}
test "2^64 is None"            { assert show("18446744073709551616") == "None" }
test "2^64+1 is None"          { assert show("18446744073709551617") == "None" }
test "10*2^63 is None"         { assert show("92233720368547758080") == "None" }
test "10*2^63+1 is None"       { assert show("92233720368547758081") == "None" }
test "negative 2^64 is None"   { assert show("-18446744073709551616") == "None" }
test "negative 10*2^63 is None" { assert show("-92233720368547758080") == "None" }
test "twenty nines is None"    { assert show("99999999999999999999") == "None" }
test "thirty ones is None"     { assert show("111111111111111111111111111111") == "None" }
"""

#: The boundary the overflow rule must NOT move: everything up to and including
#: Int.MAX parses, Int.MIN parses (its magnitude is 2**63, the one out-of-|MAX|
#: magnitude that is in range), and the two nearest neighbours do not.
PARSE_BOUNDARY = """
pub fn show(s: Str) -> Str {
  return match s.to_int() { Some(v) => v.to_str(), None => "None" }
}
test "Int.MAX parses"       { assert show("9223372036854775807") == "9223372036854775807" }
test "Int.MAX+1 is None"    { assert show("9223372036854775808") == "None" }
test "Int.MIN parses"       { assert show("-9223372036854775808") == "-9223372036854775808" }
test "Int.MIN-1 is None"    { assert show("-9223372036854775809") == "None" }
test "leading zeros do not lift the bound" {
  assert show("0009223372036854775808") == "None"
}
test "leading zeros keep a value in range" {
  assert show("0009223372036854775807") == "9223372036854775807"
}
"""

#: CONTROL, passing on both trees: the spellings docs/stdlib-2.0.md §Str.to_int
#: enumerates. The overflow fix must not narrow or widen the accepted set.
PARSE_SPELLINGS = """
pub fn show(s: Str) -> Str {
  return match s.to_int() { Some(v) => v.to_str(), None => "None" }
}
test "digits"            { assert show("42") == "42" }
test "leading minus"     { assert show("-7") == "-7" }
test "leading zeros"     { assert show("007") == "7" }
test "minus zero"        { assert show("-0") == "0" }
test "empty"             { assert show("") == "None" }
test "bare minus"        { assert show("-") == "None" }
test "leading plus"      { assert show("+7") == "None" }
test "leading space"     { assert show(" 42") == "None" }
test "trailing space"    { assert show("42 ") == "None" }
test "trailing newline"  { assert show("42\\n") == "None" }
test "partial digits"    { assert show("12a") == "None" }
test "underscored"       { assert show("1_0") == "None" }
test "hex prefixed"      { assert show("0x10") == "None" }
test "exponent form"     { assert show("1e3") == "None" }
test "decimal point"     { assert show("1.0") == "None" }
test "non-ascii digits"  { assert show("\\u0664\\u0662") == "None" }
test "fullwidth digits"  { assert show("\\uff14\\uff12") == "None" }
test "superscript two"   { assert show("\\u00b2") == "None" }
"""

#: CONTROL: every Int renders and every rendering parses back to it, at the
#: edges. The pairing law `Str.to_int` is specified against.
ROUND_TRIP = """
pub fn trip(x: Int) -> Str {
  return match x.to_str().to_int() { Some(v) => (v - x).to_str(), None => "lost" }
}
test "zero"     { assert trip(0) == "0" }
test "Int.MAX"  { assert trip(9223372036854775807) == "0" }
test "Int.MIN"  { assert trip((0 - 9223372036854775807) - 1) == "0" }
test "negative" { assert trip(0 - 42) == "0" }
"""


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_a_magnitude_past_the_int_bound_is_none(tier: str):
    status, message = _run(tier, PARSE_OVERFLOW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_a_magnitude_past_the_int_bound_is_none_slow(tier: str):
    status, message = _run(tier, PARSE_OVERFLOW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_the_parse_boundary_is_exactly_the_int_range(tier: str):
    status, message = _run(tier, PARSE_BOUNDARY)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_the_accepted_spellings_are_unchanged(tier: str):
    """NON-VACUITY: the accepted set is the one docs/stdlib-2.0.md publishes,
    on both trees. Tightening an overflow test by rejecting more is the way
    this is most easily got wrong."""
    status, message = _run(tier, PARSE_SPELLINGS)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_render_then_parse_is_the_identity(tier: str):
    """NON-VACUITY, the other direction: every Int still renders and re-parses,
    Int.MIN included."""
    status, message = _run(tier, ROUND_TRIP)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


def test_the_go_parse_checks_its_bound_before_the_accumulate_step():
    """The static half, so a tier with no go toolchain still holds the shape.

    `n = n*10 + d` then `n > lim` is the defect: uint64 wraps, so the test can
    be passed by a magnitude that came back under the limit."""
    emitted = _emit("go", 'pub fn p(s: Str) -> Opt[Int] { return s.to_int() }')
    assert "if n > (lim-d)/10 {" in emitted, (
        "the go parse is back to testing the bound after a wrapping "
        "`n = n*10 + uint64(c-'0')`")
    assert "\t\tn = n*10 + uint64(c-'0')\n\t\tif n > lim {" not in emitted


# --------------------------------------- 2. a Float binding on the wasm tier

def test_wasm_refuses_a_float_binding_by_name():
    """The tier cannot hold a Float local — every local is an i32 but an Int —
    and it fails CLOSED now: an emit-time refusal naming the type, not a module
    wasmtime rejects at load (docs/wasm-capabilities.md)."""
    with pytest.raises(Exception) as excinfo:
        _emit("wasm", "pub fn probe() -> Int { let x: Float = 0.5 return 7 }")
    assert "type 'Float' is not lowerable" in str(excinfo.value), excinfo.value


def test_wasm_refuses_a_float_binding_even_where_it_is_read():
    """The shape that surfaced it: a `${…}` template over a bound Float. The
    same refusal, at the binding, before the rendering is reached."""
    with pytest.raises(Exception) as excinfo:
        _emit("wasm", "pub fn probe() -> Str { let x: Float = 0.5 return `${x}` }")
    assert "type 'Float' is not lowerable" in str(excinfo.value), excinfo.value


WASM_INLINE_FLOAT = """
pub fn probe() -> Str { return `${3.0}` }
pub fn summed() -> Str { return `${1.0 + 2.0}` }
test "an integral Float literal renders" { assert probe() == "3" }
test "an integral Float sum renders"     { assert summed() == "3" }
"""


def test_wasm_still_renders_an_unbound_integral_float():
    """NON-VACUITY, and the boundary of the refusal: the documented
    `$f64_to_str` subset (an integer-valued finite, never bound to a local)
    still lowers and still runs. A refusal that swallowed this would have taken
    a working path down with it."""
    status, message = _run("wasm", WASM_INLINE_FLOAT)
    if status == "skip":
        pytest.skip(f"wasm: {message}")
    assert status == "pass", f"wasm: {message}"


# -------------------------------- 3. the conversions reached through `?.`

OPT_CONVERSIONS = """
pub fn some_str(s: Str) -> Opt[Str] { return Some(s) }
pub fn some_int(n: Int) -> Opt[Int] { return Some(n) }
pub fn some_float(f: Float) -> Opt[Float] { return Some(f) }
pub fn show(n: Int) -> Str { return n.to_str() }

pub fn parsed(s: Str) -> Str {
  // `?.to_int()` is `Opt[Opt[Int]]`, and the two levels are one value on the
  // tiers whose Opt None is a host absence, so the nesting is collapsed with
  // `??` rather than matched: -1 stands for "no Int came back", whichever
  // level said so.
  let o = some_str(s)
  let inner: Opt[Int] = o?.to_int() ?? None
  let v: Int = inner ?? (0 - 1)
  return show(v)
}
pub fn rendered(n: Int) -> Str {
  let o = some_int(n)
  return match o?.to_str() { Some(v) => v, None => "absent" }
}
pub fn rendered_float(f: Float) -> Str {
  let o = some_float(f)
  return match o?.to_str() { Some(v) => v, None => "absent" }
}
pub fn narrowed(n: Int) -> Str {
  let o = some_int(n)
  return match o?.to_int32() { Some(v) => show(v.to_int()), None => "absent" }
}
pub fn counted(s: Str) -> Str {
  let o = some_str(s)
  return match o?.length() { Some(v) => show(v), None => "absent" }
}

test "a parsed digit string"   { assert parsed("42") == "42" }
test "a parse that fails"      { assert parsed("+7") == "-1" }
test "a parse past the bound"  { assert parsed("18446744073709551616") == "-1" }
test "an Int renders"          { assert rendered(0 - 42) == "-42" }
test "Int.MIN renders"         { assert rendered((0 - 9223372036854775807) - 1) == "-9223372036854775808" }
test "a Float renders canonically" { assert rendered_float(3.0) == "3" }
test "a small Float keeps the exponent" { assert rendered_float(1.0e-7) == "1e-7" }
test "a narrowed Int widens back" { assert narrowed(0 - 2147483648) == "-2147483648" }
test "a code-point length"     { assert counted("\U0001f600z") == "2" }
"""

#: The same node against the three named divisions and the Int32 narrow, in a
#: document whose ONLY bounded-Int use is through `?.` — which is what made the
#: py preamble gate observable at all.
OPT_ARITHMETIC = """
pub fn some_int(n: Int) -> Opt[Int] { return Some(n) }
pub fn show(n: Int) -> Str { return n.to_str() }
pub fn trunc(n: Int) -> Str {
  let o = some_int(n)
  return match o?.div_trunc(2) { Some(v) => show(v), None => "absent" }
}
pub fn floored(n: Int) -> Str {
  let o = some_int(n)
  return match o?.div_floor(2) { Some(v) => show(v), None => "absent" }
}
pub fn euclid(n: Int) -> Str {
  let o = some_int(n)
  return match o?.div_euclid(2) { Some(v) => show(v), None => "absent" }
}
pub fn narrow(n: Int) -> Str {
  let o = some_int(n)
  return match o?.to_int32() { Some(v) => show(v.to_int()), None => "absent" }
}
test "truncating division"  { assert trunc(7) == "3" }
test "a negative truncates toward zero" { assert trunc(0 - 7) == "-3" }
test "flooring division"    { assert floored(0 - 7) == "-4" }
test "euclidean division"   { assert euclid(0 - 7) == "-4" }
test "the Int32 narrow"     { assert narrow(7) == "7" }
"""

#: CONTROL, passing on both trees: the `?.` forms that happened to be JS
#: methods with the right semantics already, and the absent case.
OPT_UNCHANGED = """
pub fn some_str(s: Str) -> Opt[Str] { return Some(s) }
pub fn no_str() -> Opt[Str] { return None }
pub fn show(n: Int) -> Str { return n.to_str() }
pub fn cut(s: Str) -> Str {
  let o = some_str(s)
  return match o?.slice(0, 2) { Some(v) => v, None => "absent" }
}
pub fn at(s: Str) -> Str {
  let o = some_str(s)
  return match o?.charAt(1) { Some(v) => v, None => "absent" }
}
pub fn found(s: Str) -> Str {
  let o = some_str(s)
  return match o?.indexOf("c") { Some(v) => show(v), None => "absent" }
}
pub fn missing() -> Str {
  let o = no_str()
  return match o?.indexOf("c") { Some(v) => show(v), None => "absent" }
}
test "slice"        { assert cut("abcd") == "ab" }
test "charAt"       { assert at("abcd") == "b" }
test "indexOf"      { assert found("abcd") == "2" }
test "a None short-circuits" { assert missing() == "absent" }
"""

OPT_DOCUMENTS = {
    "the conversions": OPT_CONVERSIONS,
    "the named divisions": OPT_ARITHMETIC,
}


@pytest.mark.parametrize("name", sorted(OPT_DOCUMENTS))
@pytest.mark.parametrize("tier", OPT_TIERS)
def test_an_optional_chained_conversion_agrees(tier: str, name: str):
    status, message = _run(tier, OPT_DOCUMENTS[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} on {name}: {message}"


@pytest.mark.parametrize("tier", OPT_TIERS)
def test_the_optional_chained_forms_that_already_worked_still_do(tier: str):
    """NON-VACUITY: routing `?.` through the builtin table must not disturb the
    forms whose JS spelling was already right, nor the short-circuit itself."""
    status, message = _run(tier, OPT_UNCHANGED)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


#: The go tier lowers `?.` but refuses a `match` over its result, so its half of
#: the optcall proof is spelled with `??`.
GO_OPT_COALESCE = """
pub fn some_str(s: Str) -> Opt[Str] { return Some(s) }
pub fn some_int(n: Int) -> Opt[Int] { return Some(n) }
pub fn some_float(f: Float) -> Opt[Float] { return Some(f) }
pub fn show(n: Int) -> Str { return n.to_str() }
pub fn counted(s: Str) -> Str { let o = some_str(s) let v: Int = o?.length() ?? (0 - 1) return show(v) }
pub fn found(s: Str) -> Str   { let o = some_str(s) let v: Int = o?.indexOf("c") ?? (0 - 1) return show(v) }
pub fn cut(s: Str) -> Str     { let o = some_str(s) let v: Str = o?.slice(0, 2) ?? "absent" return v }
pub fn joined(s: Str) -> Str  { let o = some_str(s) let v: Str = o?.concat("Z") ?? "absent" return v }
pub fn rendered(n: Int) -> Str { let o = some_int(n) let v: Str = o?.to_str() ?? "absent" return v }
pub fn rendered_float(f: Float) -> Str { let o = some_float(f) let v: Str = o?.to_str() ?? "absent" return v }
test "a code-point length"  { assert counted("\U0001f600z") == "2" }
test "indexOf"              { assert found("abcd") == "2" }
test "slice"                { assert cut("abcd") == "ab" }
test "concat"               { assert joined("ab") == "abZ" }
test "an Int renders"       { assert rendered(0 - 42) == "-42" }
test "a Float renders canonically" { assert rendered_float(3.0) == "3" }
"""


@pytest.mark.parametrize("tier", ("py", "ts", "go"))
def test_an_optional_chained_builtin_picks_its_receiver_family(tier: str):
    """Six rows of the go builtin table dispatch on the RECEIVER's type
    (`length`, `concat`, `slice`, `indexOf` and both `to_str` rows); the
    optcall path handed them a synthetic node with no type, so every one took
    its List/Int branch and the emitted package did not build."""
    status, message = _run(tier, GO_OPT_COALESCE)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


# ------------------------------------------------------------------ static
#
# The half that runs with no node and no go toolchain: a tier whose runner
# skips is UNMEASURED, and unmeasured has never meant right.

def test_typescript_does_not_emit_a_builtin_as_a_js_method():
    emitted = _emit("typescript", """
pub fn some_str(s: Str) -> Opt[Str] { return Some(s) }
pub fn some_int(n: Int) -> Opt[Int] { return Some(n) }
pub fn parsed(s: Str) -> Opt[Opt[Int]] { return some_str(s)?.to_int() }
pub fn rendered(n: Int) -> Opt[Str] { return some_int(n)?.to_str() }
pub fn counted(s: Str) -> Opt[Int] { return some_str(s)?.length() }
""")
    for spelling in ("?.to_int(", "?.to_str(", "?.length("):
        assert spelling not in emitted, (
            f"`{spelling}` is back: JS values have no such method and the "
            "emitted module throws a TypeError at run time")
    assert "revlOptToInt(" in emitted, (
        "`to_int` is the one row whose receiver family an optcall node cannot "
        "carry; it has to split on the payload at run time")
    assert "function revlOptToInt(" in emitted and "function revlParseInt(" in emitted


def test_python_renders_an_optional_chained_to_str_through_the_payload():
    emitted = _emit("python", """
pub fn some_float(f: Float) -> Opt[Float] { return Some(f) }
pub fn rendered(f: Float) -> Opt[Str] { return some_float(f)?.to_str() }
""")
    assert "_revl_opt_to_str(" in emitted, (
        "`?.to_str()` is back on the bare `str(..)` row, which prints "
        "python's `3.0` where the canonical Float spelling is `3`")
    assert "def _revl_opt_to_str(v):" in emitted
    assert "def _revl_ftoa(x):" in emitted, "the payload split calls it"


def test_python_gates_the_bound_on_an_optional_chained_division():
    """The preamble a `?.` render pulls in has to be emitted for it. A document
    whose only bounded-Int use is through `?.` is the one that shows it."""
    for method, marker in (("div_trunc", "_REVL_I64_MIN = "),
                           ("div_floor", "_REVL_I64_MIN = "),
                           ("div_euclid", "_REVL_I64_MIN = "),
                           ("to_int32", "def _revl_i32(v):")):
        emitted = _emit("python", f"""
pub fn some_int(n: Int) -> Opt[Int] {{ return Some(n) }}
pub fn probe(n: Int) -> Opt[Int] {{ return some_int(n)?.{method}(2) }}
""" if method != "to_int32" else """
pub fn some_int(n: Int) -> Opt[Int] { return Some(n) }
pub fn probe(n: Int) -> Opt[Int32] { return some_int(n)?.to_int32() }
""")
        assert marker in emitted, (
            f"`?.{method}()` renders a helper the preamble does not define — "
            "a NameError at run time on the reference tier")


def test_go_dispatches_an_optional_chained_length_on_the_str_family():
    emitted = _emit("go", """
pub fn some_str(s: Str) -> Opt[Str] { return Some(s) }
pub fn counted(s: Str) -> Opt[Int] { return some_str(s)?.length() }
""")
    assert "revlStrLen(_x)" in emitted, (
        "the optcall receiver lost its type again: `revlListLen` over a "
        "string does not build")
    assert "revlListLen(_x)" not in emitted


@pytest.mark.parametrize("tier", ("rust", "java", "wasm"))
def test_optional_chaining_is_still_refused_by_name_where_it_is_not_lowered(tier: str):
    """NON-VACUITY, and the reason these three tiers said nothing wrong: they
    said nothing at all. Pinned so a later lowering cannot land without
    revisiting the dispatch above."""
    source = ("pub fn some_str(s: Str) -> Opt[Str] { return Some(s) }\n"
              "pub fn counted(s: Str) -> Opt[Int] { return some_str(s)?.length() }\n")
    with pytest.raises(Exception) as excinfo:
        _emit(tier, source)
    assert "optional chaining" in str(excinfo.value), excinfo.value


# ---------------------------------------------- the conversions that agreed
#
# The negative result, executed rather than asserted in prose: these are the
# edges the hunt expected to break and did not.

FLOAT_RENDERING = """
pub fn show(x: Float) -> Str { return x.to_str() }
pub fn interp(x: Float) -> Str { return `${x}` }
test "an integral Float drops its point" { assert show(3.0) == "3" }
test "the ES upper exponent threshold"   { assert show(1.0e21) == "1e+21" }
test "just below it"                     { assert show(1.0e20) == "100000000000000000000" }
test "the ES lower exponent threshold"   { assert show(1.0e-7) == "1e-7" }
test "just above it"                     { assert show(1.0e-6) == "0.000001" }
test "a computed negative zero renders unsigned" { assert show((0.0 - 1.0) * 0.0) == "0" }
test "the smallest subnormal"            { assert show(5.0e-324) == "5e-324" }
test "the largest finite"                { assert show(1.7976931348623157e308) == "1.7976931348623157e+308" }
test "not a number"                      { assert show(0.0 / 0.0) == "NaN" }
test "an infinity"                       { assert show(1.0 / 0.0) == "Infinity" }
test "shortest round trip"               { assert show(0.1 + 0.2) == "0.30000000000000004" }
test "a Float past 2^53"                 { assert show(9007199254740993.0) == "9007199254740992" }
test "to_str and interpolation agree"    { assert show(1.0e21) == interp(1.0e21) }
test "and at the small end"              { assert show(1.0e-7) == interp(1.0e-7) }
"""

INT32_OVERFLOW = """
pub fn probe(n: Int) -> Int { return n.to_int32().to_int() }
test "a narrow out of the 32-bit range must not answer a value" {
  assert probe(2147483648) == probe(2147483648)
}
"""


@pytest.mark.parametrize("tier", ("py", "ts", "go"))
def test_float_rendering_agrees_at_every_threshold(tier: str):
    """wasm is absent: it refuses a Float binding and a Float parameter by
    name, which `test_wasm_refuses_a_float_binding_by_name` pins instead."""
    status, message = _run(tier, FLOAT_RENDERING)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_a_narrow_out_of_the_int32_range_faults_everywhere(tier: str):
    """`probe(n) == probe(n)` is true of any value a tier invents, so this
    document PASSES on a tier that returns and FAILS only on one that faults —
    the assertion is therefore that it FAILS. docs/arithmetic.md: `.to_int32()`
    re-imposes the 32-bit bound at run time and traps out of range."""
    status, message = _run(tier, INT32_OVERFLOW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier} answered a VALUE for `2147483648.to_int32()` ({status}). The "
        "narrow is checked on every tier; docs/arithmetic.md.")
