"""Each session's runtime slots are its own (issue #3380, slice 2).

The Python runtime used to keep the running session's owner, the host trace
sink and the stack of activating frames in module slots, one of each for the
whole process, which is why two sessions on two threads had to take turns
(slice 1, the process's session turn). Those three are context-local now: a
thread, and every asyncio task it starts, sees the owner, trace and activating
stack of its own session and nothing of another's. The turn stays for the
slots later slices make per session; these tests take it away to show that the
three no longer need it.
"""

import importlib
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

from revl import compile_files
from revl.mcp import session as session_mod
from revl.mcp.session import Session

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a live Session needs the cordis-py runtime")

HOOK = "_revl_3380_ctx_hook"

# `boot` runs in the activation body, so its host code runs while this
# component's frame is the activating one; `whose` runs in a method call.
SOURCE = f"""
type Boot = {{ id: Int }}
extern pure fn unboot(h: Boot) = @py {{ return None }}
extern acquire fn boot() -> Boot undo unboot(result) = @py {{
  import {HOOK}
  return {{"id": {HOOK}.booted()}}
}}
extern emission[probe] fn whose() -> Int = @py {{
  import {HOOK}
  return {HOOK}.fn()
}}
service Echo {{ emission[probe] fn owner() -> Int }}
component Plain provides echo: Echo {{
  let booted = effect boot() undo unboot(booted)
  let store = effect Map.new() undo store.drop()
  provide echo {{
    fn owner() {{
      let o = emit whose()
      return o
    }}
  }}
}}
"""


class _NoTurn:
    """The process's session turn, bypassed: two sessions run at once."""

    def __init__(self, session) -> None:
        self.session = session

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _runtime():
    """The Python runtime module: the one a loaded Session imported, or
    imported here when no test in this process has loaded one yet."""
    module = sys.modules.get("runtime")
    if module is None:
        backend = str(Path(__file__).resolve().parents[1] / "backends" / "python")
        if backend not in sys.path:
            sys.path.insert(0, backend)
        module = importlib.import_module("runtime")
    return module


def _slots() -> tuple:
    """The running session as the runtime sees it from the calling thread:
    its owner, its host trace sink and the activating frame on top."""
    runtime = _runtime()
    trace_var = getattr(runtime, "_TRACE", None)
    trace = trace_var.get() if trace_var is not None else runtime._trace
    stack = runtime._ACTIVATING
    if hasattr(stack, "get"):
        stack = stack.get()
    return runtime.session_owner(), trace, (stack[-1] if stack else None)


@pytest.fixture
def hook(tmp_path):
    module = types.ModuleType(HOOK)
    module.booted = lambda: 1
    module.fn = lambda: 0
    sys.modules[HOOK] = module
    path = tmp_path / "echo.rvl"
    path.write_text(SOURCE)
    module.ir = compile_files([str(path)])
    try:
        yield module
    finally:
        del sys.modules[HOOK]


@pytest.fixture
def no_turn(monkeypatch):
    monkeypatch.setattr(session_mod, "_SessionTurn", _NoTurn)


def _owner_call(session) -> int:
    return session.call("echo", "owner", [], raw=True)["result"]


def _frame_of(session):
    """The one activation frame this session's owner registered: `Plain`'s,
    and not the other session's."""
    frames = list(session._owner._registry)
    assert [f._owner is session._owner for f in frames] == [True], frames
    return frames[0]


def test_two_sessions_at_once_each_see_their_own_owner_trace_and_frame(
        hook, no_turn):
    """Both threads sit inside a host call at the same time, once during the
    activation (`boot`) and once during a method call (`whose`), and each
    reads the slots then. Each must read its own session's."""
    barrier = threading.Barrier(2, timeout=30)
    seen: dict = {}

    def booted() -> int:
        barrier.wait()
        seen[(threading.current_thread().name, "load")] = _slots()
        return 1

    def fn() -> int:
        barrier.wait()
        seen[(threading.current_thread().name, "call")] = _slots()
        return 0

    hook.booted, hook.fn = booted, fn
    sessions: dict = {}
    errors: dict = {}

    def run():
        name = threading.current_thread().name
        try:
            session = Session()
            session.load(hook.ir, origin={"source": SOURCE})
            sessions[name] = session
            _owner_call(session)
        except BaseException as exc:  # noqa: BLE001 - read by the test
            errors[name] = exc
            barrier.abort()

    crew = [threading.Thread(target=run, name=f"session-{i}") for i in (1, 2)]
    for thread in crew:
        thread.start()
    for thread in crew:
        thread.join(120)
    try:
        assert errors == {}
        for name, session in sessions.items():
            frame = _frame_of(session)
            own_trace = session._driver._on_host
            owner, trace, top = seen[(name, "load")]
            assert owner is session._owner, name
            assert trace == own_trace, name
            assert top is frame, name
            owner, trace, top = seen[(name, "call")]
            assert owner is session._owner, name
            assert trace == own_trace, name
            assert top is None, name
            # the map the activation body made is this session's frame's
            maps = [r for r in frame._resources if type(r).__name__ == "Map"]
            assert len(maps) == 1 and maps[0]._owner is frame, name
            assert frame._owner is session._owner, name
    finally:
        for session in sessions.values():
            session.unload()


def test_two_sessions_at_once_never_run_a_call_under_the_others_owner(
        hook, no_turn):
    """The issue's probe with the turn bypassed: every call of each session
    says its own owner."""
    calls = 2000
    seen: dict = {}
    ready = [threading.Event(), threading.Event()]
    go = threading.Event()

    def fn() -> int:
        owner = _runtime().session_owner()
        return id(owner) if owner is not None else 0

    hook.fn = fn

    def run(i):
        session = Session()
        session.load(hook.ir, origin={"source": SOURCE})
        ready[i].set()
        go.wait()
        own = other = none = faults = 0
        for _ in range(calls):
            try:
                said = _owner_call(session)
            except Exception:  # noqa: BLE001
                faults += 1
                continue
            if said == id(session._owner):
                own += 1
            elif said == 0:
                none += 1
            else:
                other += 1
        seen[i] = (own, other, none, faults)
        session.unload()

    crew = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
    for i, thread in enumerate(crew):
        thread.start()
        assert ready[i].wait(120)
    go.set()
    for thread in crew:
        thread.join()
    assert seen == {0: (calls, 0, 0, 0), 1: (calls, 0, 0, 0)}


def test_a_task_started_by_an_activation_body_keeps_the_bodys_frame():
    """A map made by a task an activation body started, after the body's step
    ended, is attributed to that activation: the frame travels with the
    task's copied context, and leaves the body's own context with the step."""
    import asyncio

    runtime = _runtime()

    class _Ctx:
        """The minimum a `Frame` reads off its context here."""

    frame = runtime.Frame(_Ctx(), "Agent")
    seen: dict = {}

    async def child():
        await asyncio.sleep(0)
        seen["map"] = runtime.Map.new()

    def body():
        seen["task"] = asyncio.ensure_future(child())
        return
        yield  # noqa: unreachable, makes this the generator `_tracked` wraps

    async def drive():
        for _ in frame._tracked(body)():
            pass
        seen["after the step"] = runtime._ACTIVATING.get()
        await seen["task"]

    asyncio.run(drive())
    assert seen["after the step"] == ()
    assert seen["map"]._owner is frame
    assert frame._resources == [seen["map"]]
    assert runtime._ACTIVATING.get() == ()
