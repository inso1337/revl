"""Issue #3448: `xs.push(x)` as a statement on a `var` list compiled and did
nothing — `push` returns a NEW list, so the discarded result left the list
empty. The shared compiler now refuses the statement and names the supported
spelling; since the refusal sits in the reference lowerer, every tier refuses
identically (the check runs before any emitter)."""

import pytest

from revl import RevlError, compile_source


def test_var_list_push_as_a_statement_is_refused_and_names_the_spelling():
    src = (
        "fn build() -> Int {\n"
        "  var xs: List[Int] = []\n"
        "  xs.push(1)\n"
        "  xs.push(2)\n"
        "  return xs.length()\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "push_stmt.rvl")
    msg = str(excinfo.value)
    assert "`xs.push(...)` as a statement has no effect" in msg
    assert "`push`" in msg
    assert "`xs = xs.push(...)`" in msg  # the supported spelling, named


def test_var_push_result_reassigned_is_admitted():
    src = (
        "fn build() -> Int {\n"
        "  var xs: List[Int] = []\n"
        "  xs = xs.push(1)\n"
        "  return xs.length()\n"
        "}\n"
        "pub fn out() -> Int { return build() }\n"
    )
    ir = compile_source(src, "push_reassign.rvl")
    assert ir["functions"] or ir["components"]


def test_push_nested_in_an_expression_is_admitted():
    # a push whose result flows into a call is not a discarded statement
    src = (
        "fn total(xs: List[Int]) -> Int { return xs.push(9).length() }\n"
        "pub fn out() -> Int { return total([1, 2]) }\n"
    )
    compile_source(src, "push_expr.rvl")


def test_push_as_a_statement_on_a_non_var_list_is_not_this_refusal():
    # a `let` binding is immutable; the #3448 refusal targets `var` only, so a
    # statement push on a `let` still compiles (and is still a no-op) — pinned
    # here so the refusal's scope stays deliberate, not accidental.
    src = (
        "fn f() -> Int {\n"
        "  let xs: List[Int] = [1]\n"
        "  xs.push(2)\n"
        "  return xs.length()\n"
        "}\n"
        "pub fn out() -> Int { return f() }\n"
    )
    compile_source(src, "push_let.rvl")
