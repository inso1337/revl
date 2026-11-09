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


# --------------------------------------------------------- separators (#3131)
@pytest.mark.parametrize("backend", ["ts", "rs", "java", "go"])
@pytest.mark.parametrize(
    "separator", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
                  "\u2028", "\u2029"])
def test_separators_outside_a_string_are_refused(backend, separator):
    with pytest.raises(HostBodyError) as excinfo:
        check_host_body(backend, f"// c{separator}code\nreturn x")
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
