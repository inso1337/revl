"""Awaitable Session teardown from an async host (roadmap item 524).

The synchronous lifecycle verbs drive the session's own loop through
`run_until_complete`. A host already inside its own running loop cannot call
them — asyncio refuses to nest a second loop on one thread — so it conserves
the Session rather than risk a half-torn-down composition. `Session.aclose`
is the supported async route: it offloads the loop-bound disposal to a worker
thread, awaits it, and returns a structured settlement that keeps requested
verdict, native cleanup, unresolved ownership and release-permission distinct.

These tests need a real runtime (owned resources actually acquired and
disposed), so the cordis-py gate is a per-test marker rather than a
module-level `importorskip`, matching `test_mcp_session.py`.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the awaitable-teardown tests need the cordis-py runtime — install "
           "it with `sh backends/python/setup.sh`, then run this file under "
           "`backends/python/.venv/bin/pytest`",
)

CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""


def _loaded_session() -> Session:
    """A fresh Session with an owned resource live (the `Map` under an `undo`),
    so teardown has real async disposal to await."""
    session = Session()
    session.load(compile_source(CACHE, "<awaitable>.rvl"))
    session.call("cache", "size")   # spin the loop, acquire the resource
    return session


# ---------------------------------------------------------------- scenario 1

# the deliberate `unload()` below raises mid-`run_until_complete`, leaving its
# `_dispose_all` coroutine un-awaited — an expected artifact of demonstrating
# the very failure `aclose` exists to avoid, not a leak under test.
@pytest.mark.filterwarnings("ignore:coroutine '_Driver._dispose_all':RuntimeWarning")
@needs_runtime
def test_aclose_from_running_host_loop_settles_cleanly():
    """Close a real Session from a running host loop with no nested
    `run_until_complete` failure, and prove the disposal actually happened."""
    session = _loaded_session()

    async def host() -> dict:
        # The synchronous route is exactly what an async host cannot do:
        with pytest.raises(RuntimeError, match="another loop is running"):
            session.unload()
        # The awaitable route succeeds from the same running loop.
        return await session.aclose()

    result = asyncio.run(host())

    assert result["closed"] is True
    assert result["requestedVerdict"] == "commit"       # implicit terminal commit
    assert result["disposal"] == {"invoked": True, "returned": True,
                                   "failed": None, "cancelled": False}
    assert result["nativeCleanupComplete"] is True
    assert result["settled"] is True
    assert result["releaseOwnership"] is True
    assert result["noResidue"] is True
    assert session.loaded is False                       # driver released


# ---------------------------------------------------------------- scenario 3

@needs_runtime
def test_aclose_surfaces_failure_rather_than_verified_success():
    """A disposal that raises must expose unresolved/failure state, retain the
    Session, and never read back as settled."""
    session = _loaded_session()
    real_dispose_all = session._driver._dispose_all

    async def boom(_ir):
        raise RuntimeError("inverse blew up")

    session._driver._dispose_all = boom   # inject a disposal fault

    result = asyncio.run(session.aclose())

    assert result["closed"] is False
    assert result["settled"] is False
    assert result["nativeCleanupComplete"] is False
    assert result["releaseOwnership"] is False
    assert result["disposal"]["returned"] is False
    assert "inverse blew up" in result["unresolved"]["error"]
    assert result["unresolved"]["liveComponents"] == ["MemCache"]
    # ownership is retained: the composition is still loaded for the host to
    # inspect/strand/reconcile, not silently dropped on an ambiguous failure.
    assert session.loaded is True

    # a real teardown still works once the fault is cleared: the failed attempt
    # genuinely finished (it was not left in-flight), so clearing the retained
    # future permits a fresh close, and the sync path still works too.
    session._driver._dispose_all = real_dispose_all
    session._teardown_future = None
    session.unload()


# ---------------------------------------------------------------- scenario 4

@needs_runtime
def test_duplicate_and_rejoined_callers_share_one_teardown():
    """Two concurrent callers (and a re-entrant caller) join ONE retained
    attempt: the owned disposal fires exactly once, no duplicate pool release."""
    session = _loaded_session()
    driver = session._driver
    original = driver._dispose_all
    calls = {"n": 0}

    async def counting(ir):
        calls["n"] += 1
        return await original(ir)

    driver._dispose_all = counting

    async def host():
        a, b = await asyncio.gather(session.aclose(), session.aclose())
        # a third caller after completion reads the same settled result back.
        c = await session.aclose()
        return a, b, c

    a, b, c = asyncio.run(host())

    assert calls["n"] == 1                    # disposed once, never duplicated
    assert a is b is c                         # all three joined the one attempt
    assert a["closed"] is True and a["settled"] is True
    assert session.loaded is False


# ---------------------------------------------------------------- scenario 5

@needs_runtime
def test_closing_one_session_leaves_an_independent_one_running():
    """Two independent Sessions: closing A neither stops nor freezes B."""
    a = _loaded_session()
    b = _loaded_session()

    async def host():
        closed = await a.aclose()
        # B is untouched inside the same host loop: still loaded, its own loop
        # neither driven nor frozen by A's teardown.
        assert b.loaded is True
        assert b._loop is not a._loop
        return closed

    closed = asyncio.run(host())

    assert closed["closed"] is True
    assert a.loaded is False
    # and B still actually answers on its own loop once the host loop is idle.
    assert b.loaded is True
    assert b.call("cache", "size")["result"] == 0

    b.unload()


# ---------------------------------------------------------------- scenario 6

@needs_runtime
def test_aclose_refuses_shared_host_loop_before_any_effect():
    """When the session's runtime loop IS the caller's running loop, the owned
    -resource settlement contract is unmet: refuse before any terminal effect,
    keeping the composition loaded."""
    session = _loaded_session()

    async def host():
        session._loop = asyncio.get_running_loop()   # force the shared-loop shape
        with pytest.raises(SessionError, match="own a loop distinct"):
            await session.aclose()
        # nothing was torn down — the guard fired before any effect.
        assert session.loaded is True

    asyncio.run(host())

    # restore an independent loop and tear down normally.
    session._loop = asyncio.new_event_loop()
    session.unload()


# ------------------------------------------------------- guards / compatibility

@needs_runtime
def test_aclose_on_nothing_loaded_refuses_cleanly():
    with pytest.raises(SessionError, match="nothing is loaded"):
        asyncio.run(Session().aclose())


@needs_runtime
def test_sync_unload_semantics_are_unchanged():
    """Scenario 7: the ordinary synchronous lifecycle stays byte-compatible —
    a plain unload is still the clean implicit commit, no automatic abort."""
    session = Session()
    session.load(compile_source(CACHE, "<compat>.rvl"))
    session.call("cache", "size")
    report = session.unload()
    assert report["unloaded"] is True
    assert report["noResidue"] is True
    assert "requestedVerdict" not in report      # the async envelope is aclose-only
    assert session.loaded is False
