"""`Str +` of two call results builds on the rust tier (issue #2496).

WHAT WAS WRONG. `f("a") + f("b")`, both calls returning `Str`, was emitted
as a native `+`: Rust's `String + String` is E0308 (the right operand must
be a `&str`). A `Str` local or literal on either side already went through
`format!`, because `_v3_is_str` knew those shapes; a call did not count.

WHAT IS EMITTED NOW. An operand whose type the emitter can infer as `Str`
(a free-function or extern call's declared return, a generic call whose
type parameter the arguments bind to `Str`, a field read, an index) makes
the `+` a `format!("{}{}", ..)`, the same lowering a `Str` local gets.

The run test executes the program's `test` block on the py tier and on rust
and asserts both pass. It needs `cargo` and skips without it; a skip is not
a pass, CI's cross-tier step names rust in REVL_REQUIRE_TIERS.
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

_spec = importlib.util.spec_from_file_location("revl_rust_emit_2496", HERE / "emit.py")
rust_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rust_emit)

PROGRAM = """\
type Mb[T] = Jj(T) | Nn
type Rec = { tag: Str }

pub fn f(s: Str) -> Str { return s }
fn tag(n: Int) -> Str { return n.to_str() }
fn un[T](m: Mb[T], d: T) -> T {
  return match m {
    Jj(v) => v,
    Nn => d,
  }
}

pub fn g() -> Str { return f("a") + f("b") }
pub fn h() -> Str { return tag(1) + f("x") + tag(2) }
pub fn k(s: Str) -> Str { return s + f("!") }
pub fn m() -> Str { return un(Jj("j"), "n") + un(Nn, "n") }
pub fn r() -> Str { let rec: Rec = { tag: "t" }; return rec.tag + f("u") }

test "concat" {
  assert g() == "ab"
  assert h() == "1x2"
  assert k("q") == "q!"
  assert m() == "jn"
  assert r() == "tu"
}
"""


def _ir():
    return compile_source(PROGRAM, "str_concat_calls_2496.rvl")


def test_a_str_plus_of_two_call_results_is_a_format():
    src = rust_emit.emit(_ir())
    assert 'format!("{}{}", f(String::from("a")), f(String::from("b")))' in src, src
    assert ('format!("{}{}", format!("{}{}", tag(1i64), f(String::from("x"))), '
            'tag(2i64))') in src, src
    assert 'format!("{}{}", rec.tag, f(String::from("u")))' in src, src
    assert ('format!("{}{}", un(Mb::Jj(String::from("j")), String::from("n")), '
            'un(Mb::Nn, String::from("n")))') in src, src
    assert ") + " not in src.split("pub fn g()")[1], src


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_rust_runs_the_str_concat_program_like_py():
    ir = _ir()
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["rust"](_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "1 test(s) passed" in message and "1 test(s) passed" in py_message, (
        py_message, message)
