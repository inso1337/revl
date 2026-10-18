"""An unpinned list of nested generic cases builds on the go tier (issue #2497).

WHAT WAS WRONG. `[Jj(Nn), Jj(Jj(1)), Nn]` is a `List[Mb[Mb[Int]]]`: only the
second item pins the inner type argument. `_v3_list_elem` picked the element
type by checking the TOP-LEVEL type arguments of each item, so `Jj(Nn)`
(`Mb[Mb[any]]`, every top-level argument bound) won, and the construction of
its inner `Nn` was refused by name while py and rust ran the program.

WHAT IS EMITTED NOW. The element type is the first item type with no
unbound type argument at ANY depth, so the list is `[]Mb[Mb[int64]]` and
each item takes its instantiation from it.

The run test executes the program's `test` block on the py tier and on go
and asserts both pass. It needs `go` and skips without it; a skip is not a
pass, CI's cross-tier step names go in REVL_REQUIRE_TIERS.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_go_emit_2497", HERE / "emit.py")
go_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(go_emit)

PROGRAM = """\
type Mb[T] = Jj(T) | Nn

fn depth(m: Mb[Mb[Int]]) -> Int {
  return match m {
    Jj(inner) => match inner {
      Jj(x) => x,
      Nn => -1,
    },
    Nn => -2,
  }
}

pub fn h() -> Int {
  let xs = [Jj(Nn), Jj(Jj(1)), Nn]
  var acc = 0
  for (m of xs) {
    acc = acc + depth(m)
  }
  return acc + xs.length()
}

test "h" { assert h() == 1 }
"""


def _ir():
    return compile_source(PROGRAM, "unpinned_case_2497.rvl")


def test_the_list_element_type_is_the_fully_pinned_item():
    src = go_emit.emit(_ir())
    assert "[]Mb[Mb[int64]]{" in src, src
    assert "MbJj[Mb[int64]]{Value: MbNn[int64]{}}" in src, src
    assert "any" not in src.split("func h()")[1].split("}\n")[0], src


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_the_nested_case_list_like_py():
    ir = _ir()
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "1 test(s) passed" in message and "1 test(s) passed" in py_message, (
        py_message, message)
