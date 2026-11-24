"""Issue #3500: a refused method whose name matches a stdlib function names
that function and its module in the refusal's hint.

`xs.contains(x)` on a list is refused with the whole method list and no answer;
the refusal now also says "use `list_contains` from stdlib/list.rvl instead"
(the name with the receiver's type head as a prefix), falling back to the bare
name (`contains` on a `Str` -> `contains` from stdlib/str.rvl). A name no
stdlib function matches gets no pointer. The pointer rides the HINT, not the
message: the message is what the gate/reference census compares byte-for-byte,
the hint is what the CLI prints to the author.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import lower  # noqa: E402


def _refusal(src: str):
    with pytest.raises(lower.RevlError) as excinfo:
        compile_source(src, "h.rvl")
    return excinfo.value


def test_a_refused_list_method_names_the_list_function():
    err = _refusal('test "t" {\n  let xs = ["a", "b"]\n  assert xs.contains("a")\n}\n')
    assert "no builtin method `contains`" in err.message
    assert "use `list_contains` from stdlib/list.rvl instead" in err.hint
    # the message itself stays byte-stable (the census compares it)
    assert "list_contains" not in err.message


def test_a_refused_str_method_names_the_bare_function():
    err = _refusal('test "t" {\n  let s = "abc"\n  assert s.contains("b")\n}\n')
    assert "use `contains` from stdlib/str.rvl instead" in err.hint


def test_a_refused_method_with_no_matching_function_gets_no_pointer():
    err = _refusal('test "t" {\n  let xs = ["a"]\n  assert xs.frobnicate("a")\n}\n')
    assert "no builtin method `frobnicate`" in err.message
    assert "use `" not in err.hint


def test_the_component_position_refusal_names_it_too():
    """`_refuse_value_method` is the component-position site (message carries
    the receiver type); it gets the same hint pointer."""
    with pytest.raises(lower.RevlError) as excinfo:
        lower._refuse_value_method("contains", "List[Str]", "h.rvl", 3)
    err = excinfo.value
    assert "no builtin method `contains` on `List[Str]`" in err.message
    assert "use `list_contains` from stdlib/list.rvl instead" in err.hint


def test_builtin_methods_are_still_accepted():
    """The pointer is only appended to a REFUSAL; a real builtin method still
    lowers (no refusal at all)."""
    ir = compile_source('test "t" {\n  let xs = ["a", "b"]\n'
                        '  assert xs.length() == 2\n}\n', "h.rvl")
    assert ir is not None
