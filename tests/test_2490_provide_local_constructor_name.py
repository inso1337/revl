"""Issue #2490: a provide-method parameter or local named `Some` (or another
built-in case name) is the callee of `Some(1)`, as it is in a `fn` body.

The component-stratum lowering (`lower._lower_component_pure_expr`) read the
callee's spelling against the constructors and the module callables before it
read the scope, so `Some(1)` in a provide method built the built-in `Opt`
constructor while the method's own parameter was renamed to `Some_` beside it.
Before #2488 the program compiled to the wrong call; with #2488 the checker
refused it as a `fn` returning `Opt[Int]`. The `fn` body path
(`lower._lower_pure_expr`) has always let the binding win (issue #320).

Now the scope is read first in both strata, and the checker types the call on
the local (`typecheck.local_function_call_type`) as the `fn` body's checker
does, so a wrong-typed argument meets the same refusal in both.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402

emit_py = backend_emitter("python")

_SERVICE = ("service Q {\n"
            "  fn f(Some: (Int) -> Int) -> Int\n"
            "  fn h(Ok: (Int) -> Int) -> Int\n"
            "  fn k(step: (Int) -> Int) -> Int\n"
            "  fn wrap(x: Int) -> Opt[Int]\n"
            "}\n")
_METHODS = {
    "f": "fn f(Some) = Some(1)",
    "h": "fn h(Ok) = Ok(2)",
    "k": "fn k(step) { let Some = step\n    return Some(3) }",
    "wrap": "fn wrap(x) = Some(x)",
}


def _provide(**bodies: str) -> str:
    methods = dict(_METHODS, **bodies)
    return (_SERVICE + "component C provides q: Q { provide q {\n  "
            + "\n  ".join(methods[m] for m in ("f", "h", "k", "wrap"))
            + "\n} }\n")


PROVIDE = _provide()

# method -> (the lowered callee of its return, the `fn` body twin)
# `LOCAL_CALLEE` is read by tests/test_selfhost_compile.py.
LOCAL_CALLEE: dict[str, tuple[str, str]] = {
    "f": ("Some_", "fn f(Some: (Int) -> Int) -> Int { return Some(1) }\n"),
    "h": ("Ok_", "fn h(Ok: (Int) -> Int) -> Int { return Ok(2) }\n"),
    "k": ("Some_", "fn k(step: (Int) -> Int) -> Int {\n"
                   "  let Some = step\n  return Some(3)\n}\n"),
}

# name -> (provide-method source, the `fn` body twin, what both say)
# `LOCAL_CALLEE_REFUSED` is read by tests/test_selfhost_compile.py.
LOCAL_CALLEE_REFUSED: dict[str, tuple[str, str, str]] = {
    "some_argument_type": (
        _provide(f='fn f(Some) = Some("x")'),
        'fn f(Some: (Int) -> Int) -> Int { return Some("x") }\n',
        "argument 1 of `Some` expects `Int`, got `Str`"),
    "some_arity": (
        _provide(f="fn f(Some) = Some(1, 2)"),
        "fn f(Some: (Int) -> Int) -> Int { return Some(1, 2) }\n",
        "`Some` is a `(Int) -> Int` and takes 1 argument(s), 2 given"),
    "ok_argument_type": (
        _provide(h='fn h(Ok) = Ok("x")'),
        'fn h(Ok: (Int) -> Int) -> Int { return Ok("x") }\n',
        "argument 1 of `Ok` expects `Int`, got `Str`"),
}


def _method_return(ir: dict, name: str) -> dict:
    provide = ir["components"][0]["body"][0]
    assert provide["step"] == "provide"
    method = next(m for m in provide["methods"] if m["name"] == name)
    step = method["body"][-1]
    assert step["step"] == "return", method["body"]
    return step["expr"]


@pytest.mark.parametrize("method", sorted(LOCAL_CALLEE))
def test_the_local_is_the_callee_in_a_provide_method(method):
    callee, _twin = LOCAL_CALLEE[method]
    expr = _method_return(compile_source(PROVIDE, "t.rvl"), method)
    assert expr["kind"] == "call", expr
    assert expr["callee"] == {"kind": "name", "id": callee}, expr


def test_the_constructor_still_builds_an_opt_in_a_provide_method():
    expr = _method_return(compile_source(PROVIDE, "t.rvl"), "wrap")
    assert expr["callee"] == {"kind": "var", "name": "Some"}, expr


@pytest.mark.parametrize("method", sorted(LOCAL_CALLEE))
def test_the_fn_body_twin_calls_the_local(method):
    _callee, twin = LOCAL_CALLEE[method]
    ns: dict = {}
    exec(compile(emit_py.emit(compile_source(twin, "t.rvl")), "twin.py",
                 "exec"), ns)
    assert ns[method](lambda x: x + 40) in (41, 42, 43)


def test_the_provide_method_calls_the_local_when_run():
    pytest.importorskip(
        "cordis",
        reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
    import loader  # backends/python
    import runtime  # backends/python
    from cordis import Context

    module = loader.load(compile_source(PROVIDE, "t.rvl"))

    async def main() -> dict:
        root = Context()
        runtime.plug(root, module.C, {})
        for _ in range(12):
            await asyncio.sleep(0)
        q = root.get("q")
        return {"f": q.f(lambda x: x + 40), "h": q.h(lambda x: x + 40),
                "k": q.k(lambda x: x + 40), "wrap": q.wrap(7)}

    assert asyncio.run(main()) == {"f": 41, "h": 42, "k": 43, "wrap": 7}


@pytest.mark.parametrize("name", sorted(LOCAL_CALLEE_REFUSED))
def test_a_wrong_call_on_the_local_is_refused_like_the_fn_body(name):
    source, twin, message = LOCAL_CALLEE_REFUSED[name]
    for text in (twin, source):
        with pytest.raises((RevlError, RevlErrors)) as refused:
            compile_source(text, "t.rvl")
        assert message in str(refused.value), str(refused.value)
