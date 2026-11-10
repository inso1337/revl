"""The `compensate` slot of an `emit` step is a taint crossing (issue #3180).

The statement walker read only the forward call of an `emit` step and
returned, so the call in its `compensate` slot was never visited. That call
runs on an abort, as a bare host call, with the values it captured, so a
value the checker refuses at a sink when it is passed directly was admitted
when the same call was written as the compensation of another emission: an
`Untrusted[T]` value into a `Trusted[T]` shell sink (G9), a `Secret[T]` value
into a log (G-SECRET-FLOW), the bound key into a log (G-SECRET), and the
policy-gated tier (`web-taint may not reach net`) saw no edge.
"""

from __future__ import annotations

import pytest

from revl import RevlError
from revl.audit_diff import audit_report
from revl.compiler import compile_source
from revl.policy import evaluate, parse_policy

_PRELUDE = (
    'extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return "" }\n'
    'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int = @py { return 0 }\n'
    'extern emission[note] fn note(m: Str) -> Int = @py { return 0 }\n'
    'extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n'
    'extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] = @py { return a }\n'
)
_BOUND = (
    "secret openai_key for model.complete\n"
    "extern emission[model.complete] fn complete(p: Str) -> Str = @py { return p }\n"
)


def _agent(body: str, extra: str = "") -> str:
    return (
        _PRELUDE + extra
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(u: Str) {\n" + body + "\n      return 0\n    }\n"
        + "  }\n}\n"
    )


def _code(src: str) -> str | None:
    try:
        compile_source(src, "comp.rvl")
    except RevlError as err:
        return getattr(err, "code", None)
    return None


def test_untrusted_value_in_a_compensate_shell_call_is_refused():
    src = _agent("      let p = emit fetch(u)\n"
                 '      emit note("x") compensate run(p)')
    assert _code(src) == "G9"


def test_secret_value_in_a_compensate_log_call_is_refused():
    src = _agent("      let t = emit charge(u)\n"
                 '      emit note("x") compensate logit(t)')
    assert _code(src) == "G-SECRET-FLOW"


def test_bound_key_in_a_compensate_log_call_is_refused():
    src = _agent("      let t = emit complete(u)\n"
                 '      emit note("x") compensate logit(t)', extra=_BOUND)
    assert _code(src) == "G-SECRET"


def test_a_nested_call_in_the_compensate_slot_is_walked():
    src = _agent("      let p = emit fetch(u)\n"
                 '      emit note("x") compensate note(p.concat("")) + run(p)')
    assert _code(src) == "G9"


@pytest.mark.parametrize("comp", ['run("ls")', "note(u)", 'logit("done")'])
def test_a_clean_compensate_still_compiles(comp):
    src = _agent("      let p = emit fetch(u)\n"
                 f'      emit note("x") compensate {comp}')
    assert _code(src) is None


_SEND = (
    'extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return "" }\n'
    "service Sink { emission[net] fn send(body: Str) -> Int }\n"
    "service Ops { emission fn go(url: Str) }\n"
    "component Backend provides s: Sink { provide s { fn send(body) = 0 } }\n"
)


def _sender(step: str) -> str:
    return (
        _SEND
        + "component Agent requires s: Sink provides ops: Ops {\n"
        + "  provide ops {\n"
        + f"    fn go(url) {{ let page = emit fetch(url)  {step} }}\n"
        + "  }\n}\n"
    )


def _violations(step: str) -> list:
    audit = audit_report(compile_source(_sender(step), "comp_policy.rvl"))
    return evaluate(parse_policy("web-taint may not reach net", "p.rvl"), audit)


def test_the_policy_sees_a_web_tainted_send_in_the_compensate_slot():
    assert len(_violations("emit s.send(page)")) == 1   # the direct control
    assert len(_violations('emit s.send("y") compensate s.send(page)')) == 1


def test_a_clean_compensate_send_gives_no_violation():
    assert _violations('emit s.send("y") compensate s.send("z")') == []


_RECEIVERS = (
    'extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] = @py { return a }\n'
    'extern emission[note] fn note(m: Str) -> Int = @py { return 0 }\n'
    "service V { emission[vault.keep] fn keep(x: Secret[Str]) -> Int }\n"
    "service S { emission[net] fn send(b: Str) -> Str }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component Agent requires v: V, s: S provides ops: Ops {\n"
    "  provide ops {\n    fn go(u: Str) {\n"
    "      let t = emit charge(u)\n"
)


def _receiver_code(comp: str) -> str | None:
    return _code(_RECEIVERS + f'      emit note("x") compensate {comp}\n'
                 "      return 0\n    }\n  }\n}\n")


def test_an_inner_crossing_in_the_compensate_slot_gets_its_own_receiver_check():
    """The outer call declares a `Secret[T]` receiver; the inner `s.send`
    does not, so the confidential value it carries is refused there."""
    assert _receiver_code("v.keep(s.send(t))") == "G-SECRET-FLOW"


def test_a_declared_secret_receiver_in_the_compensate_slot_is_admitted():
    assert _receiver_code("v.keep(t)") is None


def test_a_confidential_value_to_an_undeclared_compensate_receiver_is_refused():
    assert _receiver_code("s.send(t)") == "G-SECRET-FLOW"
