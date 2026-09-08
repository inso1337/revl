"""Conformance for the service lifecycle contract (roadmap item 460, issue #723).

Executes the full contract sequence on the REAL cordis-py runtime, one leg per
named transition of docs/lifecycle-contract.md:

    serving
      -> cancel-requested vs cancel-completed
      -> owned-after-failure
      -> recovery-resume

The proof is execution: these drive the native `Session` lifecycle verbs and the
restart path (`revl.recovery.recover`) against a live composition with resources
actually acquired and disposed, mirroring test_session_awaitable_teardown.py and
test_crash_recovery.py. Without the cordis-py runtime installed
(`sh backends/python/setup.sh`) they skip with a reason, never reported as a pass.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402
from revl.recovery import recover  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the lifecycle contract is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and run "
           "this file under `backends/python/.venv/bin/python -m pytest`",
)

# A consumer (App, disposed FIRST) over a provider (MemCache, disposed SECOND).
# Each owns a `Map` under an `undo`, so both are real original disposers and the
# LIFO order lets a fault at the consumer leave the provider unattempted — the
# shape section 3 of the contract needs to tell a failed resource from an owned,
# unattempted one.
TWO = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
service Front { fn hit() -> Int }
component App requires cache: Cache provides front: Front {
  let h = effect Map.new() undo h.drop()
  provide front { fn hit() = cache.size() }
}
"""

# A successor that still DECLARES `front` but requires `cache` from a provider it
# no longer carries: its App fiber comes up PENDING (unmet requirement), which
# item 372 makes non-raising. Swapping gen N to it must be REFUSED by the
# post-activation health gate, leaving gen N serving (owned-after-failure at
# admission time).
BROKEN_SUCCESSOR = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
service Front { fn hit() -> Int }
component App requires cache: Cache provides front: Front {
  let h = effect Map.new() undo h.drop()
  provide front { fn hit() = cache.size() }
}
"""

# The recovery leg reuses the shipped user_cache example: a `put` emits a real
# `db.execute` process crossing, so a crash mid-activation rolls back with that
# crossing named as honest residue, and a completed activation rolls forward.
USER_CACHE = (ROOT / "examples" / "user_cache.rvl").read_text(encoding="utf-8")
PG_CONFIG = {"PgDatabase": {"url": "postgres://lifecycle-723"}}


def _loaded_two() -> Session:
    session = Session()
    session.load(compile_source(TWO, "<723-two>.rvl"))
    session.call("front", "hit")   # spin the loop; acquire both resources
    return session


async def _raise_native_fault():
    raise RuntimeError("native disposer fault")


# ---------------------------------------------------------------------------
# 1. serving
# ---------------------------------------------------------------------------


@needs_cordis
def test_serving_is_a_live_provider_not_merely_an_active_fiber():
    """A key is serving iff loaded, its provider fiber is ACTIVE, and the key
    resolves to a live provider in ROOT (`providedKeys`)."""
    session = _loaded_two()
    try:
        state = session.state()
        assert state["loaded"] is True
        fibers = {c["name"]: c["state"] for c in state["components"]}
        assert fibers == {"MemCache": "ACTIVE", "App": "ACTIVE"}
        assert state["providedKeys"] == ["cache", "front"]
        # serving means it actually answers.
        assert session.call("front", "hit")["result"] == 0
    finally:
        if session.loaded:
            session.unload()


# ---------------------------------------------------------------------------
# 2. cancellation: requested -> completed (the clean path)
# ---------------------------------------------------------------------------


@needs_cordis
def test_cancellation_that_completes_releases_ownership_with_no_residue():
    """A clean cancellation is two transitions: the disposal is INVOKED
    (requested), then the attempt settles RELEASED (completed) — ownership
    dropped, no residue, and `teardown_disposition` reads back `released`."""
    session = _loaded_two()

    result = asyncio.run(session.aclose())

    # requested: the disposal was actually invoked and returned.
    assert result["disposal"] == {"invoked": True, "returned": True,
                                   "failed": None, "cancelled": False}
    # completed: settled clean, ownership released, no residue.
    assert result["closed"] is True
    assert result["settled"] is True
    assert result["releaseOwnership"] is True
    assert result["noResidue"] is True
    assert {r["component"]: r["outcome"] for r in result["resources"]} == {
        "App": "returned", "MemCache": "returned"}
    assert session.loaded is False
    assert session.teardown_disposition()["attempt"] == "released"


# ---------------------------------------------------------------------------
# 3. owned after a failure (a requested cancellation that does NOT complete)
# ---------------------------------------------------------------------------


@needs_cordis
def test_a_native_disposer_fault_retains_ownership_and_names_what_is_owed():
    """A native fault at the first original disposer settles the attempt
    `unresolved`: ownership is NOT released, the faulted and the unattempted
    resources are named distinctly, the session stays loaded and inspectable, and
    `strand_teardown` then accepts the owed state as stranded."""
    session = _loaded_two()
    # App is disposed FIRST (consumer before provider); fault its disposer.
    session._driver.fibers["App"].dispose = _raise_native_fault

    result = asyncio.run(session.aclose())

    assert result["closed"] is False
    assert result["settled"] is False
    assert result["releaseOwnership"] is False
    assert result["nativeCleanupComplete"] is False
    assert {r["component"]: r["outcome"] for r in result["resources"]} == {
        "App": "failed", "MemCache": "owned"}
    assert result["unresolved"]["ownedResources"] == ["App", "MemCache"]
    assert result["unresolved"]["liveComponents"] == ["MemCache"]
    assert "native disposer fault" in result["unresolved"]["error"]

    # retained: still loaded, ownership NOT dropped, inspectable.
    assert session.loaded is True
    disposition = session.teardown_disposition()
    assert disposition["attempt"] == "unresolved"
    assert (disposition["settlement"]["unresolved"]["ownedResources"]
            == ["App", "MemCache"])

    # the host's explicit terminal move: accept the owed state as stranded.
    stranded = session.strand_teardown()
    assert stranded["stranded"] is True
    assert stranded["unresolved"]["ownedResources"] == ["App", "MemCache"]


@needs_cordis
def test_a_refused_swap_keeps_the_previous_generation_serving():
    """Owned-after-failure at admission time: a successor whose activation is not
    healthy (an unmet requirement leaves its fiber PENDING) is refused by the
    post-activation health gate. `swap` rolls back to gen N, which keeps serving
    with its resources intact — the change is cancelled, not the service."""
    session = _loaded_two()
    try:
        with pytest.raises(SessionError) as excinfo:
            session.swap(compile_source(BROKEN_SUCCESSOR, "<723-broken>.rvl"))
        assert "swap rejected" in str(excinfo.value)
        assert "rolled back to the previous generation" in str(excinfo.value)

        # gen N is untouched and still serving both keys.
        state = session.state()
        assert state["loaded"] is True
        assert state["providedKeys"] == ["cache", "front"]
        assert session.call("front", "hit")["result"] == 0
    finally:
        if session.loaded:
            session.unload()


# ---------------------------------------------------------------------------
# 4. recovery: how a restart resumes
# ---------------------------------------------------------------------------


@needs_cordis
def test_a_completed_activation_rolls_forward_and_resumes_the_generation(tmp_path):
    """`activation-complete` present: recover rolls the persisted generation
    FORWARD, re-admitting it through item 15's restore. The verdict resumes and
    the fresh session comes up loaded."""
    import replay  # noqa: PLC0415 — backends/python, cordis-only

    session = Session()
    session.load(compile_source(USER_CACHE, "<723-recovery>.rvl"), PG_CONFIG,
                 record=True, origin={"source": USER_CACHE})
    snap = session.snapshot()
    session.unload()   # the process "died"; only the snapshot + WAL survive

    path = str(tmp_path / "done.wal")
    with replay.WriteAheadLog(path, ir=session.ir or {}, generation=1) as wal:
        wal.commit_activation(["PgDatabase", "UserCache"])

    fresh = Session()
    try:
        report = recover(path, session=fresh, snapshot=snap)
        assert report["verdict"] == "rolled-forward"
        assert report["resumed"] is True
        assert report["resume"]["resumedForCrashRecovery"] is True
        assert fresh.loaded
    finally:
        if fresh.loaded:
            fresh.unload()


@needs_cordis
def test_a_crash_mid_activation_rolls_back_and_names_the_owed_crossing(tmp_path):
    """`activation-complete` absent: the real `db.execute` emission captured on a
    live accumulator is a process crossing with no inverse. A crash rolls back
    and reports it as honest residue rather than pretending an undo ran — the
    durable form of section 3's owned-after-failure."""
    import replay  # noqa: PLC0415 — backends/python, cordis-only

    session = Session()
    session.load(compile_source(USER_CACHE, "<723-rollback>.rvl"), PG_CONFIG,
                 record=True)
    session.call("cache", "put", ["k", "v"])   # emits a real db.execute crossing

    path = str(tmp_path / "crash.wal")
    tl = session.recorder.timeline("UserCache")
    with replay.WriteAheadLog(path, ir=session.ir, generation=1) as wal:
        wal.append_timeline(tl)   # NO commit -> crashed mid-activation
    session.unload()

    loaded = replay.WriteAheadLog.read(path)
    assert loaded["complete"] is False
    report = recover(path)
    assert report["verdict"] == "rolled-back"
    assert any(e["kind"] == "emission" for e in report["unreconstructible"])
