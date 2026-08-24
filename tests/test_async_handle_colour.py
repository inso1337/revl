"""Async colour through a spawned-handle emission in an arrow body — roadmap
item 106 (harness finding #27), a follow-on to items 90/92 async coloring.

The async-colour analysis colours a `req`-based emission (`model.complete` on a
`requires`), but a *handle*-based one — `emit <handle>.<key>.<method>()` on a
spawned instance — reaches the same suspension the emitter must `await`. The py
emitter's arrow decision (`_py_async_arrow`) used to miss the handle call and
wrap the arrow with `_revl_as_async` (the sync classification), so awaiting the
arrow yielded a coroutine OBJECT: `can only concatenate str (not 'coroutine')`
and `coroutine was never awaited` at runtime.

Two guards, tested here:
  * the py emitter recognises a handle-emission tail call as a coroutine, so a
    colored arrow delegating to a spawned async worker renders as a plain
    tail-coroutine lambda (awaited at the call site) — the arrow shape RUNS on
    cordis-py without the coroutine leak (the exit test);
  * `_revl_as_async` degrades safely — it awaits an awaitable result and passes
    a genuinely-sync result straight through — so a residual misclassification
    can never leak a coroutine.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backends" / "python"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402
import emit as pyemit  # noqa: E402  (backends/python)


# A supervisor spawns a worker whose service op is `async`, then delegates to
# it through an arrow — `t => emit w1.wtask.run(t)` — handed to a module fn
# coloured async by its `Async[…]`-typed parameter (items 90/92). The arrow's
# body is a handle emission, the exact shape finding #27 leaked a coroutine on.
SOURCE = """
service WorkTask { emission async fn run(task: Str) -> Str }
service Super { emission async fn go(task: Str) -> Str }

fn run_one(task: Str, f: (Str) -> Async[Str]) -> Str {
  let r = f(task)
  return r
}

component Worker provides wtask: WorkTask {
  provide wtask {
    async fn run(task) = task
  }
}

component Supervisor provides super: Super {
  let w1 = effect spawn Worker with {} undo w1.dispose()
  provide super {
    async fn go(task) = run_one(task, t => emit w1.wtask.run(t))
  }
}
"""


def test_handle_emission_arrow_renders_as_tail_coroutine_not_sync_wrap():
    """The colour fix at the emission boundary: the arrow tail-calling the
    spawned async worker must emit as a plain coroutine lambda, never wrapped
    in `_revl_as_async` (the sync classification that leaks the coroutine)."""
    ir = compile_source(SOURCE, "wf.rvl")
    py = pyemit.emit(ir)
    go = next(line for line in py.splitlines() if "await run_one(" in line)
    # a plain lambda whose body is the handle emission — the call site awaits it
    assert "lambda t:" in go
    assert "_revl_as_async" not in go, go


cordis = pytest.importorskip(
    "cordis", reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")

import loader  # noqa: E402  (backends/python)
import runtime  # noqa: E402  (backends/python)
from cordis import Context  # noqa: E402


async def _flush(root) -> None:
    for _ in range(12):
        await asyncio.sleep(0)


def test_handle_emission_arrow_runs_without_coroutine_leak():
    """The exit test (item 106 DoD): the spawn + handle-emission arrow shape
    RUNS on cordis-py and returns the awaited value — no coroutine leak."""
    ir = compile_source(SOURCE, "wf.rvl")
    module = loader.load(ir)

    result: dict = {}

    async def main() -> None:
        root = Context()
        runtime.plug(root, module.Supervisor, {})
        await _flush(root)
        # go() delegates to the spawned worker through the handle-emission
        # arrow; before the fix this raised TypeError (str + coroutine) /
        # warned "coroutine was never awaited".
        result["value"] = await root.get("super").go("ping")
        await _flush(root)

    asyncio.run(main())
    assert result["value"] == "ping", result


def test_revl_as_async_degrades_safely_on_a_coroutine_result():
    """Belt-and-suspenders: even if a coroutine-returning body is ever routed
    through the sync wrapper, `_revl_as_async` awaits it instead of leaking it,
    while a genuinely-sync body still returns its value."""
    # Build the wrapper exactly as the emitter emits it, in isolation.
    src = "\n".join([
        "def _revl_as_async(_f):",
        "    async def _g(*_a, **_k):",
        "        _r = _f(*_a, **_k)",
        "        if hasattr(_r, \"__await__\"):",
        "            return await _r",
        "        return _r",
        "    return _g",
    ])
    ns: dict = {}
    exec(src, ns)
    _revl_as_async = ns["_revl_as_async"]

    async def _coro(x):
        return x + "!"

    async def check() -> None:
        # a coroutine-returning `_f`: awaited, not leaked
        assert await _revl_as_async(lambda x: _coro(x))("hi") == "hi!"
        # a genuinely-sync `_f`: value passes straight through
        assert await _revl_as_async(lambda x: x.upper())("hi") == "HI"

    asyncio.run(check())


def test_emitted_wrapper_matches_the_degrading_shape():
    """The emitted `_revl_as_async` (when a sync coercion is present) carries
    the awaitable-degrading body, not the old bare `return _f(...)`."""
    coerce_src = """
service Model { emission async fn complete(msgs: Str) -> Str }
fn agent_loop(current: Str, complete: (Str) -> Async[Str]) -> Str {
  let resp = complete(current)
  return resp
}
component Mock provides model: Model {
  provide model { async fn complete(msgs) = agent_loop(msgs, m => "canned") }
}
"""
    py = pyemit.emit(compile_source(coerce_src, "mock.rvl"))
    assert "def _revl_as_async(_f):" in py
    assert 'if hasattr(_r, "__await__"):' in py
    assert "return await _r" in py
