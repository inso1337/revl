"""Issue #2901: a TypeScript `==` against a Str, Int or Bool literal is `===`.

`revlEq` opens with `a === b` and answers false for any pair where either side
is not an object, so a literal operand makes the call and the bare operator
agree on every value. The emitter writes the operator there and keeps `revlEq`
for everything structural.

Cases that keep `revlEq` on purpose: a record or list operand (structural),
two literals (`1n === 2n` is a tsc TS2367), a variable bound to a literal by an
immutable `let` (its TS type is the literal, same error), a Float literal, and
an operand carrying the Int to Float widening marker of issue #2715.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402


def _emit_ts(source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        "emit_ts_2901", ROOT / "backends" / "typescript" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source, "lit_eq_2901.rvl"))


def _body(emitted: str, fn: str) -> str:
    """The emitted text of one exported function, signature to closing brace."""
    start = emitted.index(f"function {fn}(")
    return emitted[start:emitted.index("\n}", start)]


LITERALS = """
pub fn s_eq(s: Str) -> Bool { return s == "a" }
pub fn s_ne(s: Str) -> Bool { return s != "a" }
pub fn i_eq(n: Int) -> Bool { return n == 3 }
pub fn i_flip(n: Int) -> Bool { return 3 == n }
pub fn i_ne(n: Int) -> Bool { return n != 3 }
pub fn b_eq(b: Bool) -> Bool { return b == true }
pub fn b_ne(b: Bool) -> Bool { return b != false }
"""

STRUCTURAL = """
type Row = { id: Int }
pub fn rec(a: Row, b: Row) -> Bool { return a == b }
pub fn lst(a: List[Int], b: List[Int]) -> Bool { return a == b }
pub fn both(a: Int) -> Bool { return 1 == 2 }
pub fn const_var() -> Bool { let k = 4
  return k == 5 }
pub fn fl(x: Float) -> Bool { return x == 3.0 }
pub fn mixed(x: Float) -> Bool { return x == 3 }
pub fn widened(x: Int) -> Bool { return x == 3.0 }
"""


def test_literal_comparison_emits_strict_equality():
    emitted = _emit_ts(LITERALS)
    for fn, want in [
        ("s_eq", '(s === "a")'), ("s_ne", '(s !== "a")'),
        ("i_eq", "(n === 3n)"), ("i_flip", "(3n === n)"), ("i_ne", "(n !== 3n)"),
        ("b_eq", "(b === true)"), ("b_ne", "(b !== false)"),
    ]:
        body = _body(emitted, fn)
        assert want in body, f"{fn}: {body}"
        assert "revlEq(" not in body, f"{fn}: {body}"


def test_structural_comparisons_keep_revl_eq():
    emitted = _emit_ts(STRUCTURAL)
    for fn in ("rec", "lst", "both", "const_var", "fl", "mixed", "widened"):
        assert "revlEq(" in _body(emitted, fn), f"{fn}: {_body(emitted, fn)}"
    assert "revlEq(a, b)" in _body(emitted, "rec")
    assert "revlEq(a, b)" in _body(emitted, "lst")
    # #2715: the Int against Float comparisons are unchanged, byte for byte
    assert "revlEq(x, 3.0)" in _body(emitted, "fl")
    assert "revlEq(x, Number(3n))" in _body(emitted, "mixed")
    assert "revlEq(Number(x), 3.0)" in _body(emitted, "widened")


RUN = """
pub fn s_eq(s: Str) -> Bool { return s == "a" }
pub fn s_ne(s: Str) -> Bool { return s != "a" }
pub fn i_eq(n: Int) -> Bool { return n == 3 }
pub fn i_eq_flip(n: Int) -> Bool { return 3 == n }
pub fn i_ne(n: Int) -> Bool { return n != 3 }
pub fn neg_eq(n: Int) -> Bool { return n == -5 }
pub fn big_eq(n: Int) -> Bool { return n == 9007199254740993 }
pub fn big_ne(n: Int) -> Bool { return n != 9007199254740993 }
pub fn b_eq(b: Bool) -> Bool { return b == true }
pub fn b_ne(b: Bool) -> Bool { return b != true }
pub fn f_eq(x: Float) -> Bool { return x == 3 }
pub fn f_lit(x: Int) -> Bool { return x == 3.0 }
pub fn const_var() -> Bool { let k = 4
  return k == 4 && k != 5 }

test "Str against a literal" {
  assert s_eq("a")
  assert !s_eq("b")
  assert !s_eq("")
  assert s_ne("b")
  assert !s_ne("a")
}
test "Int against a literal" {
  assert i_eq(3)
  assert i_eq_flip(3)
  assert !i_eq(4)
  assert !i_eq_flip(4)
  assert i_ne(4)
  assert !i_ne(3)
}
test "negative Int" {
  assert neg_eq(0 - 5)
  assert !neg_eq(5)
  assert !neg_eq(0 - 4)
}
test "Int above 2^53 is exact" {
  assert big_eq(9007199254740993)
  assert !big_eq(9007199254740992)
  assert !big_eq(9007199254740994)
  assert big_ne(9007199254740992)
  assert !big_ne(9007199254740993)
}
test "Bool against a literal" {
  assert b_eq(true)
  assert !b_eq(false)
  assert b_ne(false)
  assert !b_ne(true)
}
test "Int and Float comparisons keep their answers" {
  assert f_eq(3.0)
  assert f_lit(3)
  assert !f_lit(4)
}
test "a variable bound to a literal" {
  assert const_var()
}
"""


def test_literal_comparisons_agree_across_values():
    status, message = RUNNERS["ts"](compile_source(RUN, "lit_eq_2901_run.rvl"))
    if status == "skip":
        pytest.skip(f"ts: {message}")
    assert status == "pass", message
    status_py, message_py = RUNNERS["py"](compile_source(RUN, "lit_eq_2901_run.rvl"))
    assert status_py == "pass", f"py reference: {message_py}"
