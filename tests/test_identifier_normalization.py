"""Identifier normalization: a name distinct to the checker must be distinct
on EVERY tier.

The hole. The lexer classified identifiers with `str.isalpha()`/`str.isalnum()`,
which are the full Unicode classes, with no normalization and no ASCII
restriction. CPython NFKC-normalizes identifiers at parse time (PEP 3131), so
names the revl checker sees as two became ONE Python function:

    extern emission fn send(data: Str) -> Int = @py { ... }
    pub fn ｓend(data: Str) -> Int { ... }        // U+FF53 fullwidth s

compiled ADMITTED (`revl audit` reported `boundary: none`), and the emitted
module defined `def ｓend` and `def send` — the second binding overwriting the
first, so a PLAIN `fn` captured the emission extern's binding and a provision
declared `fn calc` on a plain service reached the irreversible host call. The
ASCII spelling of the same program is correctly refused (G4), which is the
tell: the homoglyph is exactly what splits the frontend's view (two names) from
the backend's (one name).

Same root cause, two more shapes: the ligature `ﬁlter` (U+FB01) merges with
`filter`, and a superscript identifier CONTINUATION (`x²`, admitted because
`isalnum()` accepts category-No digits) normalizes to `x2`.

The tiers had also already diverged on ADMISSION: the typescript, rust and java
emitters match ASCII-only identifier regexes and hard-error on `ﬁlter`, while
python silently merged it. That divergence is itself the bug, so the refusal
belongs in the FRONTEND.

The fix restricts identifiers to ASCII in the lexer. Every ASCII identifier is
an NFKC fixed point (so normalization is the identity on the admitted set, and
uniqueness on raw names IS uniqueness on normalized names), and
`[A-Za-z_][A-Za-z0-9_]*` is a subset of what every emitter accepts.

Also covered here (same file, same audit): the item-380 `f"..."` guard never
grew to cover `f'...'` after item 382 made `'...'` an equal `Str` spelling, so
`return f'hi {name}'` still parsed as `return f` plus a dead string statement.
"""

import importlib.util
import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.lexer import lex  # noqa: E402


def _err(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    return str(excinfo.value)


def _emit(backend: str, source: str) -> str:
    ir = compile_source(source, "t.rvl")
    sys.path.insert(0, str(ROOT / "backends" / backend))
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_identnorm_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.emit(ir))
    finally:
        sys.path.remove(str(ROOT / "backends" / backend))


# --------------------------------------------------------------------------
# 1. the executed exploit: a fullwidth `ｓ` captures an emission extern
# --------------------------------------------------------------------------

_CAPTURE = (
    'extern emission fn send(data: Str) -> Int = @py {{ print("HOST EMISSION: " + data); return 1 }}\n'
    'pub fn {name}(data: Str) -> Int {{ var acc = 0  acc += data.length()  return acc }}\n'
    'service Pure {{ fn calc(x: Str) -> Int }}\n'
    'component P provides p: Pure {{ provide p {{ fn calc(x) = {name}(x) }} }}\n'
)


def test_fullwidth_homoglyph_cannot_capture_an_emission_extern():
    # THE exploit. Before the fix this compiled ADMITTED and the emitted python
    # bound one `send`, so the plain `fn` shadowed the emission extern and the
    # provision's plain `calc` reached the host print.
    err = _err(_CAPTURE.format(name="ｓend"))
    assert "non-ASCII character in identifier" in err
    assert "U+FF53" in err
    # the diagnostic names the collision, not just the character
    assert "`send`" in err and "NFKC" in err


def test_ascii_control_still_gets_its_original_g4_diagnostic():
    # The control that proves the homoglyph was doing the work: with an ASCII
    # `s` the SAME program is refused by the emission checker, and that
    # diagnostic must be untouched by this change.
    err = _err(_CAPTURE.format(name="send"))
    assert "`Pure.calc` is declared plain, but this implementation reaches `send()`" in err
    assert "(G4)" in err


def test_fullwidth_homoglyph_is_refused_even_with_no_collision_partner():
    # The refusal is a lexer rule, not a duplicate-name check: it fires with no
    # `send` anywhere in the program, so there is no window in which the
    # normalized name is admitted first and collided with later.
    err = _err("pub fn ｓend(data: Str) -> Int { return 1 }\n")
    assert "non-ASCII character in identifier" in err


# --------------------------------------------------------------------------
# 2. the ligature merge: `ﬁlter` and `filter`
# --------------------------------------------------------------------------

_LIGATURE = (
    "pub fn filter(x: Int) -> Int { return x * 1000 }\n"
    "pub fn ﬁlter(x: Int) -> Int { return x }\n"
    "pub fn go(x: Int) -> Int { return ﬁlter(x) }\n"
)


def test_ligature_identifier_merge_is_refused():
    # Before: ADMITTED; the emitted python defined `def filter` then `def ﬁlter`
    # (one NFKC name), so `go(1)` evaluated to 1000 where revl semantics say 1.
    err = _err(_LIGATURE)
    assert "non-ASCII character in identifier" in err
    assert "U+FB01" in err
    assert "`filter`" in err


def test_ligature_was_a_per_tier_admission_divergence():
    # The typescript emitter always rejected this name while python merged it.
    # Now neither tier ever sees it, because the frontend refuses first: the
    # SAME error comes out no matter which backend the caller asked for.
    for backend in ("python", "typescript", "rust", "java", "go", "wasm"):
        with pytest.raises(RevlError, match="non-ASCII character in identifier"):
            _emit(backend, _LIGATURE)


# --------------------------------------------------------------------------
# 3. the continuation vector: `x²` (category No, admitted by `isalnum()`)
# --------------------------------------------------------------------------

def test_superscript_identifier_continuation_is_refused():
    # `isalnum()` accepted category-No digits in a CONTINUATION, and `x²`
    # NFKC-normalizes to `x2`.
    err = _err("pub fn f(x2: Int) -> Int { return x2 * 1000 }\n"
               "pub fn g(x²: Int) -> Int { return x² }\n")
    assert "non-ASCII character in identifier" in err
    assert "U+00B2" in err
    # reported as the whole word the author wrote, with its normalized twin
    assert "`x²`" in err and "`x2`" in err


def test_non_ascii_digit_does_not_start_a_number():
    # `_lex_number` was entered on `c.isdigit()`, the full Unicode class, while
    # `_scan_grouped_digits` only accepts ASCII. An Arabic-Indic digit now hits
    # the identifier refusal rather than the number scanner.
    with pytest.raises(RevlError, match="non-ASCII character in identifier"):
        lex("let n = ١٢", "<test>")


def test_combining_mark_after_an_ascii_run_is_refused():
    # A combining mark is identifier text too, and `e` + U+0301 NFKC-composes
    # to `é` — a third way for two source names to become one on the py tier.
    err = _err("pub fn café() -> Int { return 1 }\n")
    assert "non-ASCII character in identifier" in err


# --------------------------------------------------------------------------
# 4. the cross-tier invariant
# --------------------------------------------------------------------------

_PORTABLE = (
    "pub fn add_two(x_1: Int, y2: Int) -> Int { return x_1 + y2 }\n"
    "pub fn _helper(n: Int) -> Int { return add_two(n, 2) }\n"
)


def test_every_frontend_admitted_identifier_renders_on_every_tier():
    # The invariant the fix buys: what the frontend admits, every emitter takes.
    # Previously the frontend admitted names the rust/java/typescript emitters
    # rejected outright, so admission depended on which backend you asked for.
    for backend in ("python", "typescript", "rust", "java", "go", "wasm"):
        out = _emit(backend, _PORTABLE)
        assert "add_two" in out


def test_admitted_identifiers_are_nfkc_fixed_points():
    # The property that makes "distinct to the checker" imply "distinct on the
    # py tier": normalization is the IDENTITY on every admitted identifier, so
    # the frontend's duplicate check is already the normalized-name check.
    source = (
        "pub fn add_two(x_1: Int, y2: Int) -> Int { return x_1 + y2 }\n"
        "pub fn _helper(n: Int) -> Int { return add_two(n, 2) }\n"
        'pub fn note() -> Str { return "café ｓend ﬁlter x²" }\n'
    )
    names = [t.value for t in lex(source, "<test>") if t.kind in ("ident", "kw")]
    assert names, "expected identifiers"
    for name in names:
        assert unicodedata.normalize("NFKC", name) == name
        assert name.isascii()


def test_frontend_refuses_before_any_backend_is_chosen():
    # The refusal is not per-emitter: `compile_source` alone rejects it, so the
    # IR a backend receives can never contain a non-normalization-stable name.
    with pytest.raises(RevlError, match="non-ASCII character in identifier"):
        compile_source("pub fn ｓend() -> Int { return 1 }\n", "t.rvl")


# --------------------------------------------------------------------------
# 5. false positives: ordinary code must still lex and compile
# --------------------------------------------------------------------------

def test_ordinary_ascii_identifiers_still_lex():
    assert [t.kind for t in lex("a_b1 _c D2e", "<test>")] == [
        "ident", "ident", "ident", "eof",
    ]


def test_underscores_and_digits_in_continuations_still_compile():
    ir = compile_source(_PORTABLE, "t.rvl")
    names = {f["name"] for f in ir.get("functions", [])}
    assert {"add_two", "_helper"} <= names


def test_numbers_are_unchanged():
    toks = [(t.kind, t.value) for t in lex("1 2.5 0xFF 1_000 1e3 0b1010", "<test>")]
    assert toks == [
        ("int", 1), ("float", 2.5), ("int", 255), ("int", 1000),
        ("float", 1000.0), ("int", 10), ("eof", None),
    ]


def test_non_ascii_text_in_strings_comments_and_host_bodies_still_works():
    # Only NAMES are constrained. Data stays full Unicode.
    source = (
        "// café 日本語 ｓend\n"
        'extern pure fn label() -> Str = @py { return "café 日本語" }\n'
        'pub fn greet() -> Str { return "こんにちは ﬁ" }\n'
    )
    ir = compile_source(source, "t.rvl")
    assert ir is not None
    out = _emit("python", source)
    assert "こんにちは" in out


def test_host_block_backend_name_still_lexes():
    toks = lex('extern fn f() -> Int = @py { return 1 }\n', "<test>")
    assert any(t.kind == "hostbody" and t.value[0] == "py" for t in toks)


# --------------------------------------------------------------------------
# 6. F6: the item-380 f-string guard never grew to cover item 382's `'...'`
# --------------------------------------------------------------------------

_FSTRING = (
    "pub fn greet(name: Str) -> Str {{\n"
    "  let f = \"prefix\"\n"
    "  return f{q}hi {{name}}{q}\n"
    "}}\n"
)


def test_single_quoted_f_string_prefix_is_refused():
    # Before: ADMITTED, and the emitted python was `return f` followed by a dead
    # `'hi {name}'` statement — the unrelated binding `f`, which is exactly the
    # class item 380 closed for the double-quoted spelling.
    err = _err(_FSTRING.format(q="'"))
    assert "`f'...'` is not a revl string" in err
    assert "backtick template" in err


def test_double_quoted_f_string_prefix_diagnostic_is_unchanged():
    err = _err(_FSTRING.format(q='"'))
    assert '`f"..."` is not a revl string' in err


def test_capital_f_prefix_is_refused_for_both_quotes():
    for q in ("'", '"'):
        err = _err(_FSTRING.format(q=q).replace("return f", "return F"))
        assert f"`F{q}...{q}` is not a revl string" in err


def test_an_f_identifier_not_glued_to_a_quote_still_works():
    # The guard keys on the quote being GLUED to the `f`; a real `f` binding
    # used normally must keep compiling.
    ir = compile_source(
        "pub fn greet() -> Str {\n"
        "  let f = 'prefix'\n"
        "  return f\n"
        "}\n", "t.rvl")
    assert ir is not None


def test_dollar_interpolation_guard_now_covers_the_single_quoted_spelling():
    # Item 382 made `'...'` an equal `Str` spelling, but the item-380 guard was
    # gated on `quote == '"'`, so `'hi ${name}'` silently emitted literal text.
    err = _err("pub fn greet(name: Str) -> Str { return 'hi ${name}' }\n")
    assert "does not interpolate" in err


def test_triple_quoted_verbatim_strings_still_carry_template_source():
    # Deliberately NOT extended: the triple-quoted form is the verbatim
    # spelling used to quote template/shell source as DATA.
    ir = compile_source(
        'pub fn tmpl() -> Str { return """\nhi ${name}\n""" }\n', "t.rvl")
    assert ir is not None
