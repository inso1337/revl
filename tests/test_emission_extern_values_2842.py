"""Emission extern callables must not escape as first-class values (#2842)."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


_HOST = "extern emission fn send(x: Str) -> Int = @py { return 1 }\n"
_SERVICE = "service Ops { emission fn invoke() -> Int }\n"

_CASES = [
    # (id, method body, module functions, the value refused, the extern named)
    ("bound-local", 'let f = send\nreturn f("x")', "", "send", "send"),
    (
        "function-typed-argument",
        'return apply(send, "x")',
        "fn apply(f: (Str) -> Int, x: Str) -> Int { return f(x) }\n",
        "send",
        "send",
    ),
    (
        "helper-returns-callable",
        'let f = get_send()\nlet first = f("one")\nreturn f("two")',
        "fn get_send() -> (Str) -> Int { return send }\n",
        "send",
        "send",
    ),
    (
        "helper-reaching-emission-used-as-value",
        'let f = bill\nreturn f("x")',
        "fn bill(x: Str) -> Int { return send(x) }\n",
        "bill",
        "send",
    ),
    (
        "arrow-captures-callable",
        'let get_send = () => send\nlet f = get_send()\nreturn f("x")',
        "",
        "send",
        "send",
    ),
    ("record-field", 'let r = { f: send }\nlet g = r.f\nreturn g("x")', "",
     "send", "send"),
    ("list-element", 'let fs = [send]\nreturn fs[0]("x")', "", "send", "send"),
    (
        "argument-inside-an-emit-call",
        'return emit apply(send, "x")',
        "fn apply(f: (Str) -> Int, x: Str) -> Int { return f(x) }\n",
        "send",
        "send",
    ),
    (
        "generic-identity",
        'let f = id(send)\nreturn f("x")',
        "fn id[T](x: T) -> T { return x }\n",
        "send",
        "send",
    ),
]

# An arrow written in a module function whose body calls an emission is the
# same value: whoever receives it runs the host body outside any `emit`.
_ARROW_CASES = [
    (
        "returned-arrow",
        'let f = emit h()\nreturn f("x")',
        "fn h() -> (Str) -> Int { return (x: Str) => send(x) }\n",
        "this arrow reaches emission `send` and cannot be used as a function "
        "value; call `send` by name under `emit` (G4)",
    ),
    (
        "arrow-in-a-record",
        'let r = emit h()\nlet g = r.f\nreturn g("x")',
        "type R = { f: (Str) -> Int }\n"
        "fn h() -> R { return { f: (x: Str) => send(x) } }\n",
        "this arrow reaches emission `send` and cannot be used as a function "
        "value; call `send` by name under `emit` (G4)",
    ),
    (
        "arrow-through-a-helper",
        'let f = emit h()\nreturn f("x")',
        "fn bill(x: Str) -> Int { return send(x) }\n"
        "fn h() -> (Str) -> Int { return (x: Str) => bill(x) }\n",
        "this arrow reaches emission `send` through `bill` and cannot be used "
        "as a function value; call `bill` by name under `emit` (G4)",
    ),
]


def _value_message(name: str, extern: str) -> str:
    subject = (f"emission `{extern}`" if name == extern
               else f"`{name}` reaches emission `{extern}` and")
    return (f"{subject} cannot be used as a function value; call it by name "
            "under `emit` (G4)")


def _program(body: str, functions: str = "") -> str:
    return (
        _HOST
        + functions
        + _SERVICE
        + "component C provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn invoke() {\n"
        + f"      {body}\n"
        + "    }\n"
        + "  }\n"
        + "}\n"
    )


@pytest.mark.parametrize(
    ("_name", "body", "functions", "callable_name", "extern"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_emission_reaching_callables_are_refused_by_name(
    _name, body, functions, callable_name, extern
):
    with pytest.raises(RevlError) as caught:
        compile_source(_program(body, functions), "emission-values-2842.rvl")

    assert caught.value.code == "G4"
    assert caught.value.category == "emission-value"
    assert caught.value.message == _value_message(callable_name, extern)


@pytest.mark.parametrize(
    ("_name", "body", "functions", "message"),
    _ARROW_CASES,
    ids=[case[0] for case in _ARROW_CASES],
)
def test_an_arrow_calling_an_emission_in_a_fn_body_is_refused(
    _name, body, functions, message
):
    with pytest.raises(RevlError) as caught:
        compile_source(_program(body, functions), "emission-arrows-2842.rvl")

    assert caught.value.code == "G4"
    assert caught.value.message == message


_ASYNC = (
    "extern emission async fn aop(x: Str) -> Str = @py { return x }\n"
    "fn mixA(x: Str) -> Str { let a = aop(x) let n = send(x) return a }\n"
)


@pytest.mark.parametrize(
    ("functions", "body"),
    [
        ("fn get() -> (Str) -> Str { return y => mixA(y) }\n",
         'let f = emit get()\nreturn 0'),
        ("type R = { f: (Str) -> Async[Str] }\n"
         "fn get() -> R { return { f: y => mixA(y) } }\n",
         'let r = emit get()\nreturn 0'),
        ("type R = { f: (Str) -> Str }\n"
         "fn get() -> R { return { f: y => mixA(y) } }\n",
         'let r = emit get()\nreturn 0'),
        ("fn get() -> List[(Str) -> Str] { return [y => mixA(y)] }\n",
         'let r = emit get()\nreturn 0'),
        ("fn ap(f: (Str) -> Str, x: Str) -> Str { return f(x) }\n"
         "fn lift(p: Str) -> Str { return ap(y => mixA(y), p) }\n",
         'let r = emit lift("p")\nreturn 0'),
    ],
    ids=["returned", "async-record-field", "record-field", "list", "sync-slot"],
)
def test_an_async_emission_arrow_cannot_leave_its_fn_body(functions, body):
    """The value rule leaves an arrow calling an async-colored emission
    callable to A1, which refuses these ways for such an arrow to leave the
    `fn` that made it."""
    with pytest.raises(RevlError) as caught:
        compile_source(_program(body, _ASYNC + functions), "emission-async-arrow.rvl")

    assert caught.value.code == "A1"


_WRAPPED_ASYNC_PARAM = (
    "extern emission fn send(x: Str) -> Int = @py { return 1 }\n"
    "extern emission async fn aop(x: Str) -> Str = @py { return x }\n"
    "fn wrapc(c: (Str) -> Async[Str]) -> (Str) -> Str { return (y) => c(y) }\n"
    "fn get() -> (Str) -> Str { return wrapc(y => aop(y)) }\n"
    "service Ops { emission async fn invoke(x: Str) -> Str }\n"
    "component C provides ops: Ops { provide ops { async fn invoke(x) {\n"
    "  let f = emit get()\n"
    "  return f(x)\n"
    "} } }\n"
)


def test_a_sync_arrow_cannot_wrap_an_async_typed_parameter():
    """An `Async[..]`-typed parameter suspends when called, so a sync arrow
    that calls it is the same leak as one that calls the async extern by
    name. Without the refusal the arrow carries `aop` out of `wrapc` as a
    plain value and the caller reaches it with no recording seam."""
    with pytest.raises(RevlError) as caught:
        compile_source(_WRAPPED_ASYNC_PARAM, "emission-async-param-wrap.rvl")

    assert caught.value.code == "A1"
    assert "async callable `c`" in caught.value.message


def test_an_arrow_parameter_shadows_an_async_typed_parameter():
    """The control: inside an arrow that rebinds `c` as its own parameter,
    `c` is that sync parameter, not the async one."""
    compile_source(
        'fn w(c: (Str) -> Async[Str]) -> ((Str) -> Str) -> Str '
        '{ return (c) => c("a") }\n',
        "emission-async-param-shadow.rvl")


def test_an_emission_value_inside_a_helper_reached_under_emit_is_refused():
    """`emit go(x)` is a marked call, but inside `go` the extern is a value
    dispatched with no marker of its own."""
    source = _program(
        'return emit go("x")',
        "fn go(x: Str) -> Int { let f = send return f(x) }\n")
    with pytest.raises(RevlError) as caught:
        compile_source(source, "emission-values-2842-go.rvl")

    assert caught.value.message == _value_message("send", "send")


def test_emit_marked_emission_still_uses_the_python_recording_seam():
    source = _program('return emit send("x")')
    ir = compile_source(source, "emission-values-2842-control.rvl")
    spec = importlib.util.spec_from_file_location(
        "pyemit_emission_values_2842", ROOT / "backends/python/emit.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "_revl_extern_emit(_revl_ctx, 'send', send," in module.emit(ir)


def test_non_emission_extern_values_remain_usable():
    source = (
        "extern pure fn format_name(x: Str) -> Str = @py { return x }\n"
        + _SERVICE
        + "component C provides ops: Ops {\n"
        + "  provide ops {\n"
        + '    fn invoke() { let f = format_name return f("x") }\n'
        + "  }\n"
        + "}\n"
    )

    compile_source(source, "emission-values-2842-pure-control.rvl")


def test_scoped_callable_shadowing_an_emission_name_is_not_misclassified():
    source = (
        _HOST
        + "service Ops { emission fn invoke(send: (Str) -> Int) -> Int }\n"
        + "component C provides ops: Ops {\n"
        + "  provide ops { fn invoke(send) { let alias = send return 1 } }\n"
        + "}\n"
    )

    compile_source(source, "emission-values-2842-shadow-control.rvl")
