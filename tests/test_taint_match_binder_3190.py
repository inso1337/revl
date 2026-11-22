"""A pattern binder carries the taint of the value it unwraps (issue #3190).

A `match` arm's binder is the scrutinee's payload. The flow checker walked the
arm body with the binder unbound, so it read as CLEAN, and a sink called inside
the arm with the binder admitted an `Untrusted[T]` value into a `Trusted[T]`
shell sink (G9) and a `Secret[T]` value into a log (G-SECRET-FLOW). The same
value bound out of the match and passed to the sink afterwards was refused, so
the verdict depended only on where the call was written.

The destructuring `let` is the sibling binder form and had the same gap: its
names were never bound, so a pure fn that destructures its parameter and
returns a part laundered the argument's taint.
"""

from __future__ import annotations

import pytest

from revl import RevlError
from revl.compiler import compile_source

_UNTRUSTED = (
    'extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return "" }\n'
    'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int = @py { return 0 }\n'
)
_SECRET = (
    'extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] = @py { return a }\n'
    'extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n'
)


def _agent(body: str, extra: str = "") -> str:
    return (
        _UNTRUSTED + _SECRET + "type Rec = { a: Str, b: Str }\n"
        + "type Box = Wrap(Str) | Empty\n" + extra
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(u: Str) {\n" + body + "\n      return 0\n    }\n"
        + "  }\n}\n"
    )


def _code(src: str) -> str | None:
    try:
        compile_source(src, "m.rvl")
    except RevlError as err:
        return getattr(err, "code", None)
    return None


# -- the arm form: a sink called inside the arm with the binder -------------

@pytest.mark.parametrize("wrap,arms", [
    ("Some(p)", "Some(v) => emit run(v), None => 0"),
    ("Ok(p)", "Ok(v) => emit run(v), Err(e) => 0"),
    ("Wrap(p)", "Wrap(v) => emit run(v), Empty => 0"),
])
def test_untrusted_binder_reaching_a_shell_sink_is_refused(wrap, arms):
    src = _agent("      let p = emit fetch(u)\n"
                 f"      let o = {wrap}\n"
                 f"      let r = match o {{ {arms} }}")
    assert _code(src) == "G9"


@pytest.mark.parametrize("wrap,arms", [
    ("Some(t)", "Some(v) => emit logit(v), None => 0"),
    ("Ok(t)", "Ok(v) => emit logit(v), Err(e) => 0"),
    ("Wrap(t)", "Wrap(v) => emit logit(v), Empty => 0"),
])
def test_secret_binder_reaching_a_log_is_refused(wrap, arms):
    src = _agent("      let t = emit charge(u)\n"
                 f"      let o = {wrap}\n"
                 f"      let r = match o {{ {arms} }}")
    assert _code(src) == "G-SECRET-FLOW"


def test_the_err_arm_binder_carries_the_scrutinee_taint_too():
    """Which arm runs is not static, so every binder carries the scrutinee."""
    src = _agent("      let p = emit fetch(u)\n"
                 "      let o = Ok(p)\n"
                 "      let r = match o { Ok(v) => 0, Err(e) => emit run(e) }")
    assert _code(src) == "G9"


def test_a_nested_match_binder_is_refused():
    src = _agent("      let p = emit fetch(u)\n"
                 "      let o = Some(Some(p))\n"
                 "      let r = match o { Some(w) => match w { Some(v) => emit run(v),"
                 " None => 0 }, None => 0 }")
    assert _code(src) == "G9"


def test_a_clean_scrutinee_still_compiles():
    src = _agent('      let o = Some("ls")\n'
                 "      let r = match o { Some(v) => emit run(v), None => 0 }")
    assert _code(src) is None


def test_a_binder_does_not_leak_past_its_arm():
    """The binder is scoped to its arm: a clean outer name is not overwritten."""
    src = _agent("      let p = emit fetch(u)\n"
                 '      let c = "ls"\n'
                 "      let o = Some(p)\n"
                 "      let r = match o { Some(c) => 0, None => 0 }\n"
                 "      emit run(c)")
    assert _code(src) is None


# -- field granularity: a record payload keeps its per-field taint ----------

def test_a_clean_field_of_a_tainted_record_payload_stays_clean():
    src = _agent("      let p = emit fetch(u)\n"
                 '      let rec = { a: p, b: "ls" }\n'
                 "      let o = Some(rec)\n"
                 "      let r = match o { Some(v) => emit run(v.b), None => 0 }")
    assert _code(src) is None


def test_the_tainted_field_of_a_record_payload_is_refused():
    src = _agent("      let p = emit fetch(u)\n"
                 '      let rec = { a: p, b: "ls" }\n'
                 "      let o = Some(rec)\n"
                 "      let r = match o { Some(v) => emit run(v.a), None => 0 }")
    assert _code(src) == "G9"


# -- the destructuring `let` sibling ------------------------------------------

_PICK_RECORD = "fn pick(r: Rec) -> Str {\n  let { a, b } = r\n  return a\n}\n"
_PICK_LIST = "fn pick(xs: List[Str]) -> Str {\n  let [h, ...t] = xs\n  return h\n}\n"


def test_a_record_destructuring_carries_the_field_taint():
    src = _agent("      let p = emit fetch(u)\n"
                 '      let rec = { a: p, b: "ls" }\n'
                 "      emit run(pick(rec))", extra=_PICK_RECORD)
    assert _code(src) == "G9"


def test_a_list_destructuring_carries_the_element_taint():
    src = _agent("      let p = emit fetch(u)\n"
                 "      emit run(pick([p]))", extra=_PICK_LIST)
    assert _code(src) == "G9"


def test_a_destructuring_of_clean_values_still_compiles():
    src = _agent('      let rec = { a: "x", b: "ls" }\n'
                 "      emit run(pick(rec))", extra=_PICK_RECORD)
    assert _code(src) is None


def test_a_secret_through_a_destructuring_is_refused():
    src = _agent("      let t = emit charge(u)\n"
                 '      let rec = { a: t, b: "ls" }\n'
                 "      emit logit(pick(rec))", extra=_PICK_RECORD)
    assert _code(src) == "G-SECRET-FLOW"


# -- `try`: it desugars to a match, so its binder carries the taint too ------

_FIRST = ("fn first(r: Result[Str, Str]) -> Result[Str, Str] {\n"
          "  let v = try r\n  return Ok(v)\n}\n")


def test_a_try_unwrap_carries_the_taint():
    src = _agent("      let p = emit fetch(u)\n"
                 "      let r = match first(Ok(p)) { Ok(v) => emit run(v),"
                 " Err(e) => 0 }", extra=_FIRST)
    assert _code(src) == "G9"


def test_a_try_unwrap_of_a_clean_value_still_compiles():
    src = _agent('      let r = match first(Ok("ls")) { Ok(v) => emit run(v),'
                 " Err(e) => 0 }", extra=_FIRST)
    assert _code(src) is None
