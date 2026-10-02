"""An `every <x> in <sub>` loop whose item is named after a Python builtin the
emitter escapes (`len`, `str`, ...).

`_stream_iter` wrapped the item name in `_mangle` on top of `_ident`, which
already applies the same keyword/builtin rename. A colliding name was therefore
escaped twice at its binding and once where the body read it: the frontend's
`len_` was bound as `len___` and read as `len__`, so the first item raised
`NameError` inside the activation. Run here end to end against cordis-py, the
same way tests/test_stream_runtime.py drives the iteration form.
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
import runtime as runtime_mod

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl import compile_source  # noqa: E402

_ITER = """
service Sink { emission fn write(v: Str) }
component C requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  every %s in sub {
    emit sink.write(%s)
  }
}
"""


class _Sink:
    def __init__(self) -> None:
        self.written: list = []

    def write(self, v):
        self.written.append(v)
        return None


def _module(name: str, bind: str) -> types.ModuleType:
    code = emit.emit(compile_source(_ITER % (bind, bind)))
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


@pytest.fixture(autouse=True)
def _reset_streams():
    runtime_mod.Stream.reset()
    runtime_mod.Clock.reset()
    yield
    runtime_mod.Stream.reset()
    runtime_mod.Clock.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("bind", ["len", "str", "o"])
async def test_an_item_named_after_a_builtin_reaches_the_body(bind):
    module = _module(f"stream_iter_bind_{bind}", bind)
    root = Context()
    sink = _Sink()
    root.provide("sink", sink)
    c = root.plugin(module.C)
    await _flush()
    assert c.state is FiberState.ACTIVE

    src = runtime_mod.Stream.last_source()
    src.emit("a")
    src.emit("b")
    await _flush()
    assert c.state is FiberState.ACTIVE, "the body raised on the first item"
    assert sink.written == ["a", "b"]

    c.dispose()
    await _flush()
    assert c.state is FiberState.DISPOSED
    assert runtime_mod.Stream.pending() == 0


def test_the_binding_and_the_read_are_one_name():
    code = emit.emit(compile_source(_ITER % ("len", "len")))
    assert "len__ = await sub.next()" in code
    assert "_revl_ctx.sink.write(len__)" in code
    assert "len___" not in code
