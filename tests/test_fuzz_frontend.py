"""Regressions from the frontend fuzzer (tools/fuzz_frontend.py), plus a smoke
test that the harness itself still runs.

Every source string in this file is a MINIMAL reproducer a fuzz campaign
produced and a delta-debugger shrank: none of them is longer than a line, and
each one is here because it once did something the frontend must not do. A
fuzz finding at 400 bytes is a curiosity; the same finding at ten characters is
a test someone can read, which is the whole reason the tool shrinks before it
reports.

The three faults these pin, in the order they get worse:

  a REFUSAL is correct. `RevlError` is the language saying no.
  a CRASH is a bug. An `IndexError` out of the parser is an unhandled fault in
      a library, and `revl.gate` is a library with a security contract.
  a WRONG-BUT-QUIET answer is the worst. A negative list index reads the LAST
      element on python and panics on rust, go and java — so the tier every
      oracle test runs on is the one tier that cannot see it.

The `tkc` guard below is all three at once, which is why it is the example the
tool's docstring uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import fuzz_frontend as F  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.lexer import lex as reference_lex  # noqa: E402


# --------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def cache():
    return F._StageCache()


@pytest.fixture(scope="module")
def selfhost_lex(cache):
    """The self-hosted lexer, python-emitted, with every subscript hardened."""
    return cache.selfhost("lexer.rvl")["lex_src"]


@pytest.fixture(scope="module")
def gate(cache):
    """`admit_src` from selfhost/lower.rvl — what `crates/revl-gate` embeds."""
    return cache.selfhost("lower.rvl")["admit_src"]


# ------------------------------------------- the reference frontend never crashes

# Each of these once raised something that was NOT a RevlError out of
# `compile_source`, or is the neighbouring shape of one that did. `test"e"{`
# is the original: a `test` body reached `_reject_lifecycle_stmt_here`, which
# took a `self.toks[self.pos + 1]` lookahead before proving a next token
# existed, and the input had simply run out.
CRASHERS = [
    'test"e"{',
    'test "x" {',
    'lifecycle',
    'lifecycle test "x" {',
    'fault test',
    'prop test',
    'test "x" { load',
    'test "x" { abort',
    'test "x" { assert',
]


@pytest.mark.parametrize("src", CRASHERS)
def test_reference_refuses_rather_than_crashes(src):
    """A refusal is a fine answer here; a traceback is not."""
    try:
        compile_source(src, "fuzz.rvl")
    except RevlError:
        pass


@pytest.mark.parametrize("src", [
    "fn e(t:e)->I{return match t{e(v)=>v}}fn k(o:e)->n{return Some()}",
    "fn k() -> Opt[Int] { return Some() }",
    "fn k() -> Result[Int, Str] { return Ok() }",
    "fn k() -> Result[Int, Str] { return Err() }",
    "type T = A(Int) | B\nfn k() -> T { return A() }",
])
def test_nullary_case_calls_do_not_crash_the_checker(src):
    """`Some()` read `arg_types[0]` on an empty list and threw a bare
    `IndexError` out of the CHECKER — the second reference crash the campaign
    turned up, and the first outside the parser.

    Zero arguments at a payload-carrying case is ACCEPTED on purpose here
    (tests/test_selfhost_checker.py pins `Circle()` alongside the widening and
    unknown-argument cases), so this asserts only the property the fuzzer
    tests: a refusal is fine, an inferred type is fine, an `IndexError` is
    not."""
    try:
        compile_source(src, "fuzz.rvl")
    except RevlError:
        pass


def test_the_builtin_cases_still_infer_from_their_argument():
    """The guard must not have flattened the inference it protects."""
    compile_source("fn k() -> Opt[Int] { return Some(1) }", "fuzz.rvl")
    compile_source("type T = A(Int) | B\nfn k() -> T { return A(1) }", "fuzz.rvl")
    with pytest.raises(RevlError):
        compile_source('fn k() -> Opt[Int] { return Some("s") }', "fuzz.rvl")


def test_peek_ahead_clamps_to_eof():
    """The helper is total: past the end it answers with the eof token, which
    is what "there is nothing after this" means to every caller."""
    import revl.parser as P

    parser = P.Parser("fn", "fuzz.rvl")
    assert parser.peek_ahead(99).kind == "eof"
    assert parser.peek_ahead(0) is parser.peek()


# ------------------------------- extreme and malformed input still diagnoses
#
# Four inputs (issues #310, #311, #312, #315) that each produced a raw python
# exception, or a silently broken program, where a diagnostic was the whole
# contract. They are here rather than in the campaign because none of them is
# reachable by mutation in any useful time — a 4400-digit literal and a
# 5000-deep parenthesis nest are not what a shrinker converges on — and each
# one is a property the frontend now promises out loud.

NESTED = [
    # (name, source) — every one of these once ended in `RecursionError`
    ("parens", "fn f() -> Int { return " + "(" * 5000 + "1" + ")" * 5000 + " }"),
    ("calls", "fn g(x: Int) -> Int { return x }\nfn f() -> Int { return "
              + "g(" * 500 + "1" + ")" * 500 + " }"),
    ("lists", "fn f() -> Int { return " + "[" * 500 + "]" * 500 + " }"),
    ("records", "fn f() -> Int { return " + "{a:" * 500 + "1" + "}" * 500 + " }"),
    ("templates", 'fn f() -> Str { return ' + "`${" * 500 + '"x"' + "}`" * 500 + " }"),
    ("blocks", "fn f() -> Int { " + "if (true) { " * 5000 + "return 1"
               + " }" * 5000 + "\nreturn 0 }"),
    ("types", "fn f(x: " + "Opt[" * 20000 + "Int" + "]" * 20000 + ") -> Int { return 1 }"),
]


@pytest.mark.parametrize("name,src", NESTED, ids=[n for n, _ in NESTED])
def test_deep_nesting_is_a_diagnostic_not_a_recursion_error(name, src):
    """Issue #310. The parser is recursive descent and its precedence ladder
    cost ~26 python frames per nested `(`, so the ceiling sat at 38 nested
    parentheses — reached as a traceback. Recursion depth is the COMPILER's
    bound, not the language's, so the refusal has to say so."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "fuzz.rvl")
    assert "nest" in str(excinfo.value)


@pytest.mark.parametrize("depth", [38, 40, 190])
def test_ordinary_generated_nesting_still_compiles(depth):
    """The bound must sit above what generated code writes: 38 parens and a
    40-deep call chain are the two shapes the issue measured."""
    compile_source("fn f() -> Int { return " + "(" * depth + "1" + ")" * depth + " }",
                   "fuzz.rvl")
    compile_source("fn g(x: Int) -> Int { return x }\nfn f() -> Int { return "
                   + "g(" * depth + "1" + ")" * depth + " }", "fuzz.rvl")


def test_the_nesting_bound_is_stated_in_the_refusal():
    """A limit nobody can read is a crash with better manners."""
    import revl.parser as P

    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f() -> Int { return " + "(" * 5000 + "1" + ")" * 5000 + " }",
                       "fuzz.rvl")
    assert str(P.NESTING_LIMIT) in str(excinfo.value)


def test_gate_admit_answers_a_recursion_bomb():
    """`gate.admit` is a security surface with a `Verdict` contract: it
    ANSWERS. It caught `RevlError` only, so this input walked out of it as a
    `RecursionError` — fail-closed, but not through the documented boundary."""
    from revl import gate

    verdict = gate.admit("fn f() -> Int { return " + "(" * 5000 + "1" + ")" * 5000 + " }")
    assert verdict.admitted is False


def test_gate_admit_refuses_rather_than_faults_on_any_of_these():
    """The whole set, through the gate, as verdicts."""
    from revl import gate

    for src in (
        "fn f() -> Int { return " + "9" * 4400 + " }",          # issue #311
        "fn f() -> Int { return 0x" + "f" * 5000 + " }",        # issue #311
        'fn f() -> Str { return "' + LONE_SURROGATE + '" }',    # issue #311
        "fn f() -> Float { return 1e999 }",                     # issue #312
    ):
        assert gate.admit(src).admitted is False


@pytest.mark.parametrize("digits", [4400, 101, 25])
def test_an_oversized_int_literal_is_a_diagnostic(digits):
    """Issue #311. `int(num)` ran before any range check, so past CPython's
    4300-digit int-str limit the LEXER raised `ValueError` — and the checker's
    own message could not be built either, because rendering the value is the
    same conversion."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f() -> Int { return " + "9" * digits + " }", "fuzz.rvl")
    assert "outside the 64-bit range" in str(excinfo.value)


def test_an_oversized_hex_literal_is_a_diagnostic():
    """`0x` + 5000 hex digits converts fine (the limit is base-10 only) and
    then died stringifying the value INTO the refusal."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f() -> Int { return 0x" + "f" * 5000 + " }", "fuzz.rvl")
    assert "outside the 64-bit range" in str(excinfo.value)


def test_int_literals_at_the_edge_are_untouched():
    """The bound is tested, not approximated."""
    compile_source("fn f() -> Int { return 9223372036854775807 }", "fuzz.rvl")
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f() -> Int { return 9223372036854775808 }", "fuzz.rvl")
    assert "`9223372036854775808`" in str(excinfo.value)


# A lone high surrogate. Unreachable from a UTF-8 file and trivially reachable
# from every JSON-carried source path: the LSP's `didOpen`, the MCP verbs and
# `gate.admit(str)`.
LONE_SURROGATE = "\ud800"


@pytest.mark.parametrize("src", [
    'fn f() -> Str { return "' + LONE_SURROGATE + '" }',
    "fn f() -> Str { return '" + LONE_SURROGATE + "' }",
    'fn f() -> Str { return `' + LONE_SURROGATE + '` }',
    'fn f() -> Str { return """' + LONE_SURROGATE + '""" }',
    "fn f" + LONE_SURROGATE + "() -> Int { return 1 }",
    "// " + LONE_SURROGATE + "\nfn f() -> Int { return 1 }",
])
def test_a_lone_surrogate_is_a_diagnostic(src):
    """Issue #311. `lower._str_literal_value` raised a bare `ValueError` — the
    invariant it documents is real, but the refusal belongs in the lexer, where
    it is a diagnostic with a line."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "fuzz.rvl")
    assert "surrogate" in str(excinfo.value)


def test_astral_characters_still_lex():
    """The guard is for UNPAIRED surrogates; a real astral scalar is ordinary
    text and must be untouched."""
    compile_source('fn f() -> Str { return "\U0001F600" }', "fuzz.rvl")


@pytest.mark.parametrize("src", [
    "fn f() -> Float { return 1e999 }",
    "fn f() -> Float { return -1e999 }",
    "fn f() -> Float { return 1.5e400 }",
    "fn f() -> Float { let x = 1e999\n  return x }",
    "fn f() -> Float { return " + "9" * 4400 + ".5 }",
])
def test_a_non_finite_float_literal_is_a_diagnostic(src):
    """Issue #312, the worst of the four: this one did not crash, it COMPILED.
    `1e999` folded to IEEE infinity and every emitter printed the host repr as
    a bare word — py `inf`, rust `inff64`, java `infd`, go `float64(inf)`, all
    unbound names — while TypeScript rendered `Infinity` and passed. A program
    that does not run, produced quietly, is worse than a traceback."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "fuzz.rvl")
    assert "Float literal" in str(excinfo.value)


def test_finite_float_literals_at_the_edge_still_compile():
    compile_source("fn f() -> Float { return 1e308 }", "fuzz.rvl")
    compile_source("fn f() -> Float { return 1e-999 }", "fuzz.rvl")  # underflows to 0.0


def test_no_backend_can_render_a_non_finite_float():
    """The frontend refuses the literal, so this is the emitters' own belt: an
    IR handed straight to a backend must not make it print an unbound name."""
    import importlib.util

    node = {"kind": "lit", "value": float("inf")}
    for backend in ("python", "typescript", "rust", "java", "go", "wasm"):
        path = ROOT / "backends" / backend / "emit.py"
        spec = importlib.util.spec_from_file_location(f"_finite_{backend}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with pytest.raises(module.EmitError):
            module._finite_float(node["value"])
        assert module._finite_float(1.5) == 1.5


@pytest.mark.parametrize("src", [
    "\ufefffn f() -> Int { return 1 }",
    "\ufeff// a comment\nfn f() -> Int { return 1 }",
])
def test_a_leading_byte_order_mark_is_consumed(src):
    """Issue #315. A UTF-8 BOM is part of the file's ENCODING — Notepad and
    PowerShell write one — and `encoding="utf-8"` hands it through as a first
    character no grammar mentions. It was refused as `unexpected character
    '\ufeff'`, which names an invisible."""
    compile_source(src, "fuzz.rvl")


def test_a_byte_order_mark_inside_the_file_is_named():
    """Still refused, but by a name a reader can search for."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f() -> Int { return \ufeff1 }", "fuzz.rvl")
    assert "byte-order mark" in str(excinfo.value)


# ------------------------------------------------ the self-host never faults

def test_gate_survives_a_backward_token_scan(gate):
    """`fn t])->t[` drove a scan in selfhost/lower.rvl past the START of the
    token stream. `tkc` guarded the upper bound only, so `i < ts.length()` was
    true for i = -1 and the read went through: the LAST token on python, an
    `index out of bounds: len is 11 but the index is 18446744073709551615` on
    rust, a panic on go, an exception on java. The gate must answer, and the
    answer must not depend on which tier is running it."""
    assert gate("fn t])->t[") != ""  # a refusal, not a fault


@pytest.mark.parametrize("src", [
    "fn t])->t[",
    "fn f(])->t[",
    "]",
    "})",
    "fn]",
    "component]{",
])
def test_gate_never_indexes_out_of_range(gate, src):
    """The hardened build raises `IndexFault` where python would silently wrap.
    Nothing the gate does on any of these may reach that."""
    gate(src)  # must not raise


def test_gate_refuses_an_out_of_range_int_literal(gate):
    """`9223372036854775808` made the self-hosted lexer's `radix_value` fold
    past the 64-bit edge. revl's Int TRAPS there on every tier — it does not
    wrap, whatever the comment on that function used to claim — so the LEXER
    faulted on nineteen digits, and a gate that raises where it should refuse
    cannot be called from a `catch`-less consumer."""
    assert gate("9223372036854775808") != ""
    assert gate("fn f() -> Int { return 9223372036854775808 }") != ""
    assert gate("fn f() -> Int { return 0xFFFFFFFFFFFFFFFFFF }") != ""


def test_int_literals_at_the_edge_still_lex(selfhost_lex):
    """The bound is tested, not approximated: the largest i64 still folds."""
    assert [(t["kind"], t["text"]) for t in selfhost_lex("9223372036854775807")][0] \
        == ("int", "9223372036854775807")
    assert [(t["kind"], t["text"]) for t in selfhost_lex("0x7FFFFFFFFFFFFFFF")][0] \
        == ("int", "9223372036854775807")
    assert [t["kind"] for t in selfhost_lex("9223372036854775808")][0] == "error"


# --------------------------------------------- the self-host lexer agrees

# The double-quote path used to run through a second, escape-BLIND scanner
# (`scan_string`) written before item 183 gave `"..."` its `\"` / `\\` escape
# set. `"\""` came back as the one-character string `\` plus an unterminated
# string, and `"\\"` as two backslashes where the reference reads one — a
# token-level disagreement under every consumer of that lexer, the shipped gate
# crate included, on syntax as ordinary as an escaped quote.
ESCAPES = [
    r'"\""',
    r'"\\"',
    r'"a\\b"',
    r'"a\nb"',
    r'"\"\\\""',
    r"'a\\b'",
    r"'\''",
    r'"$x"',
    r'""',
]


@pytest.mark.parametrize("src", ESCAPES)
def test_selfhost_lexer_matches_reference_on_escapes(selfhost_lex, src):
    want = [(t.kind, str(t.value)) for t in reference_lex(src, "fuzz.rvl")
            if t.kind != "eof"]
    got = [(t["kind"], t["text"]) for t in selfhost_lex(src) if t["kind"] != "eof"]
    assert got == want


# Item 380: a `${...}` in a plain string is emitted as LITERAL text, so
# `"hi ${name}"` compiled clean and produced the characters `${name}`. The
# reference refuses that; the self-hosted lexer did not, which meant the gate
# admitted exactly the silent-wrong program the rule exists to stop.
@pytest.mark.parametrize("src", ['"${}"', '"${x}"', "'${x}'", '"a ${n} b"'])
def test_selfhost_lexer_refuses_a_dollar_in_a_plain_string(selfhost_lex, src):
    with pytest.raises(RevlError):
        reference_lex(src, "fuzz.rvl")
    assert any(t["kind"] == "error" for t in selfhost_lex(src))


# The rule's carve-outs, mirrored from the reference: an INCOMPLETE `${` and a
# string carrying a backtick (template or shell source quoted as data) are both
# ordinary strings, and refusing them would be a false rejection.
@pytest.mark.parametrize("src", ['"${"', '"$x"', '"{}"', '"`hi ${name}!`"'])
def test_the_dollar_rule_does_not_over_refuse(selfhost_lex, src):
    reference_lex(src, "fuzz.rvl")  # the reference accepts it
    assert not any(t["kind"] == "error" for t in selfhost_lex(src))


def test_unterminated_string_inside_an_interpolation(selfhost_lex):
    """The reference refuses `` `${"}` `` — the `${` never closes, because the
    `}` is inside a string literal. So must the self-hosted lexer.

    The campaign found this one accepted; the `scan_quoted` unification that
    landed alongside (item 183/382) closed it, since the interpolation scanner
    now sees the string the same way the reference does. Kept as a test rather
    than deleted: it is the one case in this family that works, and the two
    below say what "this family" is."""
    with pytest.raises(RevlError):
        reference_lex('`${"}`', "fuzz.rvl")
    assert any(t["kind"] == "error" for t in selfhost_lex('`${"}`'))


# --------------------------------------------------------------- known gaps
#
# Found by the same campaign, NOT fixed here, and pinned so they are recorded
# rather than rediscovered.


def test_host_body_tracks_the_double_quote(selfhost_lex):
    """The half of the host-body scan that already agrees, so the xfail below
    is read as an INCOMPLETE tracker rather than a missing one."""
    with pytest.raises(RevlError):
        reference_lex('@py{"}', "fuzz.rvl")
    assert any(t["kind"] == "error" for t in selfhost_lex('@py{"}'))


@pytest.mark.xfail(reason="selfhost host-body scan tracks only the double quote",
                   strict=True)
@pytest.mark.parametrize("src", ["@t{`}", "@py{'}"])
def test_unterminated_string_inside_a_host_body(selfhost_lex, src):
    """The reference matches a `@backend { ... }` brace with `_match_brace`,
    which walks EVERY string form through `_Trivia` and so knows a `}` inside
    any of them is not the closing brace. The self-hosted host-body scan
    tracks the double quote alone, so a backtick template or a single-quoted
    string (item 382's spelling) left open inside the body closes it early: a
    `hostbody` token comes back where the reference refuses.

    Closing this means porting trivia-aware brace matching into
    selfhost/lexer.rvl — its own change with its own oracle run, not a rider
    on a fuzzing pass."""
    with pytest.raises(RevlError):
        reference_lex(src, "fuzz.rvl")
    assert any(t["kind"] == "error" for t in selfhost_lex(src))


@pytest.mark.xfail(reason="the two lexers disagree on what whitespace IS",
                   strict=True)
@pytest.mark.parametrize("ws", ["\x0b", "\x0c", "\x1c", "\x85"])
def test_the_two_lexers_agree_on_the_whitespace_set(selfhost_lex, ws):
    """A vertical tab between two tokens is whitespace to the reference lexer
    and a lexical error to the self-hosted one.

    Not obviously the self-host's bug — arguably the reference's. The reference
    asks python `str.isspace()`, which says yes to `\\x0b`, `\\x0c`, `\\x1c`-`\\x1f`
    and `\\x85`; the self-host asks revl's `Str.is_space`, which the language
    DEFINES as space, tab, LF, CR (src/revl/typecheck.py, docs/stdlib-2.0.md)
    precisely so that every tier answers the same. So the reference lexer is
    the one using a set the language does not define, and closing the gap is a
    decision about the grammar — is a form feed revl whitespace? — rather than
    a defect to patch. Recorded here so the answer, whichever way it goes,
    lands deliberately.
    """
    reference_lex(f"fn{ws}f", "fuzz.rvl")
    assert not any(t["kind"] == "error" for t in selfhost_lex(f"fn{ws}f"))


# ------------------------------------------------------------------- harness

def test_index_guard_catches_what_python_would_wrap():
    """The hardening the self-host stages run under. Without it a negative
    index is invisible on python, which is the tier every oracle test uses."""
    assert F._revl_idx([1, 2, 3], 1) == 2
    assert F._revl_idx({"k": 1}, "k") == 1          # a record read, not ordinal
    for bad in (-1, 3, 99):
        with pytest.raises(F.IndexFault):
            F._revl_idx([1, 2, 3], bad)


def test_shrinker_reduces_to_the_same_signature():
    """Delta debugging has to preserve the signature, or the reproducer it
    reports is a different bug from the one it found."""
    signature = F.Signature("t", "kind", "")

    def oracle(src):
        return (signature, "") if "!" in src else None

    minimal = F.shrink("a\nb\nc!d\ne\nf", oracle, signature, budget=5.0)
    assert minimal == "!"


def test_campaign_smoke_runs_every_stage(cache):
    """A short budget over all three oracles: generation, the hardened
    self-host, the shrinker and the report, end to end on a fixed seed.

    Deliberately tiny. The fuzzer's job is to find things and that takes
    minutes to hours; this only pins that the harness still runs, so a
    refactor that breaks it fails here instead of the next time someone
    reaches for it. Assert nothing about WHAT it finds: what a campaign turns
    up is the campaign's result, not a fixed expectation.
    """
    import random

    stages = [stage(cache) for stage in F.STAGES.values()]
    corpus = F.load_corpus()
    assert len(corpus) > 100, "the .rvl corpus should be the whole tree"

    import time
    campaign = F.Campaign(stages, corpus)
    campaign.run(random.Random(1234), time.monotonic() + 20.0, 150, quiet=True)
    assert campaign.iterations > 0
