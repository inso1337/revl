"""Lexer unit tests for v2.0 operators and keywords (syntax-2.0.md §3.2)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.lexer import lex  # noqa: E402


def _kinds(source):
    return [t.kind for t in lex(source, "<test>")]


def test_equality_and_comparison_operators():
    assert _kinds("a == b") == ["ident", "==", "ident", "eof"]
    assert _kinds("a === b") == ["ident", "===", "ident", "eof"]
    assert _kinds("a != b") == ["ident", "!=", "ident", "eof"]
    assert _kinds("a !== b") == ["ident", "!==", "ident", "eof"]
    assert _kinds("a <= b") == ["ident", "<=", "ident", "eof"]
    assert _kinds("a >= b") == ["ident", ">=", "ident", "eof"]


def test_logical_operators():
    assert _kinds("a && b || !c") == ["ident", "&&", "ident", "||", "!", "ident", "eof"]


def test_arithmetic_operators():
    assert _kinds("a + b * c / d % e - f") == [
        "ident", "+", "ident", "*", "ident", "/", "ident", "%", "ident", "-", "ident", "eof",
    ]


def test_fat_arrow_vs_thin_arrow():
    assert _kinds("x => x + 1") == ["ident", "=>", "ident", "+", "int", "eof"]
    assert _kinds("fn f() -> Int") == ["kw", "ident", "(", ")", "arrow", "ident", "eof"]


def test_optional_chaining_and_coalescing():
    assert _kinds("a?.b ?? c") == ["ident", "?.", "ident", "??", "ident", "eof"]


def test_new_keywords_are_recognized():
    for word in ("type", "use", "pub", "var", "while", "for", "of", "if", "else",
                 "match", "test", "assert", "async", "as", "fail"):
        assert lex(word, "<test>")[0].kind == "kw", word


def test_adts_and_ternary_use_single_pipe_and_question():
    assert _kinds("A | B") == ["ident", "|", "ident", "eof"]
    assert _kinds("c ? a : b") == ["ident", "?", "ident", ":", "ident", "eof"]


def test_backtick_template_token_carries_parts():
    tokens = lex("`hello ${name}`", "<test>")
    assert tokens[0].kind == "template"
    # interpolations are captured as raw source and re-parsed by the parser
    assert tokens[0].value == [("text", "hello "), ("expr", "name")]


def test_backtick_template_expression_interpolation():
    tokens = lex("`x-${f(a).b + 1}`", "<test>")
    assert tokens[0].value == [("text", "x-"), ("expr", "f(a).b + 1")]


def test_backtick_template_bare_dollar_is_literal_text():
    tokens = lex("`cost: $9.99 for ${item}`", "<test>")
    assert tokens[0].value == [("text", "cost: $9.99 for "), ("expr", "item")]


def test_plain_double_quoted_string_dollar_is_literal():
    tokens = lex('"cost: $9.99"', "<test>")
    assert tokens[0].kind == "string"
    assert tokens[0].value == "cost: $9.99"


# --- §9 guard rail: legacy `$` forms in plain strings are rejected ----------

def test_stale_interpolation_is_rejected_not_silently_literal():
    import pytest
    from revl import RevlError, compile_source

    with pytest.raises(RevlError, match=r"`\$item` in a plain string"):
        compile_source(
            'service B { emission fn send(m: Str) }\n'
            'component C requires b: B { emit b.send("cost for $item") }', "s.rvl")


def test_stale_dollar_escape_is_rejected():
    import pytest
    from revl import RevlError, compile_source

    with pytest.raises(RevlError, match=r"`\$\$` in a plain string"):
        compile_source(
            'service B { emission fn send(m: Str) }\n'
            'component C requires b: B { emit b.send("5$$ off") }', "s.rvl")


def test_non_legacy_dollars_stay_legal():
    from revl import compile_source

    compile_source(
        'service B { emission fn send(m: Str) }\n'
        'component C requires b: B { emit b.send("cost is $5") }', "s.rvl")


# --- roadmap item 70: raw-brace balancing is string/comment aware ------------
#
# Both `${ ... }` interpolation capture and `@backend { ... }` host bodies
# balance raw braces. A `}` inside a string, char literal, or comment must NOT
# be counted as a closing brace, or the capture truncates early and the parse
# fails far from the real cause.

def _template_parts(source):
    tokens = lex(source, "<test>")
    assert tokens[0].kind == "template"
    return tokens[0].value


def test_interpolation_brace_inside_string_is_not_structural():
    # the buggy case: a `}` inside the interpolated string closed the `${` early
    assert _template_parts('`v=${m.lookup("}")}`') == [
        ("text", "v="), ("expr", 'm.lookup("}")')]


def test_interpolation_open_brace_inside_string_is_not_structural():
    assert _template_parts('`v=${m.lookup("{")}`') == [
        ("text", "v="), ("expr", 'm.lookup("{")')]


def test_interpolation_balances_nested_record_and_string_braces():
    assert _template_parts('`r=${ f({a: "}"}) }`') == [
        ("text", "r="), ("expr", 'f({a: "}"})')]


def test_interpolation_line_comment_hides_a_brace():
    parts = _template_parts('`x=${ a // }\n + b }`')
    assert parts[0] == ("text", "x=")
    assert parts[1][0] == "expr" and parts[1][1].startswith("a // }")


def test_interpolation_plain_form_still_works():
    # regression: no braces-in-strings, unchanged behaviour
    assert _template_parts("`x-${f(a).b + 1}`") == [("text", "x-"), ("expr", "f(a).b + 1")]


def test_interpolation_nested_backtick_template_is_opaque():
    assert _template_parts('`a=${ tag(`b=${y}`) }`') == [
        ("text", "a="), ("expr", "tag(`b=${y}`)")]


def _hostbody(source):
    toks = lex(source, "<test>")
    body = next(t for t in toks if t.kind == "hostbody")
    return body.value  # (backend, text)


def test_host_body_brace_inside_string_is_not_structural():
    backend, body = _hostbody('@java { var s = "}"; return s }')
    assert backend == "java"
    assert body == ' var s = "}"; return s '


def test_host_body_brace_inside_block_comment():
    _, body = _hostbody('@go { x := 1 /* } still open */ ; y := 2 }')
    assert "/* } still open */" in body and "y := 2" in body


def test_host_body_single_line_placeholder_comment_still_closes():
    # importers emit single-line bodies whose `}` sits right after a line
    # comment; the terminator must still be found (line comments are NOT skipped
    # inside host bodies — see lexer module note).
    assert _hostbody("@ts { // GET /x here }") == ("ts", " // GET /x here ")
    assert _hostbody("@py { # placeholder }") == ("py", " # placeholder ")
    assert _hostbody("@wasm { ;; call export here }") == ("wasm", " ;; call export here ")


def test_host_body_python_triple_quote_and_single_quotes():
    backend, body = _hostbody("@py { s = '}' \n t = '''}''' \n return s + t }")
    assert backend == "py"
    assert "'}'" in body and "'''}'''" in body


def test_host_body_rust_lifetime_is_not_a_char_literal():
    # `'a` is a lifetime, not an unterminated char literal; the body's real
    # brace still closes it.
    _, body = _hostbody("@rust { fn f<'a>(x: &'a str) -> &'a str { x } }")
    assert body == " fn f<'a>(x: &'a str) -> &'a str { x } "


def test_host_body_rust_char_literal_with_brace():
    _, body = _hostbody("@rust { let c = '}'; c }")
    assert body == " let c = '}'; c "


def test_host_body_nested_braces_still_balance():
    _, body = _hostbody("@ts { if (x) { return {a: 1} } }")
    assert body == " if (x) { return {a: 1} } "


def test_host_body_plain_form_still_works():
    # regression: no braces-in-strings/comments
    _, body = _hostbody("@py { return 1 + 2 }")
    assert body == " return 1 + 2 "


def test_buggy_interpolation_case_compiles_end_to_end():
    # the `}` inside the interpolated string used to truncate the capture and
    # fail the parse far from the cause; it now compiles cleanly.
    from revl import compile_source

    compile_source(
        'service B { emission fn send(m: Str) }\n'
        'component C requires b: B { emit b.send(`v=${"}"}`) }', "s.rvl")
