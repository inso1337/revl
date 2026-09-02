"""A call-position bare name must resolve to exactly one thing (item 445
follow-up).

`f(x)` lowers to a `var` callee named `f` whether `f` is a local binding or a
module `fn`, and the consumers of that node disagreed about which it was: the
python emitter's template inliner folded in the MODULE FN's body (dropping the
local's value outright), a TypeScript/Rust/Go local shadows the function in the
emitted code, and a Java local does not shadow a static method at all.

Two halves close it, and they cover disjoint ground:

  * the frontend refuses a body that both binds and calls a name a module
    callable VISIBLE TO THAT MODULE also has (`lower._refuse_callable_shadowing`);
  * the python inliner leaves alone any callee name the body binds, which is
    the cross-module residue the refusal cannot reach — resolution is
    module-scoped while the emitted namespace is flat over the whole merged
    program, so two modules that never `use` each other may each own a name.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_files, compile_source  # noqa: E402

emit_py = backend_emitter("python")


# ------------------------------------------------------------- the refusal

def test_a_let_shadowing_a_called_module_fn_is_refused():
    """The original reproducer. `helper` holds `g`, but the python emitter
    inlined `fn helper` and emitted `len([1, 2, 3])` — `g` was never called."""
    with pytest.raises(RevlError, match="`helper` is bound here and called"):
        compile_source(
            """
            fn helper(xs: List[Int]) -> Int { return xs.length() }
            fn shadowed(g: (List[Int]) -> Int) -> Int {
              let helper = g
              return helper([1, 2, 3])
            }
            """
        )


def test_a_parameter_shadowing_a_called_module_fn_is_refused():
    with pytest.raises(RevlError, match="`helper` is bound here and called"):
        compile_source(
            """
            fn helper(xs: List[Int]) -> Int { return xs.length() }
            fn shadowed(helper: (List[Int]) -> Int) -> Int {
              return helper([1, 2, 3])
            }
            """
        )


def test_an_arrow_parameter_shadowing_a_called_module_fn_is_refused():
    """An arrow parameter binds too, and the java/wasm tiers beta-reduce a
    called arrow — so the same name means the arrow there and the module fn on
    a tier that emits the call."""
    with pytest.raises(RevlError, match="`helper` is bound here and called"):
        compile_source(
            """
            fn helper(xs: List[Int]) -> Int { return xs.length() }
            fn shadowed(n: Int) -> Int {
              let f = (helper: (List[Int]) -> Int) => helper([1, 2, 3])
              return f((xs: List[Int]) => n)
            }
            """
        )


def test_a_binding_in_a_sibling_arm_is_refused_too():
    """Whole-body granularity: the emitters and the name-keyed analyses each
    work a function at a time, and a reader of `helper()` in the tail still has
    to look up the arm to know what it names."""
    with pytest.raises(RevlError, match="`helper` is bound here and called"):
        compile_source(
            """
            fn helper() -> Int { return 1 }
            fn shadowed(c: Bool) -> Int {
              if (c) { let helper = 2  return helper }
              return helper()
            }
            """
        )


# ------------------------------------------------------- what stays accepted

def test_a_binding_that_only_shares_a_fn_name_is_accepted():
    """Only the CALL position is ambiguous. This shape is committed today —
    backends/wasm/golden/functions.revl declares `fn name(row: Row)` beside
    `fn make_row(id: Int, name: Str)` — and refusing it would be strictness for
    its own sake."""
    ir = compile_source(
        """
        type Row = { id: Int, name: Str }
        fn name(row: Row) -> Str { return row.name }
        fn make_row(id: Int, name: Str) -> Row { return { id: id, name: name } }
        """
    )
    assert ir is not None


def test_a_renamed_binding_still_calls_both():
    ir = compile_source(
        """
        fn helper(xs: List[Int]) -> Int { return xs.length() }
        fn ok(g: (List[Int]) -> Int) -> Int {
          let h = g
          return h([1, 2, 3]) + helper([4])
        }
        """
    )
    assert ir is not None


# ------------------------------------------- the cross-module emitter residue

_OTHER_MODULE = "fn helper(xs: List[Int]) -> Int { return xs.length() }\n"
_USER = """
fn shadowed(g: (List[Int]) -> Int) -> Int {
  let helper = g
  return helper([1, 2, 3])
}
"""


def test_a_local_never_resolves_to_an_unimported_module_fn(tmp_path):
    """Resolution is per-module (`fn_scopes`) but the emitted namespace is flat
    over the merged program, so the refusal above cannot see this case: neither
    file `use`s the other, and both spellings are correct revl. The python
    inliner used to key on the bare name alone and folded `fn helper` in
    anyway; it must call the local.

    This is not hypothetical — selfhost/lexer.rvl declares `fn step` while
    selfhost/emit_py.rvl binds a local `step`.
    """
    other = tmp_path / "other.rvl"
    other.write_text(_OTHER_MODULE)
    user = tmp_path / "user.rvl"
    user.write_text(_USER)

    src = emit_py.emit(compile_files([str(other), str(user)]))
    body = src.split("def shadowed(")[1]
    assert "return helper([1, 2, 3])" in body, (
        "`helper` names the local `g` here, not the module fn of that name")
    assert "return len([1, 2, 3])" not in body


def test_the_inliner_still_folds_a_small_pure_helper():
    """The guard is a scope check, not a switch-off: item 231a's whole point is
    that `is_digit(c)` becomes an inline comparison on the py tier."""
    src = emit_py.emit(compile_source(
        """
        fn is_digit(c: Int) -> Bool { return c >= 48 && c <= 57 }
        fn count(cs: List[Int]) -> Int {
          var n = 0
          for (c of cs) { if (is_digit(c)) { n = n + 1 } }
          return n
        }
        """
    ))
    assert "is_digit(c)" not in src.split("def count(")[1]
    assert "48" in src.split("def count(")[1]
