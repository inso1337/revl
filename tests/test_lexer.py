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
    assert tokens[0].value == [("text", "hello "), ("var", "name")]


def test_backtick_template_bare_dollar_is_literal_text():
    tokens = lex("`cost: $9.99 for ${item}`", "<test>")
    assert tokens[0].value == [("text", "cost: $9.99 for "), ("var", "item")]


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
