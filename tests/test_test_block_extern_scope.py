"""A `test` block sees the same module-level callable scope a `fn` body does
(roadmap item 182).

The bug: `_lower_tests` assembled its callable scope as
`_HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | {fn names}` — it left OUT the module
`extern`s that a `fn` body (`_lower_fns`) and a `prop test` body
(`_lower_prop_tests`) both see. So `test "t" { assert mangle("x") == "x" }`
refused with G1 `` `mangle` is not declared in this function `` while the
identical call inside a `fn` compiled. That forced every extern-backed helper
to be wrapped in a `fn` purely to be unit-tested in-file — exactly the shape the
self-host emitter stages (items 174/191/198/199) hit when self-checking.

The fix widens the `test`-block scope to the module `extern`s too — and no
further: a genuinely-undeclared name still refuses with the same diagnostic.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def test_test_block_can_call_an_in_module_extern_pure_fn():
    """The exact shape the self-host stages needed: an `extern pure fn` declared
    in the module is callable from a `test` body, just as from a `fn` body."""
    src = '''
    extern pure fn mangle(x: Str) -> Str = @py { return x }
    test "mangles" { assert mangle("x") == "x" }
    '''
    ir = compile_source(src, "t.rvl")
    names = [t["name"] for t in ir["tests"]]
    assert names == ["mangles"]
    assert ir["tests"][0]["body"], "the assert body lowered"


def test_test_block_extern_scope_matches_the_fn_body_scope():
    """The same `extern` call compiles identically whether it is reached from a
    `test` block or from a `fn` — the point of the fix is parity, not a new
    capability granted only to tests."""
    common = 'extern pure fn mangle(x: Str) -> Str = @py { return x }\n'
    via_fn = compile_source(
        common + 'fn wrap(x: Str) -> Str { return mangle(x) }\n', "t.rvl")
    via_test = compile_source(
        common + 'test "t" { assert mangle("x") == "x" }\n', "t.rvl")
    # both compile; the extern is present in each program's boundary
    assert {e["name"] for e in via_fn["externs"]} == {"mangle"}
    assert {e["name"] for e in via_test["externs"]} == {"mangle"}


def test_test_block_can_still_call_a_module_fn():
    """The pre-existing guarantee — a module `fn` is visible in a `test` — is
    untouched by the widening."""
    src = '''
    fn f(x: Str) -> Str { return x }
    test "calls f" { assert f("y") == "y" }
    '''
    ir = compile_source(src, "t.rvl")
    assert [t["name"] for t in ir["tests"]] == ["calls f"]


def test_test_block_still_refuses_a_genuinely_undeclared_name():
    """The widening must not weaken the checker: a name that is neither a module
    `fn`, an `extern`, nor a host callable still refuses — with the same G1
    diagnostic a `fn` body raises."""
    src = 'test "t" { assert nope("z") == "z" }'
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert "`nope` is not declared in this function" in str(excinfo.value)


def test_lifecycle_test_block_also_sees_module_externs():
    """A `lifecycle test` shares the same assembled `callables` set, so it gains
    extern visibility too (the fix is at the shared assembly point)."""
    src = '''
    extern pure fn mangle(x: Str) -> Str = @py { return x }
    lifecycle test "lc" {
      assert mangle("x") == "x"
    }
    '''
    ir = compile_source(src, "t.rvl")
    assert [t["name"] for t in ir["tests"]] == ["lc"]
