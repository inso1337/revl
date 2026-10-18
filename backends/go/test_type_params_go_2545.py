"""Explicit type-parameter lists and return-only type parameters build on the
go tier (issue #2545).

WHAT WAS WRONG. The IR carried a generic function's parameter spellings but
not the author's `[Elem]` list, so the emitter applied the checker's implicit
rule (a single uppercase name is a type parameter) and `fn first[Elem](xs:
List[Elem]) -> Elem` emitted `func first(xs []Elem) Elem`: `undefined: Elem`.
And a type parameter only the return mentions (`fn empty[T]() -> List[T]`)
had nothing at the call to bind it: `let xs: List[Int] = empty()` emitted
`xs := empty()`, which go refuses with `cannot infer T`.

WHAT IS EMITTED NOW. The IR fn carries `type_params` when the author wrote a
list, and the emitter uses it ahead of the heuristic. A return-only type
parameter is bound from the type the result flows into (a `let`/`var`
annotation, which the frontend pins onto the call as `"expected"`, a
reassigned binding, a parameter, a return) and written at the call,
`empty[int64]()`, since Go infers a type argument from the arguments only.
With no expected type the call is left as it was and go still refuses it.

Each run test executes the program's `test` blocks on the py tier and on go
and asserts both pass. They need `go` and skip without it; a skip is not a
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

_spec = importlib.util.spec_from_file_location("revl_go_emit_2545", HERE / "emit.py")
go_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(go_emit)

# Explicit lists the heuristic would miss (`Elem`, `Key`, `Val`), one of them
# with two names, called with `Int`, `Str` and a record.
EXPLICIT = """\
type Pt = { x: Int, y: Int }

fn first[Elem](xs: List[Elem]) -> Elem { return xs[0] }
fn pick[Elem](c: Bool, a: Elem, b: Elem) -> Elem {
  if (c) { return a }
  return b
}
fn second[Key, Val](k: Key, v: Val) -> Val { return v }
fn same[Elem](a: Elem, b: Elem) -> Bool { return a == b }

pub fn a() -> Int { return first([4, 5]) }
pub fn b() -> Str { return pick(false, "x", "y") }
pub fn c() -> Int { let p: Pt = { x: 3, y: 4 }; return first([p]).y + second("k", 1) }
pub fn d() -> Bool { return same("p", "p") && !same(1, 2) }

test "explicit" {
  assert a() == 4
  assert b() == "y"
  assert c() == 5
  assert d()
}
"""

# A return-only type parameter bound from each position the result can flow
# into: a `let` and a `var` annotation, a reassignment, a parameter, a
# return, and a return spelled with the caller's own type parameter.
RETURN_ONLY = """\
fn empty[T]() -> List[T] { return [] }
fn none_of[T]() -> Opt[T] { return None }
fn conv[A, B](a: A) -> List[B] { return [] }
fn count(xs: List[Int]) -> Int { return xs.length() }
fn fresh() -> List[Str] { return empty() }
fn relay[U]() -> List[U] { return empty() }

pub fn a() -> Int {
  let xs: List[Int] = empty()
  var ys: List[Str] = empty()
  ys = ys.push("y")
  ys = empty()
  return xs.length() + ys.length()
}
pub fn b() -> Int { return count(empty()) + fresh().length() }
pub fn c() -> Int {
  let o: Opt[Int] = none_of()
  let zs: List[Bool] = relay()
  let cs: List[Str] = conv(1)
  return match o {
    Some(v) => v,
    None => 7 + zs.length() + cs.length(),
  }
}

test "return_only" {
  assert a() == 0
  assert b() == 0
  assert c() == 7
}
"""


def _ir(src: str, name: str):
    return compile_source(src, name)


def test_an_explicit_list_is_the_go_type_parameter_list():
    src = go_emit.emit(_ir(EXPLICIT, "explicit_2545.rvl"))
    assert "func first[Elem any](xs []Elem) Elem {" in src, src
    assert "func second[Key any, Val any](k Key, v Val) Val {" in src, src
    assert "func first(xs []Elem)" not in src, src


def test_a_return_only_type_parameter_is_written_at_the_call():
    src = go_emit.emit(_ir(RETURN_ONLY, "return_only_2545.rvl"))
    assert "xs := empty[int64]()" in src, src
    assert "ys = empty[string]()" in src, src
    assert "count(empty[int64]())" in src, src
    assert "return empty[string]()" in src, src
    assert "return empty[U]()" in src, src
    assert "none_of[int64]()" in src, src
    # `A` is bound from the argument, so only the prefix up to `B` is written
    assert "conv[int64, string](int64(1))" in src, src


def test_a_return_only_type_parameter_with_no_expected_type_is_left_alone():
    src = go_emit.emit(_ir(
        "fn empty[T]() -> List[T] { return [] }\n"
        "pub fn g() -> Int { let xs = empty() return xs.length() }\n",
        "unbound_2545.rvl"))
    assert "xs := empty()" in src, src


def _run_like_py(src: str, name: str, passed: str):
    py_status, py_message = RUNNERS["py"](_ir(src, name))
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](_ir(src, name))
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert passed in message and passed in py_message, (py_message, message)


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_builds_and_runs_explicit_type_parameter_lists_like_py():
    _run_like_py(EXPLICIT, "explicit_2545.rvl", "1 test(s) passed")


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_builds_and_runs_return_only_type_parameters_like_py():
    _run_like_py(RETURN_ONLY, "return_only_2545.rvl", "1 test(s) passed")
