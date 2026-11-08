"""A qualifier inside a container on a declared return mints its source
(issue #3192).

An extern declared `-> Result[Secret[Str], Str]` or `-> Opt[Secret[Str]]` got
the IR redaction stamp but minted no `confidential` origin, so the value it
returned was clean to every G-SECRET-FLOW rule and reached a log once
unwrapped. The `Untrusted[T]` half was the same: `-> Result[Untrusted[Str],
Str]` minted no G9 origin. The source is now read wherever the qualifier sits
in a container, and the `match` that unwraps the value carries it to the
payload (the binder half is issue #3190).
"""

from __future__ import annotations

import pytest

from revl import RevlError
from revl.compiler import compile_source

_SINKS = (
    'extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n'
    'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int = @py { return 0 }\n'
)


def _agent(source_decl: str, body: str, requires: str = "") -> str:
    return (
        source_decl + "\n" + _SINKS
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent " + requires + "provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(u: Str) {\n" + body + "\n      return 0\n    }\n"
        + "  }\n}\n"
    )


def _code(src: str) -> str | None:
    try:
        compile_source(src, "s.rvl")
    except RevlError as err:
        return getattr(err, "code", None)
    return None


_RESULT_UNWRAP = "match r { Ok(v) => v, Err(e) => \"\" }"
_OPT_UNWRAP = "match r { Some(v) => v, None => \"\" }"


@pytest.mark.parametrize("returns,unwrap", [
    ("Result[Secret[Str], Str]", _RESULT_UNWRAP),
    ("Opt[Secret[Str]]", _OPT_UNWRAP),
    ("Result[Opt[Secret[Str]], Str]",
     "match r { Ok(o) => match o { Some(v) => v, None => \"\" }, Err(e) => \"\" }"),
])
def test_a_nested_secret_return_reaching_a_log_is_refused(returns, unwrap):
    src = _agent(
        f"extern emission[payment.charge] fn charge(a: Str) -> {returns} "
        "= @py { return None }",
        f"      let r = emit charge(u)\n      let t = {unwrap}\n"
        "      emit logit(t)")
    assert _code(src) == "G-SECRET-FLOW"


def test_a_nested_secret_return_logged_inside_the_arm_is_refused():
    src = _agent(
        "extern emission[payment.charge] fn charge(a: Str) -> "
        "Result[Secret[Str], Str] = @py { return None }",
        "      let r = emit charge(u)\n"
        "      let x = match r { Ok(v) => emit logit(v), Err(e) => 0 }")
    assert _code(src) == "G-SECRET-FLOW"


def test_a_list_of_secrets_reaching_a_log_is_refused():
    src = _agent(
        "extern emission[payment.charge] fn charge(a: Str) -> "
        "List[Secret[Str]] = @py { return [] }",
        "      let r = emit charge(u)\n"
        "      emit logit(r[0])")
    assert _code(src) == "G-SECRET-FLOW"


@pytest.mark.parametrize("returns,unwrap", [
    ("Result[Untrusted[Str], Str]", _RESULT_UNWRAP),
    ("Opt[Untrusted[Str]]", _OPT_UNWRAP),
])
def test_a_nested_untrusted_return_reaching_a_shell_is_refused(returns, unwrap):
    src = _agent(
        f"extern emission[web] fn fetch(url: Str) -> {returns} "
        "= @py { return None }",
        f"      let r = emit fetch(u)\n      let t = {unwrap}\n"
        "      emit run(t)")
    assert _code(src) == "G9"


@pytest.mark.parametrize("returns,unwrap", [
    ("Result[Str, Str]", _RESULT_UNWRAP),
    ("Opt[Str]", _OPT_UNWRAP),
])
def test_an_unqualified_container_return_still_compiles(returns, unwrap):
    src = _agent(
        f"extern emission[payment.charge] fn charge(a: Str) -> {returns} "
        "= @py { return None }",
        f"      let r = emit charge(u)\n      let t = {unwrap}\n"
        "      emit logit(t)")
    assert _code(src) is None


def test_a_nested_secret_service_return_from_another_unit_is_refused():
    """The interface-only twin of the extern mint: a declared operation the
    unit does not implement mints from a nested qualifier too."""
    src = _agent(
        "service Vault { emission[vault.mint] fn get() -> "
        "Result[Secret[Str], Str] }",
        "      let r = emit v.get()\n"
        f"      let t = {_RESULT_UNWRAP}\n"
        "      emit logit(t)", requires="requires v: Vault ")
    assert _code(src) == "G-SECRET-FLOW"
