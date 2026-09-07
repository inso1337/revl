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


# A two-component composition: `App` (a consumer, disposed FIRST — before its
# provider) over `MemCache` (a provider, disposed SECOND). Each owns a `Map`
# under an `undo`, so both are original disposers, and the LIFO teardown order
# lets a fault at the consumer leave the provider UNATTEMPTED — the exact shape
# item 628 needs to tell a failed resource from an unattempted one.
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


def _loaded_two() -> Session:
    session = Session()
    session.load(compile_source(TWO, "<two-owner>.rvl"))
    session.call("front", "hit")    # spin the loop, acquire both resources
    return session


async def _raise_native_fault():
    raise RuntimeError("native disposer fault")


async def _noop_dispose():
    """A disposer that RETURNS without actually releasing its effect — the
    aggregate completes, but R4 sees the leaked disposable as residue."""
    return None


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


# =====================================================================
# item 625 — awaitable AUDITED abort / commit-confirm / review-confirm
# =====================================================================

@needs_runtime
def test_aabort_from_running_host_loop_is_the_audited_abort_twin():
    """`aabort` is the async twin of `abort`: from a running host loop it marks
    the verdict abort, replays the inverses on the session's own loop, writes the
    `aborted` record, and settles — the implicit-commit default of `aclose` is
    untouched (that is `unload`'s twin; this is the explicit WAL-marked verdict)."""
    session = _loaded_session()

    result = asyncio.run(session.aabort())

    assert result["requestedVerdict"] == "abort"
    assert result["aborted"] is True
    assert "replayed" in result
    assert result["disposal"] == {"invoked": True, "returned": True,
                                   "failed": None, "cancelled": False}
    assert result["settled"] is True
    assert result["releaseOwnership"] is True
    assert session.loaded is False


@needs_runtime
def test_acommit_confirm_preserves_the_durable_wal_record_order(tmp_path,
                                                                monkeypatch):
    """`acommit_confirm` is the async twin of `commit_confirm`: it writes exactly
    the Decision-3 order — `commit-approved`, then each `flushed`, then the ONE
    `discharge`, then `activation-complete` — the same as the synchronous path,
    with the frame discharge offloaded to the session's own loop."""
    backend = str(ROOT / "backends" / "python")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import replay  # noqa: PLC0415

    wal_path = str(tmp_path / "acommit.wal")
    real_instrument = replay.Recorder.instrument

    def _open_then_instrument(self, *args, **kwargs):
        self.open_wal(wal_path, generation=1)
        return real_instrument(self, *args, **kwargs)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then_instrument)

    # (a) a per-call witnessed rename that discharges on commit + (b) two
    # deferred emissions that flush FIFO — so the WAL carries flushed+discharge
    # between approved and activation-complete.
    src = (
        "type Stash = { path: Str, bak: Str }\n"
        "type FsError = { code: Str }\n"
        "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
        "    import os\n"
        "    if os.path.exists(w['bak']): os.replace(w['bak'], w['path'])\n"
        "    return\n"
        "}\n"
        "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
        " undo unstash(result) = @py {\n"
        "    import os\n"
        "    bak = p + '.bak'\n"
        "    os.replace(p, bak)\n"
        "    return Ok({'path': p, 'bak': bak})\n"
        "}\n"
        "extern emission deferred fn deliver(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f: _f.write('deliver:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { emission fn stash(p: Str)\n"
        "              emission fn enqueue(sink: Str, msg: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn stash(p) { effect stash_path(p) }\n"
        "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
        "  }\n"
        "}\n"
    )
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("deliverable", encoding="utf-8")
    sink = str(tmp_path / "sink.log")

    session = Session()
    session.load(compile_source(src, "<acommit>.rvl"), record=True)
    session.call("ops", "stash", [str(artifact)])
    session.call("ops", "enqueue", [sink, "q0"])
    session.call("ops", "enqueue", [sink, "q1"])

    async def host():
        manifest = session.commit()
        return await session.acommit_confirm(manifest["hash"])

    result = asyncio.run(host())
    assert result["committed"] is True
    assert result["settled"] is True
    assert result["releaseOwnership"] is True
    assert session.loaded is False

    written = replay.WriteAheadLog.read(wal_path)
    order = [r["record"] for r in written["records"]
             if r["record"] in ("commit-approved", "flushed", "discharge",
                                 "activation-complete")]
    assert order == ["commit-approved", "flushed", "flushed", "discharge",
                     "activation-complete"]


@needs_runtime
def test_acommit_confirm_refuses_a_stale_hash_before_any_terminal_effect():
    """A changed/stale review token refuses BEFORE any terminal effect: the
    session stays loaded, no retained attempt is created, and a fresh confirm
    with the real manifest hash still commits."""
    session = _loaded_session()

    async def host():
        refused = await session.acommit_confirm("sha256:deadbeef")
        assert refused["refused"] is True
        assert session.loaded is True                # nothing torn down
        assert session._teardown_future is None      # no attempt retained
        # the real hash still commits from the same running loop.
        manifest = session.commit()
        return await session.acommit_confirm(manifest["hash"])

    committed = asyncio.run(host())
    assert committed["committed"] is True
    assert committed["releaseOwnership"] is True
    assert session.loaded is False


@needs_runtime
def test_aconfirm_verdict_refuses_a_stale_token_then_enacts_the_audited_abort():
    """The awaitable review-confirm refuses a drifted/stale verdict token before
    any terminal effect, then — given the exact review token — enacts the audited
    abort through the same offload seam."""
    session = _loaded_session()

    async def host():
        stale = await session.aconfirm_verdict("revl-verdict:abort:bogus")
        assert stale["refused"] is True
        assert session.loaded is True                # refused before any effect
        review = session.prepare_verdict("abort")
        return await session.aconfirm_verdict(review.token)

    result = asyncio.run(host())
    assert result["aborted"] is True
    assert result["requestedVerdict"] == "abort"
    assert session.loaded is False


# =====================================================================
# item 628 — original-resource settlement + retained unresolved ownership
# =====================================================================

@needs_runtime
def test_clean_close_settles_each_original_resource_and_releases():
    """A clean close ties settlement to the ORIGINAL disposers: every provision
    ran to `returned`, nothing is owed, ownership releases, and the disposition
    reads back `released`."""
    session = _loaded_two()

    result = asyncio.run(session.aclose())

    assert result["closed"] is True
    assert result["settled"] is True
    assert result["releaseOwnership"] is True
    assert {r["component"]: r["outcome"] for r in result["resources"]} == {
        "App": "returned", "MemCache": "returned"}
    assert result["unresolved"]["ownedResources"] == []
    assert session.loaded is False
    assert session.teardown_disposition()["attempt"] == "released"


@needs_runtime
def test_native_fault_names_the_failed_resource_and_retains_ownership():
    """A real native fault at one ORIGINAL disposer names that resource as
    `failed` AND keeps the unattempted provider distinct — an inventory that
    `driver.fibers` alone cannot give (the failed fiber was popped before its
    `dispose` was awaited). Ownership is retained, not released, and stays
    inspectable through `teardown_disposition`."""
    session = _loaded_two()
    # App is disposed FIRST (consumer before provider); fault its disposer.
    session._driver.fibers["App"].dispose = _raise_native_fault

    result = asyncio.run(session.aclose())

    assert result["closed"] is False
    assert result["settled"] is False
    assert result["releaseOwnership"] is False
    assert result["nativeCleanupComplete"] is False
    outcomes = {r["component"]: r["outcome"] for r in result["resources"]}
    assert outcomes == {"App": "failed", "MemCache": "owned"}
    # the failed resource is named even though it is gone from `fibers`.
    assert result["unresolved"]["ownedResources"] == ["App", "MemCache"]
    assert result["unresolved"]["liveComponents"] == ["MemCache"]
    assert "native disposer fault" in result["unresolved"]["error"]
    # retained: still loaded, still inspectable, ownership NOT dropped.
    assert session.loaded is True
    disposition = session.teardown_disposition()
    assert disposition["attempt"] == "unresolved"
    assert (disposition["settlement"]["unresolved"]["ownedResources"]
            == ["App", "MemCache"])

    session.strand_teardown()   # release the retained attempt for teardown


@needs_runtime
def test_returned_but_r4_residue_withholds_release_and_retains():
    """A disposal that RETURNS but leaves R4 residue (a disposer that returned
    without releasing its effect) must NOT read back as released: `closed` and
    `nativeCleanupComplete` are true, but `settled`/`releaseOwnership` are false
    and the composition is retained so the residue stays inspectable. Consumers
    must not equate `closed`/`settled` alone with safe external-resource release."""
    session = _loaded_two()
    session._driver.fibers["App"].dispose = _noop_dispose   # returns, leaks effect

    result = asyncio.run(session.aclose())

    assert result["closed"] is True
    assert result["nativeCleanupComplete"] is True
    assert result["settled"] is False
    assert result["releaseOwnership"] is False
    assert result["noResidue"] is False
    assert result["unresolved"]["checks"]                    # a failed R4 check
    assert session.loaded is True                            # retained, inspectable
    assert session.teardown_disposition()["attempt"] == "unresolved"

    session.strand_teardown()


@needs_runtime
def test_waiter_cancellation_and_rejoin_share_one_attempt():
    """A cancelled waiter plus a re-issued close join ONE retained attempt: the
    owned disposal fires exactly once (no duplicate pool release), and the rejoin
    observes the same settlement rather than re-running the terminal effects."""
    session = _loaded_session()
    driver = session._driver
    original = driver._dispose_all
    calls = {"n": 0}

    async def slow(ir):
        calls["n"] += 1
        await asyncio.sleep(0.05)     # give the waiter a window to cancel
        return await original(ir)

    driver._dispose_all = slow

    async def host():
        task = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)        # let aclose retain its attempt and start
        task.cancel()
        try:
            await task
            raise AssertionError("the waiter should have cancelled")
        except asyncio.CancelledError:
            pass
        return await session.aclose()  # rejoin the SAME retained attempt

    result = asyncio.run(host())
    assert calls["n"] == 1            # disposed once, never duplicated
    assert result["closed"] is True and result["settled"] is True
    assert session.loaded is False


@needs_runtime
def test_rearm_permits_a_fresh_attempt_after_a_transient_fault():
    """The explicit, host-driven re-arm (no automatic retry): a retained-but-
    unresolved attempt can be re-armed so a fresh `aclose` re-attempts the owned
    disposal once the fault clears — and a clean re-attempt then releases."""
    session = _loaded_two()
    real = session._driver._dispose_all

    async def boom(_ir):
        raise RuntimeError("transient aggregate fault")

    session._driver._dispose_all = boom
    first = asyncio.run(session.aclose())
    assert first["closed"] is False and session.loaded is True

    rearmed = session.rearm_teardown()
    assert rearmed["rearmed"] is True
    assert session._teardown_future is None          # a fresh attempt is allowed

    session._driver._dispose_all = real              # the fault clears
    second = asyncio.run(session.aclose())
    assert second["closed"] is True
    assert second["releaseOwnership"] is True
    assert session.loaded is False


@needs_runtime
def test_rearm_and_strand_refuse_when_there_is_nothing_owed():
    """Both failure-disposition verbs refuse when there is no retained attempt or
    the attempt released cleanly — they act only on genuinely unresolved state."""
    session = _loaded_session()
    with pytest.raises(SessionError, match="no retained teardown"):
        session.rearm_teardown()
    with pytest.raises(SessionError, match="no retained teardown"):
        session.strand_teardown()

    asyncio.run(session.aclose())                    # clean release
    with pytest.raises(SessionError, match="cleanly"):
        session.rearm_teardown()
    with pytest.raises(SessionError, match="cleanly"):
        session.strand_teardown()


@needs_runtime
def test_strand_accepts_unresolved_ownership_as_a_terminal_disposition():
    """The explicit terminal disposition: accept the retained attempt's
    unresolved ownership as stranded (owed, not released), drop the composition,
    and clear the attempt — no retry, no detached-task adoption."""
    session = _loaded_two()
    session._driver.fibers["App"].dispose = _raise_native_fault
    asyncio.run(session.aclose())
    assert session.loaded is True

    stranded = session.strand_teardown()
    assert stranded["stranded"] is True
    assert stranded["unresolved"]["ownedResources"] == ["App", "MemCache"]
    assert session.loaded is False                   # composition dropped
    assert session.teardown_disposition()["attempt"] == "none"


@needs_runtime
def test_a_retained_failure_does_not_touch_an_independent_session():
    """A retained failure on session A neither stops nor freezes an independent
    session B — B still answers on its own loop and tears down cleanly."""
    a = _loaded_two()
    b = _loaded_session()
    a._driver.fibers["App"].dispose = _raise_native_fault

    async def host():
        failed = await a.aclose()
        assert failed["closed"] is False
        assert b.loaded is True and b._loop is not a._loop
        return failed

    asyncio.run(host())
    assert a.loaded is True                           # A retained
    assert b.call("cache", "size")["result"] == 0     # B untouched
    b.unload()
    a.strand_teardown()


# ------------- item 628 residual: original-disposer failure + explicit re-arm

@needs_runtime
def test_rearm_after_original_disposer_failure_keeps_the_original_and_evidence():
    """The residual item-628 contract gap: an ORIGINAL disposer failure followed
    by an explicit re-arm must not lose the original object or its prior
    settlement evidence.

    A single original disposer (`App`) is faulted at the fiber, so it is popped
    before its `dispose` is awaited (`run.py`), leaving the failed fiber outside
    `driver.fibers`. The host inspects the retained attempt, re-arms it, and
    closes again WITHOUT clearing the fault. The re-attempt must target the SAME
    original disposer (not read the resource as `absent` from a fresh `fibers`
    scan and release ownership), so with the fault still live `App` stays owed
    and ownership is NOT released. The prior `failed` evidence must remain
    inspectable across the re-arm and the second attempt."""
    session = _loaded_two()
    app_fiber = session._driver.fibers["App"]
    real_app_dispose = app_fiber.dispose                     # keep to restore later
    app_fiber.dispose = _raise_native_fault                  # fault the original

    first = asyncio.run(session.aclose())
    assert first["closed"] is False
    outcomes = {r["component"]: r["outcome"] for r in first["resources"]}
    assert outcomes == {"App": "failed", "MemCache": "owned"}
    assert session.loaded is True
    # the failed ORIGINAL object is retained (popped from `fibers`, not lost).
    assert "App" in session._driver._retained_disposers

    # re-arm must retain the prior unresolved evidence, not erase it.
    rearmed = session.rearm_teardown()
    assert rearmed["rearmed"] is True
    assert rearmed["priorSettlement"]["unresolved"]["ownedResources"] == [
        "App", "MemCache"]
    disposition = session.teardown_disposition()
    assert disposition["attempt"] != "none"                  # evidence retained
    assert (disposition["settlement"]["unresolved"]["ownedResources"]
            == ["App", "MemCache"])

    # second close, fault NOT cleared: the retained ORIGINAL disposer is
    # re-targeted and fails again — the resource stays owed, ownership is NOT
    # released, and the composition is retained (no false clean release).
    second = asyncio.run(session.aclose())
    assert second["closed"] is False
    assert second["releaseOwnership"] is False
    assert session.loaded is True
    outcomes2 = {r["component"]: r["outcome"] for r in second["resources"]}
    assert outcomes2.get("App") == "failed"                  # NOT `absent`
    assert "App" in second["unresolved"]["ownedResources"]
    # the prior failure evidence is still inspectable on the re-attempted rec.
    app_rec = next(r for r in second["resources"] if r["component"] == "App")
    assert "native disposer fault" in (app_rec.get("error") or "")

    # once the fault clears, the same explicit re-arm path re-attempts the SAME
    # original disposer and releases cleanly.
    session.rearm_teardown()
    # restore the real disposer on the SAME retained original object.
    session._driver._retained_disposers["App"].dispose = real_app_dispose
    third = asyncio.run(session.aclose())
    assert third["closed"] is True
    assert third["releaseOwnership"] is True
    assert session.loaded is False
