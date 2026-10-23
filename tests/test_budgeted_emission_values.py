"""First-class component uses must not bypass a declared emission ceiling."""

import pytest

from revl import compile_source
from revl.errors import RevlError


_HOST = (
    "extern emission[net(calls=1)] fn call(x: Str) -> Int "
    "= @py { return 1 }\n"
)
_OPS = "service Ops { emission fn m() }\n"


@pytest.mark.parametrize(
    ("extra", "body", "name"),
    [
        ("", 'let f = call return f("a")', "call"),
        (
            "fn h(x: Str) -> Int { return call(x) }\n",
            'let g = h return g("a")',
            "h",
        ),
        (
            "fn apply(f: (Str) -> Int, x: Str) -> Int { return f(x) }\n",
            'let r = apply(call, "a") return r',
            "call",
        ),
    ],
)
def test_budgeted_emission_values_are_refused_in_component_methods(extra, body, name):
    src = (
        _HOST + extra + _OPS
        + "component C provides ops: Ops { provide ops { "
        + f"fn m() {{ {body} }} }} }}\n"
    )

    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "budgeted-value.rvl")

    assert excinfo.value.code == "G4"
    assert excinfo.value.message == (
        f"call to emission `{name}` must be marked `emit` (G4)"
    )


def test_budget_free_emission_extern_remains_usable_as_a_value():
    src = (
        "extern emission[net] fn call(x: Str) -> Int = @py { return 1 }\n"
        + _OPS
        + "component C provides ops: Ops { provide ops { "
        + 'fn m() { let f = call return f("a") } } }\n'
    )

    compile_source(src, "budget-free-value.rvl")


def test_non_emission_extern_remains_usable_as_a_value():
    src = (
        "extern pure fn call(x: Str) -> Int = @py { return 1 }\n"
        "service Ops { fn m() -> Int }\n"
        "component C provides ops: Ops { provide ops { "
        + 'fn m() { let f = call return f("a") } } }\n'
    )

    compile_source(src, "pure-value.rvl")


@pytest.mark.parametrize(
    ("extra", "body", "name"),
    [
        ("", 'return call("a")', "call"),
        (
            "fn go(x: Str) -> Int { return call(x) }\n",
            'return go("a")',
            "go",
        ),
    ],
)
def test_direct_budgeted_emission_calls_keep_the_existing_g4_wording(extra, body, name):
    src = (
        _HOST + extra + _OPS
        + "component C provides ops: Ops { provide ops { "
        + f"fn m() {{ {body} }} }} }}\n"
    )

    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "direct-call.rvl")

    assert excinfo.value.message == (
        f"call to emission `{name}` must be marked `emit` (G4)"
    )


def test_statement_emit_through_a_named_wrapper_still_observes_its_ceiling():
    src = (
        _HOST
        + "fn once(x: Str) -> Int { return call(x) }\n"
        + "service Ops { emission[net(calls=0)] fn m() }\n"
        + "component C provides ops: Ops { provide ops { "
        + 'fn m() { emit once("a") return 0 } } }\n'
    )

    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "metered-wrapper.rvl")

    assert excinfo.value.code == "G4"
    assert "emits through `net(calls=1)`" in excinfo.value.message
    assert "`once()`" in excinfo.value.message
