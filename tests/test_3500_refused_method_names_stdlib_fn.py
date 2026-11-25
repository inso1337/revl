"""A refused method call names the stdlib function that does the work (#3500).

`xs.contains("a")` on a list is refused with "no builtin method `contains` on
values" plus the whole 38-name surface list — the refusal is right, but the
answer (membership is the function `list_contains(xs, x)` in stdlib/list.rvl)
was not in it. The refusal now prepends a pointer to the refusal's hint when
the method name maps to a stdlib free function: `list_<m>` from
stdlib/list.rvl for a List receiver, `<m>` (camelCase to snake_case) from
stdlib/str.rvl for a Str receiver. Unknown names and record receivers keep the
plain hint.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import lower  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as err:
        compile_source(source, "h.rvl")
    return err.value


def test_list_contains_points_at_list_contains():
    e = _refusal('test "t" { let xs = ["a", "b"] assert xs.contains("a") }\n')
    assert "no builtin method `contains`" in str(e)
    assert "use `list_contains(xs, x)` from stdlib/list.rvl" in (e.hint or "")


def test_str_contains_points_at_str_contains():
    e = _refusal('test "t" { let s = "abc" assert s.contains("b") }\n')
    assert "use `contains(s, part)` from stdlib/str.rvl" in (e.hint or "")


def test_other_list_and_str_methods_point_at_their_function():
    e = _refusal('test "t" { let xs = [1, 2] assert xs.sort() == [1, 2] }\n')
    assert "use `list_sort(xs)` from stdlib/list.rvl" in (e.hint or "")
    e = _refusal('test "t" { let s = " abc " assert s.trim() == "abc" }\n')
    assert "use `trim(s)` from stdlib/str.rvl" in (e.hint or "")
    # camelCase method -> snake_case function
    e = _refusal('test "t" { let s = "a.b" assert s.lastIndexOf(".") == 1 }\n')
    assert "use `last_index_of(s)` from stdlib/str.rvl" in (e.hint or "")


def test_an_unknown_method_keeps_the_plain_hint():
    e = _refusal('test "t" { let xs = ["a"] assert xs.frobnicate("a") }\n')
    hint = e.hint or ""
    assert "records carry data, not methods" in hint
    assert "from stdlib/" not in hint


def test_a_record_receiver_gets_no_pointer():
    e = _refusal('test "t" { let r = { x: 1 } assert r.contains("a") }\n')
    hint = e.hint or ""
    assert "records carry data, not methods" in hint
    assert "from stdlib/" not in hint


def test_the_pointer_table_matches_the_stdlib_exports():
    """Every name in `_STDLIB_FREE_FNS` is a real `pub fn` of its module, so
    the hint never names a function that does not exist."""
    for mod, names in lower._STDLIB_FREE_FNS.items():
        text = (ROOT / mod).read_text(encoding="utf-8")
        exported = set(re.findall(r"^pub fn (\w+)", text, re.M))
        missing = names - exported
        assert not missing, f"{mod} exports no {sorted(missing)}"
