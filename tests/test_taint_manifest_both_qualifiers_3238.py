"""A return that holds both qualifiers keeps both across a manifest (issue
#3238).

The services IR stamped one qualifier per operation return, so a manifest
service declaring `-> Result[Untrusted[Str], Secret[Str]]` stamped only
`Untrusted`. A unit compiled against that manifest could unwrap the `Err`
payload and log it although it is confidential. The IR now carries the full
set beside the primary, and the reading side mints every origin from it.
"""

from __future__ import annotations

import os

from revl import RevlError
from revl.compiler import compile_files, compile_source

_SINKS = (
    'extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n'
    'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int = @py { return 0 }\n'
)

_BOTH = "Result[Untrusted[Str], Secret[Str]]"

_PROVIDER = (
    f"service Both {{ emission[web] fn both_get(u: Str) -> {_BOTH} }}\n"
    "service One { emission[web] fn one_get(u: Str) -> "
    "Result[Untrusted[Str], Str] }\n"
    "component Prov provides both: Both, one: One {\n"
    "  provide both { fn both_get(u) = None }\n"
    "  provide one { fn one_get(u) = None }\n"
    "}\n"
)


def _manifest() -> dict:
    path = os.path.abspath("base_web_3238.rvl")
    return compile_files([path], sources={path: _PROVIDER})


def _turn_code(body: str) -> str | None:
    turn = (_SINKS + "service Turn { emission fn go(a: Str) -> Int }\n"
            "component T requires both: Both, one: One provides turn: Turn {\n"
            "  provide turn {\n    fn go(a: Str) {\n" + body
            + "\n      return 0\n    }\n  }\n}\n")
    try:
        compile_source(turn, "turn.rvl", manifest=_manifest())
    except RevlError as err:
        return getattr(err, "code", None)
    return None


def test_the_services_ir_stamps_every_qualifier_of_a_return():
    both = _manifest()["services"]["Both"]["methods"]["both_get"]
    assert both["returns_qualifier"] == "Untrusted"
    assert both["returns_qualifiers"] == ["Untrusted", "Secret"]


def test_a_single_qualifier_return_stamps_no_set():
    one = _manifest()["services"]["One"]["methods"]["one_get"]
    assert one["returns_qualifier"] == "Untrusted"
    assert "returns_qualifiers" not in one


def test_the_err_secret_of_a_both_qualifier_return_is_refused_through_a_manifest():
    assert _turn_code("      let r = emit both.both_get(a)\n"
                      "      let x = match r { Ok(v) => 0, Err(e) => emit logit(e) }"
                      ) == "G-SECRET-FLOW"


def test_the_ok_untrusted_of_a_both_qualifier_return_is_refused_through_a_manifest():
    assert _turn_code("      let r = emit both.both_get(a)\n"
                      "      let x = match r { Ok(v) => emit run(v), Err(e) => 0 }"
                      ) in ("G9", "G-SECRET-FLOW")


def test_a_single_qualifier_return_behaves_as_before():
    assert _turn_code("      let r = emit one.one_get(a)\n"
                      "      let x = match r { Ok(v) => emit run(v), Err(e) => 0 }"
                      ) == "G9"
    assert _turn_code("      let r = emit one.one_get(a)\n"
                      "      let x = match r { Ok(v) => 0, Err(e) => emit logit(e) }"
                      ) is None
