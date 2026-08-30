"""Lexer sugar: non-decimal / grouped int literals (item 381) and
single-quoted strings (item 382). Both are pure-lexer, additive changes."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.errors import RevlError  # noqa: E402
from revl.lexer import lex  # noqa: E402


def _toks(source):
    return lex(source, "<test>")


def _one_int(source):
    ts = _toks(source)
    assert [t.kind for t in ts] == ["int", "eof"], [t.kind for t in ts]
    return ts[0].value


def _one_string(source):
    ts = _toks(source)
    assert [t.kind for t in ts] == ["string", "eof"], [t.kind for t in ts]
    return ts[0].value


# --- item 381: non-decimal + digit-grouped integer literals -----------------

def test_hex_literals():
    assert _one_int("0xFF") == 255
    assert _one_int("0x00") == 0
    assert _one_int("0xdeadBEEF") == 0xDEADBEEF
    assert _one_int("0XFF") == 255  # upper-case prefix accepted


def test_binary_literals():
    assert _one_int("0b1010") == 10
    assert _one_int("0b0") == 0
    assert _one_int("0B1111") == 15


def test_octal_literals():
    assert _one_int("0o17") == 15
    assert _one_int("0o0") == 0
    assert _one_int("0O755") == 0o755


def test_digit_grouping_decimal():
    assert _one_int("1_000_000") == 1000000
    assert _one_int("1_0") == 10
    assert _one_int("123") == 123  # unchanged


def test_digit_grouping_non_decimal():
    assert _one_int("0xFF_FF") == 0xFFFF
    assert _one_int("0b1010_1010") == 0b10101010
    assert _one_int("0o1_7") == 0o17


def test_grouped_float():
    ts = _toks("1_000.5")
    assert [t.kind for t in ts] == ["float", "eof"]
    assert ts[0].value == 1000.5


@pytest.mark.parametrize("bad", ["_1", "1_", "1__0", "0xFF_", "0x_FF", "0xFF__FF", "0x", "0b", "0o"])
def test_malformed_number_literals_rejected(bad):
    with pytest.raises(RevlError):
        _toks(bad)


def test_decimal_still_lexes_dot_method():
    # `7.foo()` stays int + dot + method call (byte-identical to before).
    assert [t.kind for t in _toks("7.foo()")] == ["int", ".", "ident", "(", ")", "eof"]


# --- item 382: single-quoted strings ----------------------------------------

def test_single_quote_string_char():
    assert _one_string("'a'") == "a"


def test_single_quote_string_word():
    assert _one_string("'hello'") == "hello"
    # identical token kind + value to the double-quoted spelling
    assert _toks("'hello'")[0].kind == _toks('"hello"')[0].kind == "string"
    assert _toks("'hello'")[0].value == _toks('"hello"')[0].value


def test_single_quote_empty():
    assert _one_string("''") == ""


def test_single_quote_escapes():
    assert _one_string(r"'it\'s'") == "it's"     # \' escapes the quote
    assert _one_string(r"'a\\b'") == "a\\b"        # \\ is one backslash
    assert _one_string(r"'a\nb'") == "a\\nb"       # \n stays literal (no escape)


def test_single_quote_can_hold_double():
    assert _one_string("'say \"hi\"'") == 'say "hi"'


def test_unterminated_single_quote():
    with pytest.raises(RevlError):
        _toks("'oops")
