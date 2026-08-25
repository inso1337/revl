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


# --- item 183: `\"` and `\\` are the plain string's only escapes -------------
#
# `"a\"b"` used to fail with `unterminated string literal` (the `\"` closed the
# string) and a lone `\` surfaced as `unexpected character '\'`. Both now
# escape a literal quote / backslash. No other backslash sequence is an escape:
# `\n` stays a literal backslash-`n`, matching the no-escape triple-quoted form.

def test_escaped_quote_is_a_literal_quote():
    assert _one_string(r'"a\"b"') == 'a"b'


def test_escaped_backslash_is_a_literal_backslash():
    assert _one_string(r'"a\\b"') == "a\\b"


def test_escaped_quote_does_not_terminate_the_string():
    # the `\"` is content, so the *following* `"` closes the string
    tokens = lex(r'"say \"hi\""', "<test>")
    assert tokens[0].kind == "string" and tokens[1].kind == "eof"
    assert tokens[0].value == 'say "hi"'


def test_trailing_escaped_backslash_before_close():
    # `\\` collapses to one backslash, then the next `"` closes the string
    assert _one_string(r'"path\\"') == "path\\"


def test_backslash_n_is_still_two_literal_characters():
    # additive: existing no-escape behaviour for `\n` is unchanged
    assert _one_string(r'"a\nb"') == "a\\nb"


def test_lone_backslash_before_other_char_stays_literal():
    assert _one_string(r'"a\tb"') == "a\\tb"


# --- item 203: a bare `$` in a plain string is ordinary literal text ---------
#
# In 2.0 interpolation lives ONLY in backtick templates (`` `${name}` ``), so
# every `$`-shape inside a plain `"..."` is literal — including `$identifier`
# (the WAT/target fragments the self-host tiers emit, e.g. `"call $int_add"`)
# and `$$`. The old §9 guard that rejected `$name`/`$$` is gone; `revl fmt
# --migrate` still rewrites a legacy `"$name"` to a template under its own gate
# policy rather than relying on this lexer to reject it (see tests/test_fmt.py).

def test_dollar_identifier_in_plain_string_is_literal():
    # the item-203 headline case: a WAT/target fragment must lex, not raise.
    tokens = lex('"call $int_add"', "<test>")
    assert tokens[0].kind == "string"
    assert tokens[0].value == "call $int_add"


def test_bare_dollar_x_is_literal():
    assert _one_string('"$x"') == "$x"


def test_double_dollar_is_two_literal_dollars():
    # 1.x read `$$` as one escaped dollar; 2.0 has no escape, so it is literal.
    assert _one_string('"5$$ off"') == "5$$ off"


def test_former_legacy_interpolation_now_lexes_as_literal():
    # `"cost for $item"` used to be REJECTED (1.x interpolation); it is now a
    # plain literal that compiles cleanly.
    from revl import compile_source

    compile_source(
        'service B { emission fn send(m: Str) }\n'
        'component C requires b: B { emit b.send("cost for $item") }', "s.rvl")


def test_non_ident_dollar_stays_legal():
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


# --- roadmap item 85: triple-quoted multi-line string literals ---------------
#
# `"""..."""` is a verbatim (no-escape, no-interpolation) string whose body may
# contain newlines, so an agent can author a multi-line `.rvl` literal without
# concatenation. Only `"""` closes it; a lone `"`/`""` inside is ordinary text;
# a single newline right after the opening `"""` is stripped. The single-`"`
# form is untouched. See lexer `_lex_triple_string` and docs/strings.md.

def _one_string(source):
    tokens = lex(source, "<test>")
    assert tokens[0].kind == "string", tokens[0]
    assert tokens[1].kind == "eof", tokens
    return tokens[0].value


def test_triple_quoted_multiline_is_one_string_token_with_newline():
    # a real embedded newline survives verbatim; the leading newline after the
    # opening delimiter is stripped, so the body starts at "multi".
    assert _one_string('"""\nmulti\nline"""') == "multi\nline"


def test_triple_quoted_without_leading_newline_keeps_everything():
    assert _one_string('"""multi\nline"""') == "multi\nline"


def test_triple_quoted_strips_only_one_leading_newline():
    # a *second* blank line is intentional content and is preserved.
    assert _one_string('"""\n\nbody"""') == "\nbody"


def test_triple_quoted_empty_string():
    assert _one_string('""""""') == ""


def test_triple_quoted_body_may_contain_lone_and_double_quotes():
    # only `"""` closes; interior `"` and `""` are literal text.
    assert _one_string('"""a"b""c"""') == 'a"b""c'


def test_triple_quoted_is_verbatim_no_escapes():
    # no escape processing: backslash-n is two literal characters, matching the
    # single-`"` no-escape rule (the whole point of the feature).
    assert _one_string('"""a\\nb"""') == "a\\nb"


def test_triple_quoted_does_not_interpolate_dollar():
    # unlike the backtick template, `${...}` and `$ident` are literal here.
    assert _one_string('"""cost ${x} and $item"""') == "cost ${x} and $item"


def test_triple_quoted_line_tracking_continues_after_close():
    # the token carries its opening line; a following token sees the real line.
    tokens = lex('"""x\ny\nz"""\nlet', "<test>")
    assert tokens[0].kind == "string" and tokens[0].line == 1
    kw = next(t for t in tokens if t.kind == "kw")
    assert kw.value == "let" and kw.line == 4


def test_single_quoted_string_unchanged():
    # full back-compat: the single-`"` form is exactly as before.
    assert _one_string('"plain"') == "plain"
    tokens = lex('"a" "b"', "<test>")
    assert [t.value for t in tokens[:2]] == ["a", "b"]


def test_unterminated_triple_quoted_string_is_an_error():
    import pytest
    from revl import RevlError

    with pytest.raises(RevlError, match="unterminated triple-quoted string"):
        lex('"""never closed', "<test>")


# --- item 85 emit/round-trip: a multi-line literal lowers to a valid,
# single-line escaped literal on every tier (lexer-only change; the emitters'
# existing string escapers already convert the embedded newline). py + ts are
# pinned here as the required minimum.

def _emit(backend, source):
    import importlib.util

    from revl import compile_source
    ir = compile_source(source)
    sys.path.insert(0, str(ROOT / "backends" / backend))
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_multiline_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.emit(ir))
    finally:
        sys.path.remove(str(ROOT / "backends" / backend))


_MULTILINE_SRC = 'fn greet() -> Str { return """\nmulti\nline""" }\n'


def test_python_tier_emits_escaped_newline_literal():
    out = _emit("python", _MULTILINE_SRC)
    # the embedded newline becomes a `\n` escape in a valid single-line literal
    assert "multi\\nline" in out
    assert "multi\nline" not in out  # no raw newline splitting the literal
    # the emitted module is valid Python
    compile(out, "greet.py", "exec")


def test_typescript_tier_emits_escaped_newline_literal():
    out = _emit("typescript", _MULTILINE_SRC)
    assert 'multi\\nline' in out
    assert "multi\nline" not in out
