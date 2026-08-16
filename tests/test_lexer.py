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
                 "match", "test", "assert", "async", "as"):
        assert lex(word, "<test>")[0].kind == "kw", word


def test_adts_and_ternary_use_single_pipe_and_question():
    assert _kinds("A | B") == ["ident", "|", "ident", "eof"]
    assert _kinds("c ? a : b") == ["ident", "?", "ident", ":", "ident", "eof"]
