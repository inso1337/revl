"""Roadmap item 407: a `call` inside a lifecycle-test `assert`.

A witnessed `call` is the effectful driver of a lifecycle composition (it is
recorded for teardown and residue checking), while an `assert` is a pure
observation over the test's `let` bindings. Before this change, writing the
call inline in the assertion (`assert call key.op() == n`) failed with an
opaque "expected a lifecycle statement, found ..." because `pure_expr` read
`call` as a bare variable and the parser desynchronised. It now redirects to
the one-line `let`-hoist, naming why the call cannot live in the assert.

Pure module-`fn` calls in an assert were never restricted and stay allowed —
they carry no witnessed effect, so there is nothing to hoist.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

_COMPONENT = """
service Counter {
  fn count() -> Int
}

component Tally provides counter: Counter {
  let store = effect Map.new() undo store.drop()
  provide counter {
    fn count() = store.size()
  }
}
"""


def _compile(src: str):
    return compile_source(_COMPONENT + src, "assertcall407.rvl")


def test_witnessed_call_in_assert_redirects_to_let_hoist():
    src = """
lifecycle test "inline witnessed call" {
  load Tally
  assert call counter.count() == 0
  unload Tally
  assert no_residue
}
"""
    with pytest.raises(RevlError) as excinfo:
        _compile(src)
    message = str(excinfo.value)
    # names why: the call is an effect, the assert is pure
    assert "witnessed `call`" in message
    assert "inside an `assert`" in message
    # actionable: points at the concrete let-hoist for THIS call
    assert "let result = call counter.count(...)" in message
    assert "assert result == ..." in message
    # the old opaque failure is gone
    assert "expected a lifecycle statement" not in message


def test_hoisted_witnessed_call_still_compiles():
    """The redirect target — hoist to a `let`, assert over the binding."""
    src = """
lifecycle test "hoisted witnessed call" {
  load Tally
  let seen = call counter.count()
  assert seen == 0
  unload Tally
  assert no_residue
}
"""
    _compile(src)  # no raise


def test_pure_fn_call_in_lifecycle_assert_is_allowed():
    """A pure module `fn` call carries no witnessed effect — never restricted."""
    src = """
pub fn dbl(a: Int) -> Int { return a + a }

lifecycle test "pure fn call in assert" {
  load Tally
  let seen = call counter.count()
  assert dbl(seen) == 0
  unload Tally
  assert no_residue
}
"""
    _compile(src)  # no raise


def test_assert_without_a_call_is_unchanged():
    """Additivity: a plain boolean assert over a binding is untouched."""
    src = """
lifecycle test "plain assert" {
  load Tally
  let seen = call counter.count()
  assert seen == 0
  unload Tally
  assert no_residue
}
"""
    _compile(src)  # no raise


def test_pure_fn_call_in_plain_test_block_assert_still_works():
    """Calls in a plain `test` block assert were always fine — regression guard."""
    src = """
pub fn add(a: Int, b: Int) -> Int { return a + b }
test "call in assert" { assert add(2, 2) == 4 }
"""
    compile_source(src, "assertcall407_plain.rvl")  # no raise
