"""First-class uses must not bypass a declared emission ceiling (issue #2834).

An emission extern with a declared ceiling (`net(calls=1)`) is metered at its
named `emit` call site. Used as a value, it would be dispatched with no marker,
so the ceiling would never be checked and no emission step recorded. Every
such value is refused, in a component method and in a module function that
hands one to a method; the budget-free case is issue #2842 and refused the
same way."""

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
    assert excinfo.value.message == _value_message(name)


def _value_message(name: str) -> str:
    subject = ("emission `call`" if name == "call"
               else f"`{name}` reaches emission `call` and")
    return (f"{subject} cannot be used as a function value; call it by name "
            "under `emit` (G4)")


_GET = "fn get() -> (Str) -> Int { return call }\n"


@pytest.mark.parametrize(
    ("extra", "body"),
    [
        # a module function returns the extern; the method binds the result
        (_GET, 'let f = emit get() return f("a")'),
        (_GET, 'let f = get() return f("a")'),
        # a module function binds the extern itself, reached under `emit`
        ("fn go(x: Str) -> Int { let f = call return f(x) }\n",
         'return emit go("a")'),
    ],
    ids=["emit-get", "get-without-emit", "go-binds-locally"],
)
def test_a_module_function_cannot_hand_a_budgeted_emission_to_a_method(
        extra, body):
    src = (
        _HOST + extra + "service Ops { emission fn m() -> Int }\n"
        + "component C provides ops: Ops { provide ops { "
        + f"fn m() {{ {body} }} }} }}\n"
    )

    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "budgeted-helper.rvl")

    assert excinfo.value.code == "G4"
    assert excinfo.value.message == _value_message("call")


def test_a_budgeted_emission_value_in_a_compensate_slot_is_refused():
    src = (
        "service Db { emission fn execute(q: Str) -> Int }\n" + _HOST
        + "fn apply(f: (Str) -> Int, x: Str) -> Int { return f(x) }\n"
        + "service Ops { emission fn m() -> Int }\n"
        + "component C requires db: Db provides ops: Ops { provide ops { "
        + 'fn m() { emit db.execute("r") compensate apply(call, "r") '
        + "return 0 } } }\n"
    )

    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "budgeted-compensate.rvl")

    assert excinfo.value.message == _value_message("call")


def test_a_local_shadowing_the_extern_name_is_not_the_extern():
    src = (
        _HOST + "service Ops { emission fn m() -> Int }\n"
        + "component C provides ops: Ops { provide ops { "
        + "fn m() { let call = 1 let g = call return 0 } } }\n"
    )

    compile_source(src, "budgeted-shadow.rvl")


_SCOPE_HEAD = (
    _HOST + "fn apply(f: (Str) -> Int, x: Str) -> Int { return f(x) }\n"
    + "service Ops { emission fn a(call: Int) -> Int\n emission fn c() -> Int\n emission fn b() -> Int }\n"
)


@pytest.mark.parametrize(
    ("a", "b", "c"),
    [
        # a parameter of another method
        ("return call", 'return apply(call, "x")', "return 0"),
        # a let in a block that has closed
        ("return 0", 'if (true) { let call = 1 } return apply(call, "x")', "return 0"),
        # a for element after its loop
        ("return 0", 'for (call of [1]) { } return apply(call, "x")', "return 0"),
        # a let after the use
        ("return 0", 'let r = apply(call, "x") let call = 2 return r', "return 0"),
        # a let in another method
        ("return 0", 'let f = call return f("x")', "let call = 1 return call"),
    ],
    ids=["other-method-param", "closed-block", "after-loop", "let-after-use",
         "other-method-let"],
)
def test_a_binding_shadows_the_extern_only_where_it_is_in_scope(a, b, c):
    src = (
        _SCOPE_HEAD + "component C provides ops: Ops { provide ops { "
        + f"fn a(call) {{ {a} }} fn c() {{ {c} }} fn b() {{ {b} }} }} }}\n"
    )

    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "budgeted-scope.rvl")

    assert excinfo.value.code == "G4"
    assert excinfo.value.message == _value_message("call")


def test_a_budget_free_emission_extern_is_refused_as_a_value_too():
    src = (
        "extern emission[net] fn call(x: Str) -> Int = @py { return 1 }\n"
        + _OPS
        + "component C provides ops: Ops { provide ops { "
        + 'fn m() { let f = call return f("a") } } }\n'
    )

    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "budget-free-value.rvl")

    assert excinfo.value.message == _value_message("call")


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
