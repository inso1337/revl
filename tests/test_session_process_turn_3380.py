"""One session runs at a time per process (issue #3380).

The Python runtime keeps the running session's owner in one process-wide slot
(`runtime._SESSION_OWNER`). Two sessions driven from two threads used to
interleave, so a call of one ran part of the time with the other's owner in
the slot. Each session step now takes the process's session turn: another
thread waits (30 s, then a named refusal), the holding thread is refused at
once, and `estop` does not wait.
"""

import asyncio
import importlib.util
import sys
import threading
import time
import types

import pytest

from revl import compile_files
from revl.mcp import session as session_mod
from revl.mcp.session import Session, SessionError

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a live Session needs the cordis-py runtime")

HOOK = "_revl_3380_hook"

SOURCE = f"""
extern emission[probe] fn whose() -> Int = @py {{
  import {HOOK}
  return {HOOK}.fn()
}}
service Echo {{ emission[probe] fn owner() -> Int }}
component Plain provides echo: Echo {{
  provide echo {{
    fn owner() {{
      let o = emit whose()
      return o
    }}
  }}
}}
"""


def _owner_id() -> int:
    """The runtime's own look: the session it says is running."""
    owner = sys.modules["runtime"]._SESSION_OWNER
    return id(owner) if owner is not None else 0


@pytest.fixture
def hook(tmp_path):
    module = types.ModuleType(HOOK)
    module.fn = _owner_id
    sys.modules[HOOK] = module
    path = tmp_path / "echo.rvl"
    path.write_text(SOURCE)
    module.ir = compile_files([str(path)])
    try:
        yield module
    finally:
        del sys.modules[HOOK]


def _loaded(ir) -> Session:
    session = Session()
    session.load(ir, origin={"source": SOURCE})
    return session


def _owner_call(session):
    return session.call("echo", "owner", [], raw=True)["result"]


def _holding(session, release: threading.Event, entered: threading.Event):
    """A thread whose call on `session` holds the turn until `release`."""
    out = {}

    def run():
        try:
            out["result"] = _owner_call(session)
        except BaseException as exc:  # noqa: BLE001 - read by the test
            out["error"] = exc

    thread = threading.Thread(target=run, name="holder-3380")
    thread.start()
    assert entered.wait(30)
    return thread, out


def _blocking_fn(release, entered):
    def fn():
        entered.set()
        assert release.wait(30)
        return _owner_id()
    return fn


def test_two_sessions_on_two_threads_each_see_their_own_owner(hook):
    calls = 3000
    seen = {}
    ready = [threading.Event(), threading.Event()]
    go = threading.Event()

    def run(i):
        session = _loaded(hook.ir)
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


def test_a_second_session_on_the_holding_thread_is_refused_at_once(hook):
    first = _loaded(hook.ir)
    second = _loaded(hook.ir)
    caught = {}

    def fn():
        for name, session in (("second", second), ("first", first)):
            started = time.monotonic()
            try:
                _owner_call(session)
            except SessionError as exc:
                caught[name] = (exc, time.monotonic() - started)
        return _owner_id()

    hook.fn = fn
    try:
        assert _owner_call(first) == id(first._owner)
    finally:
        hook.fn = _owner_id
    for name in ("second", "first"):
        exc, waited = caught[name]
        assert exc.code == "SESSION_BUSY"
        assert waited < 1.0
        message = str(exc)
        assert f"session {id(first):#x}" in message      # the holder
        assert threading.current_thread().name in message
        assert "same thread" in message
    # the turn was given back: both sessions run again
    assert _owner_call(second) == id(second._owner)
    assert _owner_call(first) == id(first._owner)
    first.unload()
    second.unload()


def test_a_waiting_thread_gives_up_after_the_timeout_naming_the_holder(
        hook, monkeypatch):
    monkeypatch.setattr(session_mod, "_TURN_TIMEOUT", 0.3)
    first = _loaded(hook.ir)
    second = _loaded(hook.ir)
    release, entered = threading.Event(), threading.Event()
    hook.fn = _blocking_fn(release, entered)
    thread, out = _holding(first, release, entered)
    try:
        started = time.monotonic()
        with pytest.raises(SessionError) as info:
            _owner_call(second)
        waited = time.monotonic() - started
    finally:
        hook.fn = _owner_id
        release.set()
        thread.join()
    assert 0.25 <= waited < 5.0
    assert info.value.code == "SESSION_BUSY"
    message = str(info.value)
    assert "waited 0.3 s" in message
    assert f"session {id(first):#x} on thread 'holder-3380'" in message
    assert out == {"result": id(first._owner)}
    assert _owner_call(second) == id(second._owner)
    first.unload()
    second.unload()


def test_estop_does_not_wait_for_the_turn(hook):
    """A guard, not a red on base: there it fails only through the
    AttributeError on `_TURN_HOLDER`, which does not exist before #3380."""
    first = _loaded(hook.ir)
    second = _loaded(hook.ir)
    release, entered = threading.Event(), threading.Event()
    hook.fn = _blocking_fn(release, entered)
    thread, _ = _holding(first, release, entered)
    runtime = sys.modules["runtime"]
    try:
        started = time.monotonic()
        halted = second.estop("test halt", operator="test")
        waited = time.monotonic() - started
        assert session_mod._TURN_HOLDER["thread"] == thread.ident  # still held
    finally:
        hook.fn = _owner_id
        release.set()
        thread.join()
        runtime.clear_estop()             # the halt is process-wide
        runtime.arm_estop_latch(None)
    assert waited < 5.0
    assert halted.get("halted", True) is not False


def test_the_offloaded_teardown_completes_beside_a_call(hook):
    first = _loaded(hook.ir)
    second = _loaded(hook.ir)
    release, entered = threading.Event(), threading.Event()
    hook.fn = _blocking_fn(release, entered)
    thread, out = _holding(first, release, entered)
    try:
        threading.Timer(0.3, release.set).start()
        result = asyncio.run(second.aclose())
    finally:
        hook.fn = _owner_id
        release.set()
        thread.join()
    assert result["settled"] is True
    assert out == {"result": id(first._owner)}
    first.unload()


def test_an_unload_on_another_thread_never_clears_a_running_calls_owner(hook):
    """`_reset` clears the owner slot. It takes the turn, so a call that is
    mid-method on another thread never sees None (review of #3610). The
    unload's `_reset` waits (up to 0.5 s) for the other session's call to be
    inside its method; on 0029ec7e8 it got there and cleared the slot."""
    first = _loaded(hook.ir)
    second = _loaded(hook.ir)
    unloading, entered, cleared = (threading.Event(), threading.Event(),
                                   threading.Event())

    def fn():
        entered.set()
        cleared.wait(0.5)        # head: the reset waits for the turn instead
        return _owner_id()

    def reset():
        unloading.set()
        entered.wait(0.5)        # head: the unload holds the turn, no entry
        try:
            Session._reset(second)
        finally:
            cleared.set()

    second._reset = reset
    unloader = threading.Thread(target=second.unload)
    unloader.start()
    try:
        assert unloading.wait(30)
        hook.fn = fn
        said = _owner_call(first)
    finally:
        hook.fn = _owner_id
        entered.set()
        unloader.join()
    assert said == id(first._owner)
    assert not second.loaded
    first.unload()


def test_a_refused_unload_or_abort_leaves_the_session_as_it_was(hook):
    first = _loaded(hook.ir)
    second = _loaded(hook.ir)
    owner = second._owner
    caught = []

    def fn():
        for verb in (second.unload, second.abort):
            try:
                verb()
            except SessionError as exc:
                caught.append(exc.code)
        return _owner_id()

    hook.fn = fn
    try:
        assert _owner_call(first) == id(first._owner)
    finally:
        hook.fn = _owner_id
    assert caught == ["SESSION_BUSY", "SESSION_BUSY"]
    assert second.loaded and second._owner is owner
    assert owner._verdict is None
    assert not any(getattr(f, "_aborting", False) for f in owner._registry)
    assert _owner_call(second) == id(owner)
    second.unload()
    first.unload()
