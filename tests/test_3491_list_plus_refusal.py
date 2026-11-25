"""`+` on two lists is refused with a message that names the working forms (#3491).

`out + ["a"]` on a `List[Str]` was refused with "operand of `+` expects `Int`,
got `List[Str]`" — wrong twice: `+` is not arithmetic-only (`"a" + "b"`
concatenates), and the working forms (`concat`, `push`) were not named. The
refusal now says `+` does not join lists and points at `xs.concat(ys)` /
`xs = xs.push(x)`; for other bad operands of `+` the message names the full
accepted set (`Int, Float, Int32 or Str`). Other operators' messages are
unchanged.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as err:
        compile_source(source, "a.rvl")
    return err.value


def test_list_plus_names_concat_and_push():
    e = _refusal('test "t" { var out: List[Str] = [] out = out + ["a"] '
                 'assert out == ["a"] }\n')
    assert e.code == "T1"
    assert "`+` does not join lists" in str(e)
    assert "List[Str]" in str(e)
    hint = e.hint or ""
    assert "xs.concat(ys)" in hint
    assert "xs = xs.push(x)" in hint


def test_right_operand_list_says_right():
    e = _refusal('test "t" { let a = 1 + [1] }\n')
    assert "`+` does not join lists" in str(e)
    assert "right operand" in str(e)


def test_plus_message_names_all_accepted_types():
    e = _refusal('test "t" { let bad = 1 + true }\n')
    assert "operand of `+` expects `Int, Float, Int32 or Str`, got `Bool`" in str(e)


def test_str_concat_still_works():
    compile_source('test "t" { let s = "a" + "b" assert s == "ab" }\n', "a.rvl")


def test_other_operators_keep_their_message():
    e = _refusal('test "t" { let x = 1 - "s" }\n')
    assert "operand of `-` expects `Int`, got `Str`" in str(e)
