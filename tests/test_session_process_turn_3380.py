"""One session runs at a time per process (issue #3380).

The Python runtime kept the running session's owner in one process-wide slot
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


def _lend():
    """The internal lend (`lend_turn` before the review of #3610)."""
    lend = getattr(session_mod, "_lend_turn", None)
    return (lend or session_mod.lend_turn)()


def _owner_id() -> int:
    """The runtime's own look: the session it says is running."""
    owner = sys.modules["runtime"].session_owner()
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


def test_a_lent_turn_runs_a_joined_workers_session_and_comes_back(hook):
    """`truc ship` boots a scratch session on a worker thread it joins from
    inside its own session's method. `_lend_turn` lets that worker run under the
    held turn, and gives the lender its owner slot back afterwards."""
    import concurrent.futures
    import contextvars

    first = _loaded(hook.ir)
    main = threading.current_thread()
    inner = {}

    def worker():
        second = _loaded(hook.ir)
        inner["said"] = _owner_call(second)
        inner["owner"] = id(second._owner)
        second.unload()

    def fn():
        if threading.current_thread() is not main:
            return _owner_id()
        started = time.monotonic()
        with _lend():
            context = contextvars.copy_context()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(context.run, worker).result()
        inner["waited"] = time.monotonic() - started
        return _owner_id()      # the lender's owner, put back

    hook.fn = fn
    try:
        said = _owner_call(first)
    finally:
        hook.fn = _owner_id
    assert inner["said"] == inner["owner"]
    assert inner["waited"] < 5.0
    assert said == id(first._owner)
    with _lend():   # this thread holds no turn: lends nothing
        pass
    assert session_mod._TURN_HOLDER["thread"] is None
    first.unload()


def test_a_context_copied_in_a_lend_lends_nothing_after_it(hook, monkeypatch):
    """Each lend has a token of its own, retired when the block ends: a worker
    run later in a context copied inside the block waits like any other
    thread, and the lender's owner slot stays the lender's."""
    import contextvars

    monkeypatch.setattr(session_mod, "_TURN_TIMEOUT", 0.3)
    first = _loaded(hook.ir)
    main = threading.current_thread()
    seen = {}

    def worker():
        started = time.monotonic()
        try:
            late = _loaded(hook.ir)
        except SessionError as exc:
            seen["refused"] = (exc.code, time.monotonic() - started)
        else:
            seen["loaded"] = True
            late.unload()

    def fn():
        if threading.current_thread() is not main:
            return _owner_id()
        with _lend():
            stale = contextvars.copy_context()
        thread = threading.Thread(target=stale.run, args=(worker,))
        thread.start()
        thread.join()
        return _owner_id()

    hook.fn = fn
    try:
        said = _owner_call(first)
    finally:
        hook.fn = _owner_id
    assert "loaded" not in seen
    assert seen["refused"][0] == "SESSION_BUSY"
    assert seen["refused"][1] >= 0.25
    assert said == id(first._owner)
    first.unload()


def test_a_second_borrower_and_the_lender_are_refused_at_once(monkeypatch):
    import contextvars

    monkeypatch.setattr(session_mod, "_TURN_TIMEOUT", 0.5)
    borrowed, release = threading.Event(), threading.Event()
    refusals = {}

    def borrower():
        with session_mod._SessionTurn(Session()):
            borrowed.set()
            assert release.wait(30)

    def refused(name):
        started = time.monotonic()
        try:
            session_mod._SessionTurn(Session()).__enter__()
        except SessionError as exc:
            refusals[name] = (exc.code, time.monotonic() - started, str(exc))

    with session_mod._SessionTurn(Session()):
        with _lend():
            context = contextvars.copy_context()
            first = threading.Thread(target=context.copy().run,
                                     args=(borrower,))
            first.start()
            try:
                assert borrowed.wait(30)
                second = threading.Thread(target=context.copy().run,
                                          args=(refused, "second"))
                second.start()
                second.join()
                refused("lender")
            finally:
                release.set()
                first.join()
    assert session_mod._TURN_HOLDER["thread"] is None
    for name in ("second", "lender"):
        code, waited, message = refusals[name]
        assert code == "SESSION_BUSY", message
        assert waited < 0.2, (name, waited)
    assert "already borrowed" in refusals["second"][2]
    assert "lent its session turn" in refusals["lender"][2]


def test_a_borrower_that_raises_gives_the_lender_its_slots_back(hook):
    import concurrent.futures
    import contextvars

    first = _loaded(hook.ir)
    main = threading.current_thread()
    runtime = sys.modules["runtime"]
    seen = {}

    def worker():
        seen["second"] = _loaded(hook.ir)
        raise RuntimeError("the borrower failed")

    def fn():
        if threading.current_thread() is not main:
            return _owner_id()
        try:
            with _lend():
                context = contextvars.copy_context()
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    pool.submit(context.run, worker).result()
        except RuntimeError as exc:
            seen["raised"] = str(exc)
        seen["trace"] = runtime._TRACE.get()
        return _owner_id()

    hook.fn = fn
    try:
        said = _owner_call(first)
    finally:
        hook.fn = _owner_id
    assert seen["raised"] == "the borrower failed"
    assert said == id(first._owner)
    assert seen["trace"] == first._driver._on_host
    seen["second"].unload()
    first.unload()


def test_a_borrower_that_outlives_the_lend_inherits_the_lock(monkeypatch):
    """The orphan handoff: the lend block ends while its borrower is still in
    a step. `_lend_turn` raises, the lock stays taken and passes to the
    borrower, the lender's thread is refused at once meanwhile, and the
    borrower frees the lock when its step ends."""
    import contextvars

    monkeypatch.setattr(session_mod, "_TURN_TIMEOUT", 0.5)
    held = session_mod._TURN_HOLDER
    borrowed, release = threading.Event(), threading.Event()

    def borrower():
        with session_mod._SessionTurn(Session()):
            borrowed.set()
            assert release.wait(30)

    def refused_at_once():
        started = time.monotonic()
        with pytest.raises(SessionError) as info:
            session_mod._SessionTurn(Session()).__enter__()
        assert info.value.code == "SESSION_BUSY"
        assert time.monotonic() - started < 0.2
        return str(info.value)

    thread = None
    try:
        with pytest.raises(SessionError) as ended:
            with session_mod._SessionTurn(Session()):
                try:
                    with _lend():
                        context = contextvars.copy_context()
                        thread = threading.Thread(target=context.run,
                                                  args=(borrower,))
                        thread.start()
                        assert borrowed.wait(30)
                except SessionError:
                    # between the raise and the lender's exit
                    assert "lent its session turn" in refused_at_once()
                    raise
        assert ended.value.code == "SESSION_BUSY"
        assert "must be joined inside the lend" in str(ended.value)
        # the lender's turn ended; the lock passed to the running borrower
        assert session_mod._TURN_LOCK.locked()
        assert held["orphaned"] is True
        assert held["thread"] == thread.ident
        assert "lent its session turn" in refused_at_once()
    finally:
        release.set()
        if thread is not None:
            thread.join()
    assert held["thread"] is None and held["orphaned"] is False
    assert held["lender"] is None and held["lend"] is None
    assert not session_mod._TURN_LOCK.locked()
    with session_mod._SessionTurn(Session()):
        assert held["thread"] == threading.get_ident()
    assert not session_mod._TURN_LOCK.locked()
