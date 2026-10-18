"""A generic function builds and runs on the rust tier (issue #2495).

WHAT WAS WRONG. `fn id[T](x: T) -> T` passed the checker and ran on py, but
the rust emitter mapped the unknown type name `T` to the erased `Value`
(`fn id(x: Value) -> Value`), and `id(1)` in an `i64` position was E0308.

WHAT IS EMITTED NOW. The IR carries the author's spelling (a parameter typed
`T`, no `[T]` list), so the emitter applies the checker's implicit rule: a
single-uppercase name no `type` declares is a type parameter. The function
declares it with the bounds its body needs: `Clone` always (the body
renderer clones a reused local or a by-value argument, and every type a revl
value lowers to is `Clone`), plus `PartialEq` when the body compares a `T`
with `==`/`!=`. Inside the body `T` is itself; at a call the type parameters
are bound from the arguments, so a slot typed `T` is never the erased
`Value` (no boxing) and a `let` of the result is typed by the instantiation.

The run test executes the program's `test` blocks on the py tier and on rust
and asserts both pass the same assertions. It needs `cargo` and skips
without it; a skip is not a pass, CI's cross-tier step names rust in
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

_spec = importlib.util.spec_from_file_location("revl_rust_emit_2495", HERE / "emit.py")
rust_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rust_emit)

# The same program as backends/go/test_generic_fn_go_2495.py.
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


def test_a_generic_function_is_emitted_with_type_parameters():
    src = rust_emit.emit(_ir())
    assert "fn id<T: Clone>(x: T) -> T {" in src, src
    assert "fn same<T: Clone + PartialEq>(a: T, b: T) -> bool {" in src, src
    assert "fn pair<A: Clone, B: Clone>(a: A, b: B) -> Vec<B> {" in src, src
    assert "fn or_else<T: Clone>(o: Option<T>, d: T) -> T {" in src, src
    assert "fn un<T: Clone>(m: Mb<T>, d: T) -> T {" in src, src
    # a `T` slot is the instantiation, never the erased `Value`
    assert "return id(1i64);" in src, src
    assert 'return id(String::from("a"));' in src, src
    assert "Value::new" not in src, src
    assert "fn id(x: Value)" not in src, src


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_rust_runs_the_generic_function_program_like_py():
    ir = _ir()
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["rust"](_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "2 test(s) passed" in message and "2 test(s) passed" in py_message, (
        py_message, message)
