"""fn/test-body admission of teardown-bound externs (roadmap items 399, 400).

Found by the 379 break-design review. Both are the same shape as the item-243
rule-1 witnessed refusal: an extern carrying effect machinery that exists only
in effect position is refused when bare-called from a plain `fn`/`test` body,
because that machinery would be silently dropped there.

* item 399: a bare `acquire`-with-`undo` extern in a fn/test body would drop its
  declared `undo` teardown (no accumulator to register it), so the acquisition
  is silently irreversible.
* item 400: a bare `deferred` emission in a fn/test body fires immediately, once
  per loop iteration, bypassing the session-commit queue it is meant to feed.

Both stay ALLOWED in effect position (a component activation body, a provide
method), where the teardown/commit boundaries actually exist.
"""

import pytest

from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.errors import RevlError


# -- fixtures ---------------------------------------------------------------

_ACQ_UNDO = (
    "extern pure fn r_close(h: RHandle) = @py { return }\n"
    "extern acquire fn r_open(n: Int) -> RHandle undo r_close(result)"
    ' = @py { return "h" }\n'
)
_DEFERRED = "extern emission deferred fn notify(s: Str) = @py { return }\n"


def _compile(src: str) -> dict:
    return compile_source(src, "t.rvl")


# -- item 399: acquire-with-undo refused in a fn/test body ------------------

def test_399_acquire_undo_refused_in_fn_loop_body():
    with pytest.raises(RevlError) as ei:
        _compile(_ACQ_UNDO + (
            "fn f(n: Int) -> Int {\n"
            "  var i = 0\n"
            "  while (i < n) { let h = r_open(i)  i += 1 }\n"
            "  return i\n}"))
    msg = str(ei.value)
    assert "`acquire` extern `r_open` cannot be called in the body of fn `f`" in msg
    assert "would be dropped" in msg
    assert classify(ei.value)["code"] == "G4"
    assert classify(ei.value)["category"] == "acquire"


def test_399_acquire_undo_refused_in_test_body():
    with pytest.raises(RevlError) as ei:
        _compile(_ACQ_UNDO + 'test "opens" { let h = r_open(1) }\n')
    assert "cannot be called in the body of test `opens`" in str(ei.value)


def test_399_acquire_undo_allowed_in_activation_body():
    # the effect-position form: an `acquire` with a site `undo` in a component
    # activation body compiles clean (the teardown accumulator exists here).
    ir = _compile(_ACQ_UNDO + (
        "component Res {\n"
        "  let h = effect r_open(0) undo r_close(h)\n"
        "}\n"))
    assert any(c["name"] == "Res" for c in ir["components"])


def test_399_acquire_undo_allowed_in_provide_method():
    # item 399's claim here is ADMISSION: an `acquire`-with-`undo` extern in
    # effect position inside a provide method compiles, because the enclosing
    # activation's accumulator takes the entry. The site `undo` used to read
    # `r_close("h")` - a `Str` handed to an `(RHandle)` inverse, which compiled
    # only because a component-body call was never argument-checked (item 423).
    # It is now, so the slot names a declared callable at its declared
    # signature. The acquired handle itself still cannot be named at a seam
    # (only `spawn` may be bound in a provide-method body, and `result` is not
    # in scope in a site `undo`), which is item 420's design half.
    ir = _compile(_ACQ_UNDO + (
        "extern pure fn r_forget(tag: Str) = @py { return }\n"
        "service Res { fn take() }\n"
        "component R provides res: Res {\n"
        "  provide res {\n"
        "    fn take() { effect r_open(0) undo r_forget(\"h\") }\n"
        "  }\n"
        "}\n"))
    assert any(c["name"] == "R" for c in ir["components"])


# -- item 400: deferred emission refused in a fn/test body ------------------

def test_400_deferred_refused_in_fn_loop_body():
    with pytest.raises(RevlError) as ei:
        _compile(_DEFERRED + (
            "fn f(n: Int) -> Int {\n"
            "  var i = 0\n"
            "  while (i < n) { notify(\"x\")  i += 1 }\n"
            "  return i\n}"))
    msg = str(ei.value)
    assert "`deferred` emission extern `notify` cannot be called in the body of fn `f`" in msg
    assert "session commit" in msg
    assert classify(ei.value)["code"] == "G4"
    assert classify(ei.value)["category"] == "deferred"


def test_400_deferred_refused_in_test_body():
    with pytest.raises(RevlError) as ei:
        _compile(_DEFERRED + 'test "fires" { notify("x") }\n')
    assert "cannot be called in the body of test `fires`" in str(ei.value)


def test_400_deferred_allowed_in_provide_method():
    # the effect-position form: a deferred emission emitted from a provide
    # method compiles clean (the session-commit boundary exists here).
    ir = _compile(_DEFERRED + (
        "service Ops { emission fn q(to: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn q(to) { emit notify(to) }\n"
        "  }\n"
        "}\n"))
    assert any(c["name"] == "Agent" for c in ir["components"])


# -- narrowness: an extern with no dropped teardown/commit is unaffected -----

def test_plain_emission_still_compiles_in_a_fn_loop_body():
    # a non-deferred emission carries no session queue to bypass, so a bare call
    # from a fn body is unchanged (the 400 refusal is keyed on `deferred`, not on
    # the `emission` classification). An `acquire` with NO `undo` cannot be
    # declared at all (the mandatory-undo rule, lower.py: "must declare `undo`"),
    # so this plain emission is the observable "still compiles" narrowness probe.
    ir = _compile(
        "extern emission fn ping(s: Str) = @py { return }\n"
        "fn f(n: Int) -> Int {\n"
        "  var i = 0\n"
        "  while (i < n) { ping(\"x\")  i += 1 }\n"
        "  return i\n}")
    assert ir["functions"][0]["name"] == "f"


def test_pure_extern_still_compiles_in_a_fn_body():
    ir = _compile(
        "extern pure fn size(s: Str) -> Int = @py { return 1 }\n"
        "fn f(s: Str) -> Int { return size(s) }\n")
    assert ir["functions"][0]["name"] == "f"
