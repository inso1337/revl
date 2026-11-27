"""The `${...}`-in-a-plain-string refusal names the literal form (#3502).

A complete `${...}` in a plain `"..."` string is refused on purpose (item 380:
a likely forgotten backtick), but the old message said the `${...}` "is emitted
as literal text" — reading as a warning while acting as a refusal — and named
only the interpolating form, the opposite of what an author writing shell/JS
as data wants. The message now says the shape IS refused and names the literal
workaround, the triple-quoted string. docs/strings.md states the rule.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _err(source: str) -> str:
    with pytest.raises(RevlError) as e:
        compile_source(source, "j.rvl")
    return str(e.value) + "\n" + (e.value.hint or "")


def test_the_refusal_says_refused_and_names_the_triple_quoted_form():
    err = _err('test "dollar brace" { let s = "echo ${HOME}" assert s.length() == 12 }\n')
    assert "does not interpolate" in err
    assert "refused, not emitted literally" in err
    assert "backtick template" in err and "`hi ${name}`" in err
    assert '"""echo ${HOME}"""' in err


def test_single_quoted_spelling_gets_the_same_message():
    err = _err("pub fn greet(name: Str) -> Str { return 'hi ${name}' }\n")
    assert "refused, not emitted literally" in err
    assert '"""' in err


def test_the_triple_quoted_form_compiles_with_the_literal_length():
    ir = compile_source(
        'test "t" { let s = """echo ${HOME}""" assert s.length() == 12 }\n', "j.rvl")
    assert ir is not None


def test_an_incomplete_shape_is_still_literal():
    # `"a${b"` has no closing brace: literal, accepted (unchanged)
    ir = compile_source('test "t" { let s = "a${b" assert s.length() == 4 }\n', "j.rvl")
    assert ir is not None


def test_a_backtick_in_the_string_disables_the_refusal():
    # a plain string carrying a backtick quotes template/shell source as DATA
    ir = compile_source('test "t" { let s = "`hi ${name}!`" assert s.length() == 12 }\n',
                         "j.rvl")
    assert ir is not None
