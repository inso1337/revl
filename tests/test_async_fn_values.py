"""async function values — roadmap item 92 (design: docs/design/async-function-values.md).

Closes finding #21: a callback arrow whose declared function type carries an
`Async[T]` return colors the receiving fn async, so the emitted loop awaits the
callback on py and ts instead of leaking a coroutine/Promise.

  slice 1  `Async[T]` type algebra — position-restricted, sync->async coercion,
           async->sync refused, wellformedness fences, unify guard.
  slice 2  coloring (rule 2: a fn calling an async-typed param), the arrow async
           IR flag, and the sync-typed-arrow-reaches-async refusal.
  slice 3  ts emit — `Promise<T>`, async arrows, awaited async-typed params.
  slice 4  py emit — `async def`, awaited callbacks, the `_revl_as_async`
           sync->async coercion wrapper.

The exit test is finding #21 made a fixture — the real `agent_loop` shape and a
mock sync-arrow — executed on py (asyncio) and ts (cordis v4), asserting no
coroutine/Promise leak and the right values.
"""

import importlib.util
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError
from revl.typecheck import compatible, unify, check_type_wellformed


ROOT = Path(__file__).resolve().parents[1]


def _emit(backend: str, ir):
    spec = importlib.util.spec_from_file_location(
        f"revl_{backend}_emit_92", ROOT / "backends" / backend / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.emit(ir)


# the finding-#21 shape, parameterised over the callback's declared type ------

def _program(callback_type: str, arrow_body: str, run_class: str = "async fn") -> str:
    return (
        "extern emission async fn remote(msgs: Str) -> Str\n"
        '  = @py { return msgs + " -> reply" }\n'
        '  = @ts { return msgs + " -> reply" }\n'
        "service Model { emission async fn complete(msgs: Str) -> Str }\n"
        "service Runner { emission async fn run(prompt: Str) -> Str }\n"
        f"fn agent_loop(current: Str, complete: {callback_type}) -> Str {{\n"
        "  let resp = complete(current)\n"
        "  return resp\n"
        "}\n"
        "component RealModel provides model: Model {\n"
        "  provide model { async fn complete(msgs) = remote(msgs) }\n"
        "}\n"
        "component Agent requires model: Model provides runner: Runner {\n"
        f"  provide runner {{ {run_class} run(prompt) = "
        f"agent_loop(prompt, {arrow_body}) }}\n"
        "}\n"
    )


ASYNC_CB = "(Str) -> Async[Str]"
_EMIT_ARROW = "msgs => emit model.complete(msgs)"


# -- slice 1: the `Async[T]` type algebra -----------------------------------

def test_sync_flows_into_async_as_a_coercion():
    assert compatible("(Str) -> Async[Str]", "(Str) -> Str") is True
    assert compatible("Async[Str]", "Str") is True


def test_async_does_not_flow_into_sync():
    assert compatible("(Str) -> Str", "(Str) -> Async[Str]") is False
    assert compatible("Str", "Async[Str]") is False


def test_two_async_returns_meet_elementwise():
    assert compatible("(Str) -> Async[Str]", "(Str) -> Async[Str]") is True
    assert compatible("(Str) -> Async[Str]", "(Str) -> Async[Int]") is False


def test_unify_refuses_to_bind_a_tparam_to_async():
    subst: dict = {}
    assert unify("?T", "Async[Str]", subst) is False
    assert subst == {}


def test_async_is_legal_only_as_a_fn_type_return_in_a_param():
    # legal: a module fn parameter
    check_type_wellformed("t.rvl", 1, ASYNC_CB, allow_async_param=True)
    # illegal positions, one message class
    for bad in ("Async[Str]", "List[Async[Str]]", "Opt[Async[Str]]",
                "(Async[Str]) -> Int"):
        with pytest.raises(RevlError, match="not a value type"):
            check_type_wellformed("t.rvl", 1, bad, allow_async_param=True)


def test_async_fn_type_refused_outside_a_module_fn_param():
    with pytest.raises(RevlError, match="only supported as a module `fn` parameter"):
        check_type_wellformed("t.rvl", 1, ASYNC_CB, allow_async_param=False)


def test_verified_fn_may_not_declare_an_async_param():
    with pytest.raises(RevlError, match="module `fn` parameter"):
        compile_source(
            f"verified fn f(g: {ASYNC_CB}) -> Str {{ return g(\"x\") }}", "t.rvl")


# -- slice 2: coloring + the refusal ----------------------------------------

def test_agent_loop_is_colored_async_by_its_async_typed_param():
    ir = compile_source(_program(ASYNC_CB, _EMIT_ARROW), "t.rvl")
    loop = [f for f in ir["functions"] if f["name"] == "agent_loop"][0]
    assert loop.get("async") is True


def test_the_callback_arrow_carries_the_async_flag():
    ir = compile_source(_program(ASYNC_CB, _EMIT_ARROW), "t.rvl")
    arrows: list = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("kind") == "arrow":
                arrows.append(n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(ir["components"])
    assert arrows and all(a.get("async") for a in arrows)


def test_sync_typed_arrow_reaching_async_is_refused():
    # finding #21 made a compile error: the sync callback type carries no color.
    with pytest.raises(RevlError, match="no async color"):
        compile_source(_program("(Str) -> Str", _EMIT_ARROW), "t.rvl")


def test_non_arrow_value_in_an_async_slot_is_refused_v1():
    # a bare fn name would need an `as_async` wrapper the blocking tiers do not
    # yet erase — refused in v1 (both the pure-fn and component call paths).
    src = (
        f"fn apply_cb(cb: {ASYNC_CB}) -> Str {{ return cb(\"x\") }}\n"
        "fn sync_cb(s: Str) -> Str { return s }\n"
        "fn caller() -> Str { return apply_cb(sync_cb) }\n"
    )
    with pytest.raises(RevlError, match="only an arrow may be passed"):
        compile_source(src, "t.rvl")


def test_direct_call_path_is_unchanged():
    # items 80/90: a module fn directly reaching an async extern still colors,
    # with no async-typed param in sight.
    ir = compile_source(
        "extern emission async fn http(u: Str) -> Str = @ts { return await u }\n"
        "fn helper(u: Str) -> Str { return http(u) }\n", "t.rvl")
    helper = [f for f in ir["functions"] if f["name"] == "helper"][0]
    assert helper.get("async") is True


# -- slice 3: ts emit -------------------------------------------------------

def test_ts_emits_async_loop_awaiting_the_callback():
    ts = _emit("typescript", compile_source(_program(ASYNC_CB, _EMIT_ARROW), "t.rvl"))
    assert ("export async function agent_loop(current: string, "
            "complete: ((a0: string) => Promise<string>)): Promise<string> {") in ts
    assert "const resp = (await complete(current))" in ts
    # item 435(b): this arrow's body IS the un-awaited emission Promise, so an
    # `async` would only add a resolution hop over a Promise the body already
    # returns (2 excess microtask turns and 2 excess Promise allocations per
    # operation call). It renders plain, with the identical TS type
    # `(p) => Promise<T>`, so it stays assignable to `complete`. The sibling
    # test below pins the other half of the rule.
    assert "((msgs: any) => (ctx.model.complete(msgs)))" in ts
    assert "(async (msgs: any) =>" not in ts
    assert "(await agent_loop(prompt," in ts


def test_ts_no_promise_leak_sync_arrow_coercion():
    ts = _emit("typescript", compile_source(
        _program(ASYNC_CB, 'msgs => "mock"'), "t.rvl"))
    # a sync-bodied arrow coerced into the async slot still renders async (a
    # no-op await in JS); the loop awaits it uniformly. Item 435(b) deliberately
    # does NOT de-colour this one: the body is a plain value, so a plain arrow
    # would be typed `(p) => T` where the slot says `(p) => Promise<T>`.
    assert "(async (msgs: any) =>" in ts


# -- slice 4: py emit -------------------------------------------------------

def test_py_emits_async_def_awaiting_the_callback():
    py = _emit("python", compile_source(_program(ASYNC_CB, _EMIT_ARROW), "t.rvl"))
    assert "async def agent_loop(current, complete):" in py
    assert "resp = (await complete(current))" in py
    # the async-op arrow is a tail call -> plain lambda returning the coroutine
    assert "lambda msgs: _revl_ctx.model.complete(msgs)" in py
    assert "(await agent_loop(prompt," in py
    # no wrapper needed for the tail-coroutine shape
    assert "_revl_as_async" not in py


def test_py_sync_arrow_coercion_wraps_in_revl_as_async():
    py = _emit("python", compile_source(
        _program(ASYNC_CB, 'msgs => "mock"'), "t.rvl"))
    assert "_revl_as_async(lambda msgs:" in py
    assert "def _revl_as_async(_f):" in py  # helper emitted


# -- the exit test: live execution on py and ts -----------------------------

_EXIT = (
    "extern emission async fn remote(msgs: Str) -> Str\n"
    '  = @py { return msgs + " -> reply" }\n'
    '  = @ts { return msgs + " -> reply" }\n'
    "service Model { emission async fn complete(msgs: Str) -> Str }\n"
    "service Runner { emission async fn run(prompt: Str) -> Str }\n"
    "fn agent_loop(current: Str, complete: (Str) -> Async[Str]) -> Str {\n"
    "  let resp = complete(current)\n"
    "  return resp\n"
    "}\n"
    "component RealModel provides model: Model {\n"
    "  provide model { async fn complete(msgs) = remote(msgs) }\n"
    "}\n"
    "component RealAgent requires model: Model provides runner: Runner {\n"
    "  provide runner { async fn run(prompt) = "
    "agent_loop(prompt, msgs => emit model.complete(msgs)) }\n"
    "}\n"
    "component MockAgent provides runnerm: Runner {\n"
    "  provide runnerm { async fn run(prompt) = "
    'agent_loop(prompt, msgs => "mock reply") }\n'
    "}\n"
    'lifecycle test "real loop awaits the async callback" {\n'
    "  load RealModel\n  load RealAgent\n"
    '  let out = call runner.run("hi")\n'
    '  assert out == "hi -> reply"\n'
    "  unload RealAgent\n  unload RealModel\n  assert no_residue\n"
    "}\n"
    'lifecycle test "mock loop coerces a sync arrow" {\n'
    "  load MockAgent\n"
    '  let out = call runnerm.run("hi")\n'
    '  assert out == "mock reply"\n'
    "  unload MockAgent\n  assert no_residue\n"
    "}\n"
)


def test_exit_py_executes_with_no_coroutine_leak(tmp_path):
    pytest.importorskip(
        "cordis",
        reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
    import subprocess
    import sys

    # Subprocess-isolated: the emitted lifecycle tests drive the cordis-py
    # reference runtime via `asyncio.run`, which must not perturb the shared
    # in-process event loop the rest of the suite reuses. `RuntimeWarning` is
    # raised as an error, so an unawaited coroutine ("... was never awaited")
    # fails the run — the exact finding-#21 leak.
    # `runtime.py` reads its sibling `confidential.py` (the item-256
    # Slice 3 redaction choke point), so the scratch dir needs both.
    for _module in ("runtime.py", "confidential.py"):
        (tmp_path / _module).write_text(
            (ROOT / "backends" / "python" / _module).read_text())
    (tmp_path / "app.py").write_text(_emit("python", compile_source(_EXIT, "exit.rvl")))
    (tmp_path / "driver.py").write_text(
        "import warnings\n"
        "warnings.simplefilter('error', RuntimeWarning)\n"
        "import app\n"
        "for _name, _fn in app.REVL_TESTS:\n"
        "    _fn()\n"
        "    print('PASS', _name)\n")
    result = subprocess.run(
        [sys.executable, "driver.py"], cwd=tmp_path,
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"emitted py exit test failed:\n{result.stdout}\n{result.stderr}")
    assert result.stdout.count("PASS") == 2, result.stdout
