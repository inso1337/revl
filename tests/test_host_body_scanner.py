"""The shared host-body scanner (`revl.host_body`).

`tests/test_host_body_lexical_escape.py` holds the per-issue regression tests
through the emitters. This file pins the scanner itself: what `body_lines`
splits on, which lexical forms each host language skips (so a safe body is
never refused), and that a refusal is raised through the CALLER's own error
class.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import host_body  # noqa: E402
from revl.host_body import (  # noqa: E402
    HostBodyError,
    body_lines,
    check_host_body,
)


# --------------------------------------------------------------- body_lines
def test_body_lines_splits_on_newlines_only():
    body = "a\u0085b\u2028c\u2029d\x0be\x0cf\x1cg\x1dh\x1ei\nj"
    assert body_lines(body) == [
        "a\u0085b\u2028c\u2029d\x0be\x0cf\x1cg\x1dh\x1ei",
        "j",
    ]


def test_body_lines_normalises_crlf_and_cr():
    assert body_lines("a\r\nb\rc") == ["a", "b", "c"]


def test_body_lines_matches_splitlines_on_ordinary_text():
    assert body_lines("a\nb\n") == ["a", "b"]
    assert body_lines("") == [""]


# ---------------------------------------------------------- accepted bodies
_ACCEPTED = [
    ("ts", "return /a{2}/.test(s)"),
    ("ts", "return s.replace(/[}{]/g, '')"),
    ("ts", "return `x${ {a: 1}.a }y`"),
    ("ts", "return `nested ${`${x}`}`"),
    ("ts", "// } { } }\nreturn x"),
    ("ts", "/* } { */ return x"),
    ("ts", "return x / 2"),
    ("rs", "let s = r#\"a } { \"#;\nreturn x"),
    ("rs", "let s = br##\"} {\"##;\nreturn x"),
    ("rs", "fn pick<'a>(s: &'a str) -> &'a str { s }"),
    ("rs", "let c = '}';\nreturn x"),
    ("rs", "/* outer /* inner } { */ still */ return x"),
    ("java", 'String s = "} {";\nreturn s'),
    ("java", "char c = '}';\nreturn x"),
    ("go", "return `a } {`"),
    ("go", "return '}'"),
    ("go", "// } { } }\nreturn x"),
    ("wasm", "(; ) } ;) (i64.const 1)"),
    ("wasm", '(i32.const 41) ;; ) )\n(i32.const 1)'),
    ("wasm", '(i32.const 41) (; nested (; ) ;) ;)'),
]


@pytest.mark.parametrize(("backend", "body"), _ACCEPTED)
def test_balanced_bodies_are_accepted(backend, body):
    assert check_host_body(backend, body, error=HostBodyError) is None


# ----------------------------------------------------------- refused bodies
_REFUSED = [
    ("ts", "return x })())"),
    ("ts", "{ return x"),
    ("rs", "return x }"),
    ("java", "return x; }"),
    ("go", "return x }"),
    ("wasm", "(local.get $p_x))"),
    ("wasm", "(local.get $p_x"),
]


@pytest.mark.parametrize(("backend", "body"), _REFUSED)
def test_unbalanced_bodies_are_refused(backend, body):
    with pytest.raises(HostBodyError):
        check_host_body(backend, body)


# ------------------------------------------ JavaScript: `/` with two readings
# Each body is balanced under one reading of its `/` and leaves the function
# under the other, the one JavaScript (or TypeScript) takes. The last line
# pads the count back, so a scanner that picks one reading accepts the body.
_TS_PAD = '\nglobalThis.PWNED = 1\nfunction pad() { if (1) /}/.test("")'
_TS_EITHER_READING = [
    # a keyword after `.` is a property name: `o.return / 1` divides
    "// {\nconst r = o.return /1}/1/g" + _TS_PAD,
    "// {\nconst r = o?.typeof /1}/1/g" + _TS_PAD,
    # a private name is not the keyword it ends with
    "// {\nconst r = this.#return /1}/1/g" + _TS_PAD,
    # TypeScript's non-null `o!` and a postfix `++` end an operand
    "// {\nconst r = o! /1}/1/g" + _TS_PAD,
    "// {\nconst r = o++ /1}/1/g" + _TS_PAD,
    # no-break space is JavaScript white space, so `/{/` follows `return`
    "return\u00a0/{/\n}\nglobalThis.PWNED = 1\nfunction pad() { return\u00a0/}/",
    # a comment between `return` and the `/` does not make it division
    "return // c\n/{/\n}\nglobalThis.PWNED = 1\n"
    "function pad() { return // c\n/}/",
]


@pytest.mark.parametrize("body", _TS_EITHER_READING)
def test_ts_slash_is_scanned_under_both_readings(body):
    with pytest.raises(HostBodyError):
        check_host_body("ts", body)


_TS_ONE_READING = [
    "return !/[}]/.test(s)",
    "return s.split(/}/).length",
    "return xs.filter(x => /}/.test(x)).length",
    "if (s) { return /[{]/.test(s) }\nreturn false",
    "const h = (a + b) / 2 / c\nif (h) { return h }\nreturn a.return / 2",
    'return `${ {a: 1}.a } ${ s.replace(/[{]/g, "") }`',
]


@pytest.mark.parametrize("body", _TS_ONE_READING)
def test_ts_slash_with_one_reading_is_accepted(body):
    assert check_host_body("ts", body) is None


# A keyword after a member-access `.` (or `?.`) is a property name, so the
# `/` after it divides. The third dot of a spread and the dot that ends a
# numeric literal are no member access: the keyword keeps its meaning.
_TS_KEYWORDS = (
    "await", "case", "default", "delete", "do", "else", "extends", "in",
    "instanceof", "new", "of", "return", "throw", "typeof", "void", "yield",
)
_NOT_A_MEMBER_DOT = [
    ("[...", "]"), ("[... ", "]"), ("{...", "}"), ("{... ", "}"),
    ("[1. ", "]"),
]


def test_keyword_list_matches_the_scanner():
    assert frozenset(_TS_KEYWORDS) == host_body._REGEX_KEYWORDS


@pytest.mark.parametrize("keyword", _TS_KEYWORDS)
@pytest.mark.parametrize("opener,closer", _NOT_A_MEMBER_DOT)
def test_ts_keyword_after_a_spread_or_number_dot_is_no_property(
        keyword, opener, closer):
    body = (f"let q = {opener}{keyword} /[{{]/{closer}\n}}\n"
            f"globalThis.PWNED = 1\n"
            f"function pad() {{ let r = {opener}{keyword} /[}}]/{closer}")
    with pytest.raises(HostBodyError):
        check_host_body("ts", body)


# Each body is accepted only when its `/` divides: read as a regex, the `/`
# in the string closes it and the `{` after it opens a block.
@pytest.mark.parametrize("expr", [
    "a.typeof / b", "a?.in", "x.void", "o.new", "1.5", ".5", "1.5e+3",
    "1..toString().length", "a\n  .in", "this.#in", "c ?.5", "0xFF", "10n",
    "1 .in",
    # a radix, BigInt or exponent literal ends before the dot: a member
    # access follows, even with a keyword name
    "0x1.toString().length", "1e3.toFixed().length", "1n.toString().length",
    "0x1. in", "0o7. in", "0B1. typeof", "1n. in", "1e3. in", "1_0e1. void",
])
def test_ts_member_access_and_numbers_divide(expr):
    assert check_host_body("ts", f'return {expr} / f("/{{")') is None


# A radix, BigInt or exponent literal takes no fraction, so the dot after it
# is a member access and the `/` divides: JavaScript closes the function at
# the `}` a regex reading would hide.
@pytest.mark.parametrize("literal", [
    "0x1.", "0o7.", "0B1.", "0x1_f.", "1n.", "0b1n.", "1e3.", "1E3.",
    "0E1.", "1_0e1.", "1e+3.",
])
def test_ts_dot_after_a_literal_without_fraction_is_a_member_access(literal):
    body = (f"let z = /[{{]/; let q = {literal} in /1}}; "
            f"globalThis.PWNED = 1; function g(){{ 2/1; let w = /[}}]/")
    with pytest.raises(HostBodyError):
        check_host_body("ts", body)


# A hex `e` is a digit, not an exponent: `0xe+1.` is `0xe + 1.`, and the
# `in` after the decimal `1.` is the operator, so a regex follows.
@pytest.mark.parametrize("prefix", [
    "0xe+1.", "0xE-1.", "0xae+1.", "0b1+1.", "1n+1.", "1e3+1.",
])
def test_ts_sign_after_a_radix_digit_starts_a_new_number(prefix):
    body = (f"let q = {prefix} in /[{{]/\n}}\nglobalThis.PWNED = 1\n"
            f"function pad() {{ let r = {prefix} in /[}}]/")
    with pytest.raises(HostBodyError):
        check_host_body("ts", body)


def test_ts_division_control_is_refused_when_misread():
    with pytest.raises(HostBodyError):
        check_host_body("ts", 'return typeof / f("/{")')


# ----------------------------------------------- Java unicode escapes (JLS 3.3)
_JAVA_ESCAPED = [
    "return x; \\u007d static \\u007b System.out.println(1); \\u007d "
    "static long pad() \\u007b return 0;",
    # any number of `u`
    "return x; \\uuu007d static \\u007b System.out.println(1); \\u007d "
    "static long pad() \\u007b return 0;",
    # an escaped line feed ends a `//` comment; an escaped backslash keeps
    # the last string open, so its `}` pads the count back
    "// \\u000a return x; } static { System.out.println(1); }\n"
    'static long pad() { String t = "\\u005c" }"; return 0;',
    # three backslashes: a pair, then an escape
    'String s = "\\\\\\u0022; } static { System.out.println(1); '
    '} static long pad() { String t = "";',
]


@pytest.mark.parametrize("body", _JAVA_ESCAPED)
def test_java_unicode_escapes_are_decoded_before_the_scan(body):
    with pytest.raises(HostBodyError):
        check_host_body("java", body)


@pytest.mark.parametrize("body", [
    'return "\\\\u007d";',
    "// a \\\\u000a }\nreturn x;",
    "char c = '\\u007b'; return c;",
])
def test_java_even_backslashes_and_balanced_escapes_are_accepted(body):
    assert check_host_body("java", body) is None


# ------------------------------------------------ bodies left open at the end
# The emitter writes code after the body; a literal or comment the body leaves
# open would swallow it, up to a later body that closes it.
@pytest.mark.parametrize("backend,body", [
    ("ts", "return `a"),
    ("ts", "return `${a}"),
    ("ts", 'return "a\\'),
    ("ts", "return x /* c"),
    ("rs", 'return "a'),
    ("rs", 'return r#"a'),
    ("go", "return `a"),
    ("java", 'return """\na'),
    ("java", "return x; /* c"),
])
def test_body_ending_inside_a_literal_or_comment_is_refused(backend, body):
    with pytest.raises(HostBodyError) as excinfo:
        check_host_body(backend, body)
    assert "ends inside a string, comment or template" in str(excinfo.value)


@pytest.mark.parametrize("backend,body", [
    ("ts", "return x // c"),
    ("ts", 'return "a\\\r\nb"'),
    ("ts", "return `a\\\r\nb`"),
    ("rs", "return x // c"),
    ("go", "return x // c"),
])
def test_body_ending_in_a_line_comment_or_continued_string_is_accepted(
        backend, body):
    assert check_host_body(backend, body) is None


# ------------------------------------------------------ cost of the scan
class _TooMuchWork(AssertionError):
    pass


def _count_tokens(monkeypatch, bound):
    """Count the tokens the scanner steps over; stop past `bound`."""
    calls = [0]
    token = host_body._Scan._token

    def counted(self, i):
        calls[0] += 1
        if calls[0] > bound:
            raise _TooMuchWork(f"more than {bound} tokens")
        return token(self, i)

    monkeypatch.setattr(host_body._Scan, "_token", counted)
    return calls


# `a\n/[{]/` stays ambiguous (the line break may end the statement), so
# every unit doubles the readings unless equal states are merged.
@pytest.mark.parametrize("unit", [
    "`${(a)/[{]/}`;", "(a) /[{]/ ;\n", "a\n/[{]/ ;\n",
])
def test_ambiguous_slashes_are_scanned_in_linear_work(monkeypatch, unit):
    counts = []
    for repeat in (1000, 2000):
        body = unit * repeat
        calls = _count_tokens(monkeypatch, 2 * len(body))
        with pytest.raises(HostBodyError):
            check_host_body("ts", body)
        assert calls[0] <= 2 * len(body)
        counts.append(calls[0])
        monkeypatch.undo()
    # twice the body, at most twice the work (plus the fixed start)
    assert counts[1] <= 2 * counts[0] + 64


def test_readings_past_the_step_cap_are_refused(monkeypatch):
    # each `/(/` may open a parenthesis and each `/)/` close one, so the
    # readings keep disagreeing on which are open; none nests past 30
    body = "{} /(/ ;\n" * 30 + "{} /)/ ;\n" * 30
    _count_tokens(monkeypatch, 16 * len(body) + 8192)
    with pytest.raises(HostBodyError) as excinfo:
        check_host_body("ts", body)
    message = str(excinfo.value)
    assert "more possible readings" in message
    assert "nest" not in message


@pytest.mark.parametrize("body", [
    "return " + "`${" * 5000,
    "return " + "(" * 65 + "1" + ")" * 65,
])
def test_deep_nesting_is_refused_without_recursion(body):
    with pytest.raises(HostBodyError) as excinfo:
        check_host_body("ts", body)
    message = str(excinfo.value)
    assert "nests more than 64 template literals or parentheses" in message
    assert "possible readings" not in message


# --------------------------------------------------------- separators (#3131)
@pytest.mark.parametrize("backend", ["ts", "rs", "java", "go"])
@pytest.mark.parametrize(
    "separator", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
                  "\u2028", "\u2029"])
def test_separators_outside_a_string_are_refused(backend, separator):
    with pytest.raises(HostBodyError) as excinfo:
        check_host_body(backend, f"// c{separator}code\nreturn x")
    assert "outside a string literal" in str(excinfo.value)


@pytest.mark.parametrize("body", [
    # code inside a template interpolation is code, not template text
    "return `${a /* */}`",
    # a regex literal cannot hold a line terminator, so this `/` divides
    "return a /b / c",
])
def test_ts_separators_in_interpolation_or_regex_reading_are_refused(body):
    with pytest.raises(HostBodyError) as excinfo:
        check_host_body("ts", body)
    assert "outside a string literal" in str(excinfo.value)


@pytest.mark.parametrize("backend", ["ts", "rs", "java", "go"])
def test_separators_inside_a_string_are_allowed(backend):
    assert check_host_body(backend, 'return "a\u2028b"') is None


def test_py_bodies_have_no_brace_or_separator_scan():
    # Python bodies are indentation-structured; the split fix covers them.
    assert check_host_body("py", "# c\u2028import os") is None


# ------------------------------------------------------------- refusal text
class _TierRefusal(ValueError):
    """Stands in for a backend's own EmitError."""


def test_refusal_uses_the_callers_error_class_and_names_the_extern():
    with pytest.raises(_TierRefusal) as excinfo:
        check_host_body(
            "ts", "return x }",
            where="extern `dead`", error=_TierRefusal,
        )
    message = str(excinfo.value)
    assert message.startswith("extern `dead`: ")
    assert "@ts host body" in message
    assert "brace-balanced" in message


def test_unknown_backend_is_a_programming_error():
    with pytest.raises(ValueError):
        check_host_body("not-a-tier", "}")
