"""Roadmap item 380: string interpolation in a non-backtick string is
SILENT-WRONG, and it exposes a type-checker SOUNDNESS HOLE.

Two independent defects, both fixed here:

(A) The soundness hole (the serious one). `return <a function value>` where the
    declared return type is a non-function type (e.g. `Str`) type-checked as
    ANY declared return type: a bare reference to a top-level `fn` inferred to
    `None` (unknown), and `None` short-circuits every downstream compatibility
    check. `fn f() -> Str { return f }` compiled clean and silently returned
    the function value where a `Str` was declared. The fix types a bare `fn`
    reference as its function type so the mismatch is refused — while a return
    of a correctly function-typed value (item 92/342) still compiles.

(B) The interpolation diagnostic. `"hi ${name}"` compiled clean and emitted the
    LITERAL `${name}`; a leading `f"..."` parsed as `return f` (an identifier)
    plus a dead string. Both are now redirected to revl's backtick template.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _err(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    return str(excinfo.value)


# -- (A) the soundness hole -------------------------------------------------

def test_return_a_function_value_where_str_is_declared_is_refused():
    # THE soundness hole, exactly as the roadmap describes it. `f` in scope is
    # the function itself; returning it where `Str` is declared must be a
    # type-mismatch, not a silent miscompile to exit 0.
    err = _err("fn f() -> Str { return f }\n")
    assert "expects `Str`" in err and "() -> Str" in err


def test_return_a_named_fn_where_str_is_declared_is_refused():
    # the same hole with a distinct name and a parameter, so the message names
    # the real function type of the returned value.
    err = _err("fn inc(n: Int) -> Int { return n + 1 }\n"
               "fn g() -> Str { return inc }\n")
    assert "expects `Str`" in err and "(Int) -> Int" in err


def test_passing_a_wrong_signature_fn_by_name_is_refused():
    # the SAME root cause on the argument path: a bare fn name passed where a
    # function type is expected was never checked (its inferred type was None).
    err = _err("fn apply(g: (Int) -> Int, x: Int) -> Int { return g(x) }\n"
               "fn wrong(s: Str) -> Str { return s }\n"
               "fn demo(n: Int) -> Int { return apply(wrong, n) }\n")
    assert "(Int) -> Int" in err and "(Str) -> Str" in err


def test_returning_a_correctly_function_typed_fn_still_compiles():
    # item 92/342: a bare fn name whose type MATCHES the declared function
    # return type must still compile — the fix refuses mismatches, not returns.
    compile_source("fn inc(n: Int) -> Int { return n + 1 }\n"
                   "fn get_inc() -> (Int) -> Int { return inc }\n", "t.rvl")


def test_passing_a_correctly_typed_fn_by_name_still_compiles():
    compile_source("fn apply(g: (Int) -> Int, x: Int) -> Int { return g(x) }\n"
                   "fn inc(n: Int) -> Int { return n + 1 }\n"
                   "fn demo(n: Int) -> Int { return apply(inc, n) }\n", "t.rvl")
