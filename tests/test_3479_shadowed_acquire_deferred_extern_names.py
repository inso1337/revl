"""Issue #3479: a local, parameter or loop variable that shadows an `acquire`
or `deferred` extern name is not refused as a read of the extern.

`_refuse_effect_position_bound_externs_in_fn_body` matched `acquire` and
`deferred` externs by bare name without consulting the binding scope (the
emission-value check next to it already did, from #3354/#2985), so
`fn f(open_h: Int)` with an `acquire` extern `open_h` was refused G4 as if it
read the extern. Test bodies had the further defect that they were walked
statement-by-statement, so a `let ping = 5` in one statement never entered the
bound set for later statements.

Now every check in the walk — the deferred value reference, the `acquire` and
`deferred` call checks — tests `name not in bound`, and a `test` body is
walked as one block so its `let`s bind to the end. Only a read that resolves
to the extern is refused.

Checked here:
  * a parameter shadowing an `acquire`-with-`undo` extern compiles;
  * a parameter shadowing a `deferred` extern compiles, and a `let` in a
    `test` body shadowing it compiles;
  * a `for` binder and a match payload shadowing a `deferred` name compile;
  * the real reads are still refused G4: a direct `acquire` call, a direct
    `deferred` call, and a `deferred` extern passed as a function value;
  * the refusal still names the fn/test body.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

ACQUIRE = (
    "type H = { fd: Int }\n"
    "extern acquire fn open_h(p: Str) -> H undo close_h(result) "
    "= @py { return { \"fd\": 1 } }\n"
    "fn close_h(r: H) { return }\n"
)

DEFERRED = (
    "extern emission[http] deferred fn ping(k: Str) -> Unit "
    "= @py { return }\n"
)


def _compiles(src: str) -> bool:
    try:
        compile_source(src, "i.rvl")
        return True
    except RevlError:
        return False


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "i.rvl")
    return excinfo.value


# ------------------------------------------------------------- shadowing is OK

def test_parameter_shadowing_an_acquire_extern_compiles():
    assert _compiles(ACQUIRE + "\nfn f(open_h: Int) -> Int { return open_h + 1 }\n")


def test_parameter_shadowing_a_deferred_extern_compiles():
    assert _compiles(DEFERRED + "\nfn f(ping: Int) -> Int { return ping + 1 }\n")


def test_let_in_a_test_body_shadowing_a_deferred_extern_compiles():
    assert _compiles(DEFERRED + "\ntest \"t\" { let ping = 5; assert ping + 1 == 6 }\n")


def test_for_binder_and_match_payload_shadowing_a_deferred_extern_compile():
    src = (DEFERRED
           + "\nfn f(xs: List[Int]) -> Int {\n"
           + "  var acc = 0\n"
           + "  for (ping of xs) { acc = acc + ping }\n"
           + "  return acc\n"
           + "}\n"
           + "fn g(o: Opt[Int]) -> Int {\n"
           + "  return match o {\n"
           + "    Some(ping) => ping,\n"
           + "    None => 0,\n"
           + "  }\n"
           + "}\n")
    assert _compiles(src)


def test_let_in_a_fn_body_shadowing_a_deferred_extern_compiles():
    src = (DEFERRED
           + "\nfn f() -> Int { let ping = 7; return ping }\n")
    assert _compiles(src)


# ------------------------------------------------- the real reads stay refused

def test_a_real_acquire_call_in_a_fn_body_is_still_refused():
    err = _refusal(ACQUIRE + "\nfn f() -> H { return open_h(\"x\") }\n")
    assert err.code == "G4"
    assert "`acquire` extern `open_h` cannot be called in the body of fn `f`" in err.message


def test_a_real_deferred_call_in_a_fn_body_is_still_refused():
    err = _refusal(DEFERRED + "\nfn f() { return ping(\"x\") }\n")
    assert err.code == "G4"
    assert "`deferred` emission extern `ping` cannot be called" in err.message


def test_a_deferred_extern_passed_as_a_value_is_still_refused():
    err = _refusal(DEFERRED + "\nfn f() -> Int { let g = ping; return 0 }\n")
    assert err.code == "G4"
    assert "passed as a function value" in err.message


def test_a_real_deferred_call_in_a_test_body_is_still_refused():
    err = _refusal(DEFERRED + "\ntest \"t\" { ping(\"x\") }\n")
    assert err.code == "G4"
    assert "body of test `t`" in err.message
