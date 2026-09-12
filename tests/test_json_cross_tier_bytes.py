"""`json_stringify` is byte-identical across tiers — the CROSS-TIER proof (item 385).

`json_stringify` is a `pure` stdlib function, so revl's cross-tier determinism
guarantee (docs/syntax-2.0.md §3.4: "no backend can diverge") requires it to
return the SAME BYTES on every tier for the same value. It did not: the py tier
emitted `{"k": "v", "n": 1}` (Python's default `json.dumps` inserts `", "`/
`": "`) while ts emitted the compact `{"k":"v","n":1}`. Each tier was
self-consistent, so BOTH tiers' own tests passed — the drift only bit
CROSS-TIER: hashing a record, byte-comparing a ledger, a cassette asserting an
identical transcript, a signature over emitted JSON (the harness does all four).

This is the conformance test the roadmap asked for: NOT per-tier
self-consistency (each tier agreeing with itself), but the same corpus
stringified on py AND ts AND go producing the ONE canonical byte string,
asserted against a single shared expected literal. If a tier drifts by one
byte, its probe fails here.

THE CANONICAL FORM (now stated in stdlib/json.rvl's header and
docs/stdlib-json.md):
  * compact — no space after `:` or `,`  (ts `JSON.stringify` default; go;
    py `separators=(",", ":")`)
  * non-ASCII stays raw UTF-8 (py `ensure_ascii=False`; ts; go)
  * `<`, `>`, `&` stay raw (go needs `SetEscapeHTML(false)`; py/ts already raw)
  * key order = record declaration / insertion order (py dict, ts object)
  * booleans/null spelled `true`/`false`/`null`; ints exact (i64, incl. > 2^53)

GO RECORDS (item 390 — the go-emitter fix that folded records into this proof):
a revl RECORD used to lower to a Go struct whose fields were UNEXPORTED with no
`json:` tags, so `encoding/json` dropped every field and a record stringified to
`{}` on go regardless of separators. The go emitter now emits record struct
fields EXPORTED (UpperCamel) with a `json:"<revl-name>"` tag preserving the
source field name, and remaps every field read/write/construction to the
exported identifier — so a record stringifies to the SAME canonical bytes as
py/ts. Records are therefore part of the three-tier corpus below (GO_CORPUS is
now the full CORPUS), and the former pin
(`test_go_record_is_the_known_separate_defect`) has flipped to a positive
(`test_go_record_now_stringifies_canonically`).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

#: the committed module, supplied in-memory so the probe's `use` resolves to it
STDLIB = (ROOT / "stdlib" / "json.rvl").read_text(encoding="utf-8")

# Each corpus entry: (name, decl, expected). `decl` declares any type plus a
# `v()` producing the value; `expected` is the ONE canonical byte string every
# tier must return, written as a revl string literal (so `\"`/`\\` are revl
# escapes). A typed `v()` lets the go tier infer a struct rather than refuse.
CORPUS = [
    # --- objects (the verified bug: py had spaces, ts/go compact) ---
    ("record",
     'type Rec = { name: Str, n: Int }\nfn v() -> Rec { return { name: "x", n: 1 } }',
     r'{"name":"x","n":1}'),
    ("nested_object",
     'type Inner = { a: Int }\ntype Outer = { inner: Inner, tags: List[Str] }\n'
     'fn v() -> Outer { return { inner: { a: 2 }, tags: ["p", "q"] } }',
     r'{"inner":{"a":2},"tags":["p","q"]}'),
    # --- arrays ---
    ("list_int", 'fn v() -> List[Int] { return [1, 2, 3] }', r'[1,2,3]'),
    ("list_str", 'fn v() -> List[Str] { return ["a", "b"] }', r'["a","b"]'),
    ("list_nested", 'fn v() -> List[List[Str]] { return [["x", "y"], ["z"]] }',
     r'[["x","y"],["z"]]'),
    ("empty_list", 'fn v() -> List[Int] { return [] }', r'[]'),
    # --- strings ---
    ("string_plain", 'fn v() -> Str { return "hello" }', r'"hello"'),
    # a " b \ c \n(literal backslash-n) d \t(literal backslash-t) e -> JSON
    # escapes the quote and every backslash; identical on all three tiers.
    # `expected` is the RAW canonical byte string (quoted, with the JSON `\"`
    # and `\\` escapes as literal characters); _run re-escapes it for the revl
    # assert literal.
    ("string_escapes", r'fn v() -> Str { return "a\"b\\c\nd\te" }',
     r'"a\"b\\c\\nd\\te"'),
    ("string_unicode", 'fn v() -> Str { return "café ☕ 日本" }',
     r'"café ☕ 日本"'),
    # `<` `>` `&`: go's default json.Marshal HTML-escapes these; the @go body
    # sets SetEscapeHTML(false) so they stay raw, matching py/ts
    ("string_html_chars", 'fn v() -> Str { return "a<b>c&d" }', r'"a<b>c&d"'),
    # --- scalars ---
    ("int", 'fn v() -> Int { return 42 }', r'42'),
    ("int_negative", 'fn v() -> Int { return -7 }', r'-7'),
    # past 2^53: py int / ts bigint / go int64 all keep exact i64 precision
    ("int_big", 'fn v() -> Int { return 9007199254740993 }', r'9007199254740993'),
    ("float", 'fn v() -> Float { return 1.5 }', r'1.5'),
    ("float_negative", 'fn v() -> Float { return -0.25 }', r'-0.25'),
    ("bool_true", 'fn v() -> Bool { return true }', r'true'),
    ("bool_false", 'fn v() -> Bool { return false }', r'false'),
    # JSON null via a genuine parsed null value (revl's Opt None is a distinct
    # ADT with a tier-specific representation, not a JSON null)
    ("null", 'fn v() -> Any { return json_parse("null") }', r'null'),
]

#: go now renders every shape byte-identically, records included (item 390:
#: exported json-tagged struct fields). GO_CORPUS is the full corpus.
GO_CORPUS = list(CORPUS)


def _run(tier: str, decl: str, expected: str) -> tuple[str, str]:
    """Compile a probe that asserts `json_stringify(v()) == expected` and run it
    on *tier*. A pass means that tier emitted the canonical bytes exactly."""
    # `expected` is the RAW canonical byte string; escape `\` then `"` so it
    # embeds as a well-formed revl double-quoted literal.
    literal = expected.replace("\\", "\\\\").replace('"', '\\"')
    src = (
        'use "stdlib/json.rvl" { json_stringify, json_parse }\n'
        f"{decl}\n"
        "pub fn out() -> Str { return json_stringify(v()) }\n"
        f'test "canonical" {{ assert out() == "{literal}" }}\n'
    )
    ir = compile_source(src, "json_bytes_probe.rvl",
                        modules={"stdlib/json.rvl": STDLIB})
    return RUNNERS[tier](ir)


@pytest.mark.parametrize("name,decl,expected", CORPUS,
                         ids=[c[0] for c in CORPUS])
@pytest.mark.parametrize("tier", ["py", "ts"])
def test_py_and_ts_emit_the_canonical_bytes(tier, name, decl, expected):
    """py and ts — the two primary tiers, and the two the verified bug split —
    both emit the ONE canonical byte string for every value shape, records
    included (the exact case that used to differ by the `", "`/`": "` spaces)."""
    status, message = _run(tier, decl, expected)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} drifted on {name!r}: {message}"


@pytest.mark.parametrize("name,decl,expected", GO_CORPUS,
                         ids=[c[0] for c in GO_CORPUS])
def test_go_emits_the_canonical_bytes_on_supported_shapes(name, decl, expected):
    """go joins the byte-equality proof for EVERY shape — scalars, strings
    (escapes/unicode/`<>&`), arrays, AND records/nested objects (item 390) — so
    the three-tier agreement is real and complete, not a py/ts pair."""
    status, message = _run("go", decl, expected)
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go drifted on {name!r}: {message}"


def test_go_record_now_stringifies_canonically():
    """Item 390: after the go-emitter fix (exported struct fields + json tags),
    a record stringifies to the canonical bytes on go, byte-identical to py/ts —
    NOT `{}`. Fails until backends/go/emit.py exports record fields."""
    status, message = _run("go", 'type Rec = { name: Str, n: Int }\n'
                                 'fn v() -> Rec { return { name: "x", n: 1 } }',
                           r'{"name":"x","n":1}')
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go record did not stringify canonically: {message}"


def test_go_record_round_trips_after_construct():
    """The go-emitter fix (item 390) exports struct fields AND remaps every field
    read/write to the exported identifier, so a record still round-trips: a field
    read after construction returns the value it was built with. (Guards against
    a half-fix that exported the declaration but left an access site spelling the
    old unexported name — which would fail to compile.)"""
    status, message = _run(
        "go",
        'type Rec = { name: Str, n: Int }\n'
        'fn v() -> Str { let r = { name: "x", n: 1 }\n return r.name }',
        r'"x"')
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go record round-trip broke: {message}"


# ---------------------------------------------------------------------------
# A non-finite Float is REFUSED, never encoded (item 481)
# ---------------------------------------------------------------------------
#
# `json_stringify` is `pure`, so the byte-equality guarantee above is a
# requirement rather than a nicety. A non-finite `Float` is a reachable VALUE
# and not exotic input — `/` is IEEE true division on every tier, so `1.0/0.0`
# is inf and `0.0/0.0` is nan — and it used to reach FOUR different encodings:
#
#   py    `Infinity` / `NaN` — not JSON at all (json.dumps defaults to
#         allow_nan=True), which poisons a strict downstream parser
#   ts    `null` — JSON.stringify's silent substitution
#   rust  `null` — serde_json's `From<f64>` maps a non-finite f64 to Null
#   go    the EMPTY string — `Encoder.Encode`'s error was discarded, so a
#         signature over "nothing" was returned where an encoding was promised
#
# A non-finite Float has no JSON spelling (RFC 8259 section 6), and the
# frontend already refuses a non-finite Float LITERAL for exactly that reason
# (item 312). These probes pin the same refusal on the RUNTIME value, which the
# literal check structurally cannot see.
#
# The value is passed INLINE as the argument, because that is the path that
# boxes it for the `Any` parameter (`backends/rust/emit.py::_coerce_any_arg`,
# where the fix lives). A `fn v() -> Any { ... }` wrapper is deliberately NOT
# used: the rust emitter does not box a Float on the way out of an
# `Any`-returning fn at all, so such a probe fails to compile on rust for a
# FINITE float too (a pre-existing, orthogonal gap, not this finding).
NON_FINITE = [
    ("inf", "1.0/0.0"),
    ("neg_inf", "-1.0/0.0"),
    ("nan", "0.0/0.0"),
]

#: Each tier's OWN refusal text. Asserting on it is what keeps these tests
#: non-vacuous: before the fix every tier ALREADY failed a sentinel probe,
#: because it substituted a wrong string rather than refusing, so
#: `status != "pass"` alone cannot tell a refusal from the old substitution.
#: Only the py and go strings are the host stdlib's; ts and rust are this
#: repo's own text.
REFUSAL_TEXT = {
    "py": "Out of range float values are not JSON compliant",
    "ts": "a non-finite Float has no JSON representation",
    "rust": "a non-finite Float has no representation in a dynamic value",
    "go": "json_stringify: json: unsupported value",
}

ALL_TIERS = ["py", "ts", "rust", "go"]


def _run_program(tier: str, imports: str, decl: str, body: str,
                 expected: str) -> tuple[str, str]:
    """Compile a probe whose `out()` is *body* and run it on *tier*,
    asserting `out() == expected`. `imports` is the `use` name list, spelled
    per probe so no probe carries an unused import."""
    literal = expected.replace("\\", "\\\\").replace('"', '\\"')
    src = (
        f'use "stdlib/json.rvl" {{ {imports} }}\n'
        f"{decl}"
        f"pub fn out() -> Str {{ {body} }}\n"
        f'test "canonical" {{ assert out() == "{literal}" }}\n'
    )
    ir = compile_source(src, "json_probe.rvl",
                        modules={"stdlib/json.rvl": STDLIB})
    return RUNNERS[tier](ir)


def _run_inline(tier: str, arg: str, expected: str) -> tuple[str, str]:
    """Assert `json_stringify(<arg>) == expected` on *tier*."""
    return _run_program(tier, "json_stringify", "",
                        f"return json_stringify({arg})", expected)


@pytest.mark.parametrize("name,arg", NON_FINITE, ids=[c[0] for c in NON_FINITE])
@pytest.mark.parametrize("tier", ALL_TIERS)
def test_a_non_finite_float_is_refused_not_encoded(tier, name, arg, capsys):
    """Every tier refuses `json_stringify(<non-finite Float>)`, and says so.

    The sentinel expectation is deliberately unreachable: the point is that the
    tier neither returns it NOR returns the bytes it used to substitute. The
    refusal text is asserted separately, so a future change that swaps one
    wrong encoding for another still reddens here."""
    status, message = _run_inline(tier, arg, "SENTINEL")
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert status != "pass", (
        f"{tier} encoded {name!r} instead of refusing it ({message})")
    assert REFUSAL_TEXT[tier] in output, (
        f"{tier} failed on {name!r} without its refusal text — a substitution "
        f"rather than a refusal?\n--- output ---\n{output[-3000:]}")


@pytest.mark.parametrize("name,arg", NON_FINITE, ids=[c[0] for c in NON_FINITE])
@pytest.mark.parametrize("tier", ["py", "ts", "go"])
def test_a_non_finite_float_inside_a_container_is_refused(tier, name, arg,
                                                          capsys):
    """A non-finite Float nested in a list is refused too — the encoding walks
    the whole value, so a top-level-only guard would not be a fix. rust is
    excluded on purpose: it does not box a CONTAINER argument into the dynamic
    representation at all, so `json_stringify([1.5])` already fails there for a
    finite float. That is a pre-existing, orthogonal boundary documented in
    docs/stdlib-json.md, not part of this finding."""
    status, message = _run_inline(tier, f"[{arg}]", "SENTINEL")
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert status != "pass", (
        f"{tier} encoded [{name}] instead of refusing it ({message})")
    assert REFUSAL_TEXT[tier] in output, (
        f"{tier} failed on [{name!r}] without its refusal text — a "
        f"substitution rather than a refusal?\n--- output ---\n{output[-3000:]}")


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_a_finite_computed_float_still_stringifies(tier):
    """The control, and the reason the refusal is not a blanket ban: an ordinary
    COMPUTED float — the same `/` that can produce inf — still stringifies to the
    canonical bytes on every tier. On rust this exercises the checked
    `serde_json::Number::from_f64` path with a `Some`, so the new branch is
    proved to pass finite values through rather than refuse them."""
    status, message = _run_inline(tier, "1.0/4.0", "0.25")
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} refused a finite computed float: {message}"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("tier", ALL_TIERS)
def test_the_parse_side_rejects_the_non_finite_constants(tier, literal):
    """The read half of the same rule: `NaN`, `Infinity` and `-Infinity` are not
    JSON (RFC 8259 section 6) and must be an `Err` on every tier.

    py's `json.loads` was the sole tier that accepted them (its `parse_constant`
    hook defaults to passing them through), so `json_try_parse("NaN")` was `Ok`
    on py and `Err` on ts/rust/go. Asserted through the TOTAL wrapper so the
    probe needs no error handling, and so the check is the same on all four
    tiers. A tier that accepts the constant returns "ACCEPTED" and reddens."""
    status, message = _run_program(
        tier, "json_try_parse", "",
        'return match json_try_parse("%s") { Ok(_) => "ACCEPTED", '
        'Err(_) => "REFUSED" }' % literal,
        "REFUSED")
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", (
        f"{tier} accepted the non-finite JSON constant {literal!r}: {message}")
