"""Issue #3491: `+` on lists was refused as "expects `Int`" — wrong about `+`
(it concatenates `Str` too) and silent about the working forms.

Now the refusal names what `+` accepts (`Str, Int or Float`), and a List
operand gets a hint naming `xs.concat(ys)` / `xs.push(x)`. The message change
is safe for the gate/reference census: no baseline entry carries "operand
of" at all.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "a.rvl")
    return excinfo.value


def test_list_plus_refusal_names_concat_and_push():
    err = _refusal('test "t" {\n  var out: List[Str] = []\n  out = out + ["a"]\n'
                   '  assert out == ["a"]\n}\n')
    assert "operand of `+` expects `Str, Int or Float`, got `List[Str]`" \
        in err.message
    assert "`+` does not join lists" in err.hint
    assert "xs.concat(ys)" in err.hint and "xs.push(x)" in err.hint


def test_plus_refusal_names_the_accepted_types():
    err = _refusal('test "t" {\n  let b = 1 + true\n  assert b == 2\n}\n')
    assert "operand of `+` expects `Str, Int or Float`, got `Bool`" in err.message


def test_str_and_numeric_plus_still_compile():
    ir = compile_source('test "t" {\n  let s = "a" + "b"\n'
                        '  let n = 1 + 2.5\n  assert s == "ab" && n == 3.5\n}\n',
                        "a.rvl")
    assert ir is not None


def test_other_operators_keep_their_message():
    err = _refusal('test "t" {\n  let x = true - 1\n  assert x == 0\n}\n')
    assert "operand of `-` expects `Int`, got `Bool`" in err.message
