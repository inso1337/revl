"""An unpinned payload-less case on the rust tier (issue #2497).

WHAT WAS WRONG. `let x = None` with no later use that fixes the type was
E0282 (`type annotations needed for Option<_>`) where py and go run the
program (go gives it `RevlOpt[any]`). An unpinned user generic case (`let
x = Nn` with `type Mb[T] = Jj(T) | Nn`) was E0282 too, where go refuses it
by name.

WHAT IS EMITTED NOW. A `let` whose value is a bare `None` and whose name
the body never reads again is annotated `Option<Value>`, the erased type go
gives it. A `let` of a payload-less case of a generic variant that nothing
reads again is refused by name, with go's wording. A `let` the body does
read is left to rustc, which infers the type from that use, exactly as
before.

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

_spec = importlib.util.spec_from_file_location("revl_rust_emit_2497", HERE / "emit.py")
rust_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rust_emit)

# A dead `None` let beside a live one (`y` flows into `f`, which pins it),
# and a nested case list that py and rust run (the go half of the issue).
PROGRAM = """\
type Mb[T] = Jj(T) | Nn

pub fn f(o: Opt[Int]) -> Int {
  return match o {
    Some(v) => v,
    None => 7,
  }
}

fn un(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x,
    Nn => 0,
  }
}

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
  let x = None
  let y = None
  return f(y) + 1
}

pub fn live() -> Int {
  let x = Nn
  return un(x)
}

pub fn nested() -> Int {
  let xs = [Jj(Nn), Jj(Jj(1)), Nn]
  var acc = 0
  for (m of xs) {
    acc = acc + depth(m)
  }
  return acc + xs.length()
}

test "h" {
  assert h() == 8
  assert live() == 0
  assert nested() == 1
}
"""

UNPINNED = """\
type Mb[T] = Jj(T) | Nn
pub fn h() -> Int {
  let x = Nn
  return 1
}
"""


def _ir():
    return compile_source(PROGRAM, "unpinned_case_2497.rvl")


def test_a_dead_none_let_is_typed_and_a_live_one_is_left_to_rustc():
    src = rust_emit.emit(_ir())
    assert "let x: Option<Value> = None" in src, src
    assert "let y = None" in src and "let y: " not in src, src
    assert "let x = Mb::Nn;" in src, src


def test_an_unpinned_generic_case_is_refused_by_name():
    with pytest.raises(rust_emit.EmitError) as err:
        rust_emit.emit(compile_source(UNPINNED, "unpinned_2497.rvl"))
    assert "variant case `Nn`: type 'Mb[T]' has no type argument for T" in str(
        err.value), str(err.value)


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_rust_runs_the_unpinned_case_program_like_py():
    ir = _ir()
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["rust"](_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "1 test(s) passed" in message and "1 test(s) passed" in py_message, (
        py_message, message)
