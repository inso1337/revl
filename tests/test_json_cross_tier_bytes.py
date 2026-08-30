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

GO CAVEAT (a SEPARATE, deeper defect, not fixable in json.rvl's @go body —
see the module header and the report for item 385): a revl RECORD lowers to a
Go struct whose fields are UNEXPORTED (lowercase) with no `json:` tags, so
`encoding/json` cannot see them and `json.Marshal` drops every field — a record
stringifies to `{}` on go regardless of separators. go is therefore proven here
only on the shapes it renders correctly (scalars, strings, lists); the record
`{}` behavior is pinned by `test_go_record_is_the_known_separate_defect` so a
future go-emitter fix trips this file loudly.
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

#: names go renders byte-identically; the objects are excluded by the struct
#: caveat documented in the module header (records marshal to `{}` on go)
GO_UNSUPPORTED = {"record", "nested_object"}
GO_CORPUS = [c for c in CORPUS if c[0] not in GO_UNSUPPORTED]


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
    """go joins the byte-equality proof for the shapes it renders correctly —
    scalars, strings (escapes/unicode/`<>&`), and arrays — so the three-tier
    agreement is real, not a py/ts pair. Objects are covered by the caveat test
    below."""
    status, message = _run("go", decl, expected)
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go drifted on {name!r}: {message}"


def test_go_record_is_the_known_separate_defect():
    """CHARACTERIZATION of the go-emitter defect that json.rvl's @go body cannot
    fix (see the module header): a revl record lowers to a Go struct with
    UNEXPORTED fields and no `json:` tags, so `json.Marshal` drops them and the
    record stringifies to `{}`. This is pinned so a future go-emitter fix (export
    fields + tags, remap field access) trips here and gets folded into the
    cross-tier corpus above."""
    status, message = _run("go", 'type Rec = { name: Str, n: Int }\n'
                                 'fn v() -> Rec { return { name: "x", n: 1 } }',
                           r'{}')
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", (
        "go no longer stringifies a record to '{}' — the struct-field defect "
        f"may be fixed; move records into the cross-tier corpus. ({message})")
