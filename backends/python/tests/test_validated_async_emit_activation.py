"""A `validated` async service operation fired from a component ACTIVATION body
as `await emit model.op(...)`.

The `validated` seam (item 257) checks the operation's SETTLED response. In an
async provide method the call renders `_revl_validate((await <call>), ..)`, but
the activation-body `emit` step prefixed its own `await` to the whole fire, so it
rendered `await _revl_validate(<call>, ..)`: the seam validated the COROUTINE
object, never the response, and the activation failed on every well-formed
answer. These tests plug the emitted component into cordis-py and drive it.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types

import pytest

from cordis import Context
from cordis.fiber import FiberState

import emit

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl import compile_source  # noqa: E402

_PROGRAM = """
type Turn = Final(Str) | Again(Int)
service Model { emission validated %sasync fn complete(p: Str) -> Turn }
component C requires model: Model {
  await emit model.complete("hi")
}
"""


class _Model:
    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = 0

    async def complete(self, p):
        self.calls += 1
        await asyncio.sleep(0)
        return self.answer


def _module(name: str, retry: str = "") -> types.ModuleType:
    code = emit.emit(compile_source(_PROGRAM % retry))
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(code, f"{name}.py", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


async def _flush() -> None:
    for _ in range(60):
        await asyncio.sleep(0)


async def _run(module, answer):
    root = Context()
    model = _Model(answer)
    root.provide("model", model)
    c = root.plugin(module.C)
    await _flush()
    return c, model


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["", "retry 2 "])
async def test_a_well_formed_response_is_validated_and_the_activation_lives(retry):
    module = _module(f"validated_activation_ok_{len(retry)}", retry)
    c, model = await _run(module, {"tag": "Final", "value": "done"})
    assert model.calls == 1
    assert c.state is FiberState.ACTIVE, "the seam validated the coroutine, not the response"
    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED


@pytest.mark.asyncio
async def test_a_malformed_response_still_faults_the_activation():
    module = _module("validated_activation_bad")
    c, model = await _run(module, {"tag": "Nope", "value": 1})
    assert model.calls == 1
    assert c.state is FiberState.FAILED


def test_the_await_sits_inside_the_seam():
    code = emit.emit(compile_source(_PROGRAM % ""))
    assert "_revl_validate((await _revl_ctx.model.complete('hi')), " in code
    assert "await _revl_validate(" not in code
