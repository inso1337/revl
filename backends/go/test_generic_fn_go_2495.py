"""A generic function builds and runs on the go tier (issue #2495).

WHAT WAS WRONG. `fn id[T](x: T) -> T` passed the checker and ran on py, but
the go emitter printed `func id(x T) T`, with no type parameter list, and
`go build` answered `undefined: T`.

WHAT IS EMITTED NOW. The IR carries the author's spelling (a parameter typed
`T`, no `[T]` list), so the emitter applies the checker's implicit rule: a
single-uppercase name no `type` declares is a type parameter. The function is
Go generics, `func id[T any](x T) T`; `any` is enough because `==` on a
non-primitive operand already goes through `revlEq`. At a call, the type
parameters are bound from the arguments (`wrap(7)` is a `List[Int]`, so a
`let` of it is typed), a `None` or a payload-less case flowing into a `T`
slot takes the bound instantiation (`RevlOpt[int64]{}`, `MbNn[string]{}`),
and an `Int` literal in a bare `T` slot is spelled `int64(1)`, because Go
infers `int` from an untyped constant and `id(1)` would not be an `int64`.

The run test executes the program's `test` blocks on the py tier and on go
and asserts both pass the same assertions. It needs `go` and skips without
it; a skip is not a pass, CI's cross-tier step names go in
REVL_REQUIRE_TIERS.
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

_spec = importlib.util.spec_from_file_location("revl_go_emit_2495", HERE / "emit.py")
go_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(go_emit)

# Identity, a branch returning either parameter, structural `==` on `T`, list
# and Opt and generic-variant results, two type parameters, and callers that
# instantiate each with `Int`, `Str` and a record.
PROGRAM = """\
type Pt = { x: Int, y: Int }
type Mb[T] = Jj(T) | Nn

fn id[T](x: T) -> T { return x }
fn pick[T](c: Bool, a: T, b: T) -> T {
  if (c) { return a }
  return b
}
fn same[T](a: T, b: T) -> Bool { return a == b }
fn wrap[T](x: T) -> List[T] { return [x] }
fn twice[T](x: T) -> List[T] { return [x, x] }
fn some_of[T](x: T) -> Opt[T] { return Some(x) }
fn or_else[T](o: Opt[T], d: T) -> T {
  return match o {
    Some(v) => v,
    None => d,
  }
}
fn jj_of[T](x: T) -> Mb[T] { return Jj(x) }
fn un[T](m: Mb[T], d: T) -> T {
  return match m {
    Jj(v) => v,
    Nn => d,
  }
}
fn pair[A, B](a: A, b: B) -> List[B] { return [b] }

pub fn a() -> Int { return id(1) }
pub fn b() -> Str { return id("a") }
pub fn c() -> Int { let p0: Pt = { x: 1, y: 2 }; let p = id(p0); return p.x }
pub fn d() -> Str { return pick(false, "x", "y") }
pub fn e() -> Bool {
  let p1: Pt = { x: 1, y: 2 }
  let p2: Pt = { x: 1, y: 2 }
  return same(2, 2) && !same("p", "q") && same(p1, p2)
}
pub fn f() -> Int { return wrap(3).length() + twice("z").length() }
pub fn g() -> Int { return or_else(some_of(9), 0) + or_else(None, 4) }
pub fn h() -> Str { let p = un(jj_of("j"), "n"); let q = un(Nn, "n"); return p + q }
pub fn i() -> Int { return pair("s", 5)[0] }
pub fn j() -> Int { let xs: List[Int] = wrap(7); return xs[0] + id(-1) }

test "id" {
  assert a() == 1
  assert b() == "a"
  assert c() == 1
}
test "more" {
  assert d() == "y"
  assert e()
  assert f() == 3
  assert g() == 13
  assert h() == "jn"
  assert i() == 5
  assert j() == 6
}
"""


def _ir():
    return compile_source(PROGRAM, "generic_fn_2495.rvl")


def test_a_generic_function_is_emitted_as_go_generics():
    src = go_emit.emit(_ir())
    assert "func id[T any](x T) T {" in src, src
    assert "func pair[A any, B any](a A, b B) []B {" in src, src
    assert "func or_else[T any](o RevlOpt[T], d T) T {" in src, src
    assert "func un[T any](m Mb[T], d T) T {" in src, src
    # a call binds the type parameters from its arguments
    assert "return id(int64(1))" in src, src
    assert 'return id("a")' in src, src
    assert "or_else(RevlOpt[int64]{}, int64(4))" in src, src
    assert 'un(MbNn[string]{}, "n")' in src, src
    assert "func id(x T)" not in src, src


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_the_generic_function_program_like_py():
    ir = _ir()
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "2 test(s) passed" in message and "2 test(s) passed" in py_message, (
        py_message, message)
