"""Pool / Job / trace semantics — the host-runtime contract itself.

The other suites in this directory drive emitted components; this one pins the
host runtime they sit on.  Two layers:

* the behaviour, exercised directly against ``backends/python/runtime.py``;
* a **cross-tier drift guard** that reads the other three tiers' sources and
  asserts they still carry the same rules.  Backend divergence is this
  project's recurring bug class (see tests/test_cross_tier.py), and `Pool`
  and `Job` are exactly where it hid: they were placeholders on every tier
  and each tier faked them slightly differently.

The normative text lives in ``backends/python/runtime.py`` under
``.. _pool-job-semantics:``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib

import pytest

import runtime as runtime_mod
from runtime import Job, JobCancelled, Map, Pool, PoolError

REPO = pathlib.Path(runtime_mod.__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_jobs():
    Job.reset()
    yield
    Job.reset()


# ---------------------------------------------------------------------------
# Pool — real bounded capacity
# ---------------------------------------------------------------------------


def test_pool_starts_with_size_idle_connections(trace):
    pool = Pool.open("pg://x", 3)
    assert (pool.capacity(), pool.in_use(), pool.available()) == (3, 0, 3)
    assert trace == [f"{pool._tag}.open pg://x"]


def test_acquire_hands_out_connections_in_a_deterministic_order():
    pool = Pool.open("pg://x", 3)
    assert [pool.acquire(), pool.acquire(), pool.acquire()] == [1, 2, 3]
    pool.release(2)
    assert pool.acquire() == 2, "the lowest idle id comes back first"


def test_acquire_and_release_are_traced_with_the_accounting(trace):
    pool = Pool.open("pg://x", 2)
    conn = pool.acquire()
    pool.release(conn)
    assert trace[1:] == [
        f"{pool._tag}.acquire conn=1 1/2",
        f"{pool._tag}.release conn=1 0/2",
    ]


def test_exhausting_the_pool_is_a_visible_error():
    pool = Pool.open("pg://x", 2)
    pool.acquire()
    pool.acquire()
    with pytest.raises(PoolError, match=r"exhausted \(size=2, in_use=2\)"):
        pool.acquire()
    # statements borrow a connection too, so they refuse as well
    with pytest.raises(PoolError, match=r"query exhausted"):
        pool.query("SELECT 1")
    with pytest.raises(PoolError, match=r"execute exhausted"):
        pool.execute("INSERT INTO t VALUES (1)")


def test_statements_borrow_and_return_without_extra_trace_events(trace):
    """query/execute account for a connection for the duration of the call,
    but stay silent about it: the trace strings emitted code depends on are
    unchanged."""
    pool = Pool.open("pg://x", 1)
    assert pool.query("SELECT 1") == []
    assert pool.execute("INSERT INTO t VALUES (1)") == 1
    assert pool.in_use() == 0 and pool.available() == 1
    assert trace == [
        f"{pool._tag}.open pg://x",
        f"{pool._tag}.query SELECT 1",
        f"{pool._tag}.execute INSERT INTO t VALUES (1)",
    ]


def test_releasing_a_connection_that_is_not_checked_out_is_refused():
    pool = Pool.open("pg://x", 2)
    with pytest.raises(PoolError, match="is not checked out"):
        pool.release(1)
    conn = pool.acquire()
    pool.release(conn)
    with pytest.raises(PoolError, match="is not checked out"):
        pool.release(conn)


def test_close_releases_everything_and_makes_the_pool_unusable():
    pool = Pool.open("pg://x", 2)
    pool.acquire()
    pool.acquire()
    pool.close()
    assert (pool.capacity(), pool.in_use(), pool.available()) == (0, 0, 0)
    for use in (
        lambda: pool.query("SELECT 1"),
        lambda: pool.execute("INSERT INTO t VALUES (1)"),
        pool.acquire,
        lambda: pool.release(1),
        pool.close,
    ):
        with pytest.raises(PoolError, match="after close"):
            use()


def test_pool_size_must_be_a_positive_integer():
    for bad in (0, -1, True, "2", 1.5):
        with pytest.raises(PoolError, match=r">= 1"):
            Pool.open("pg://x", bad)


def test_the_a8_refusal_hook_still_fires_before_any_size_check(trace):
    with pytest.raises(RuntimeError, match="refused to open"):
        Pool.open("boom://nope", 0)
    assert trace == ["pool.open refused boom://nope"]


def test_capacity_invariant_holds_through_a_workload():
    pool = Pool.open("pg://x", 4)
    held = [pool.acquire(), pool.acquire()]
    pool.query("SELECT 1")
    assert pool.in_use() + pool.available() == pool.capacity() == 4
    for conn in held:
        pool.release(conn)
    assert pool.in_use() + pool.available() == pool.capacity() == 4


# ---------------------------------------------------------------------------
# Job — real, cancellable async
# ---------------------------------------------------------------------------


async def test_a_job_completes_after_exactly_ticks_turns(trace):
    handle = Job.run("migrations")
    assert handle.state() == "pending"
    assert Job.pending() == 1
    assert trace == ["job.run migrations start"]

    task = asyncio.ensure_future(_await(handle))
    for _ in range(Job.TICKS):
        assert handle.state() == "pending", "the job is genuinely in flight"
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.done() and task.result() == "migrations"
    assert handle.state() == "done"
    assert Job.pending() == 0
    assert trace == ["job.run migrations start", "job.run migrations done"]


async def _await(handle):
    return await handle


async def test_cancelling_a_pending_job_makes_the_await_fail(trace):
    handle = Job.run("settle")
    assert handle.cancel() is True
    assert handle.cancel() is False, "cancel is idempotent"
    assert handle.state() == "cancelled"
    with pytest.raises(JobCancelled, match='job "settle" cancelled'):
        await handle
    assert trace == ["job.run settle start", "job.run settle cancelled"]


async def test_cancelling_mid_flight_stops_the_job(trace):
    handle = Job.run("warmup")
    task = asyncio.ensure_future(_await(handle))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert handle.remaining < Job.TICKS, "work actually progressed"
    handle.cancel()
    with pytest.raises(JobCancelled):
        await task
    assert "job.run warmup done" not in trace
    assert "job.run warmup cancelled" in trace


async def test_cancelling_a_finished_job_is_a_no_op():
    handle = Job.run("done-already")
    assert await handle == "done-already"
    assert handle.cancel() is False
    assert handle.state() == "done"


async def test_awaiting_a_finished_job_again_does_not_re_record(trace):
    handle = Job.run("once")
    assert await handle == "once"
    assert await handle == "once"
    assert trace.count("job.run once done") == 1


async def test_teardown_of_the_awaiting_task_cancels_the_job(trace):
    """The A1 divert boundary: a component torn down during `await
    Job.run(...)` cancels the job, and that is observable."""
    handle = Job.run("boundary")
    task = asyncio.ensure_future(_await(handle))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.state() == "cancelled"
    assert Job.pending() == 0
    assert "job.run boundary cancelled" in trace


async def test_pending_counts_abandoned_jobs():
    Job.run("abandoned")
    assert Job.pending() == 1, "a job nobody awaited is residue, and it shows"


# ---------------------------------------------------------------------------
# tracing — multi-observer
# ---------------------------------------------------------------------------


def test_two_observers_coexist():
    first: list = []
    second: list = []
    off_first = runtime_mod.add_trace(first.append)
    off_second = runtime_mod.add_trace(second.append)
    try:
        assert runtime_mod.trace_observers() == 2
        Map.new().drop()
        assert first == second
        assert len(first) == 2
    finally:
        off_first()
        off_second()
    assert runtime_mod.trace_observers() == 0


def test_unsubscribing_is_idempotent_and_leaves_the_others_alone():
    kept: list = []
    dropped: list = []
    off_kept = runtime_mod.add_trace(kept.append)
    off_dropped = runtime_mod.add_trace(dropped.append)
    try:
        off_dropped()
        off_dropped()  # idempotent
        assert runtime_mod.remove_trace(dropped.append) is False
        Map.new().drop()
        assert len(kept) == 2 and dropped == []
    finally:
        off_kept()


def test_set_trace_does_not_disturb_subscribed_observers(trace):
    """The legacy single-observer API keeps its own slot: `set_trace(None)`
    (every driver's teardown) must not silence a demo's subscription."""
    observer: list = []
    off = runtime_mod.add_trace(observer.append)
    try:
        Map.new()
        runtime_mod.set_trace(None)
        Map.new()
        assert len(observer) == 2
        assert len(trace) == 1, "the primary callback stopped, the observer did not"
    finally:
        off()
        runtime_mod.set_trace(trace.append)  # restore for the fixture teardown


def test_an_observer_sees_the_same_events_as_the_primary(trace):
    observer: list = []
    off = runtime_mod.add_trace(observer.append)
    try:
        pool = Pool.open("pg://x", 1)
        pool.query("SELECT 1")
    finally:
        off()
    assert observer == trace


# ---------------------------------------------------------------------------
# resolved config in the trace
# ---------------------------------------------------------------------------


def test_resolved_config_is_traced_and_queryable(trace):
    schema = runtime_mod.ConfigSchema([("url", "Str", None), ("pool_size", "Int", 10)])
    resolved = schema.resolve({"url": "pg://x"})
    assert resolved == {"url": "pg://x", "pool_size": 10}
    assert trace == [], "nameless schemas wait for their component's Frame"

    frame = runtime_mod.Frame(_FakeCtx(), "PgDatabase")
    assert frame.config == resolved
    assert trace == [
        'PgDatabase.config {pool_size=10, url="pg://x"} [defaults: pool_size]'
    ]
    assert runtime_mod.resolved_config("PgDatabase") == resolved
    assert "PgDatabase" in runtime_mod.resolved_config()


def test_a_component_without_config_traces_nothing(trace):
    runtime_mod.Frame(_FakeCtx(), "UserCache")
    assert trace == []


def test_a_named_schema_traces_without_a_frame(trace):
    schema = runtime_mod.ConfigSchema([("retries", "Int", 3)], name="Retrier")
    schema.resolve({})
    assert trace == ["Retrier.config {retries=3} [defaults: retries]"]


def test_config_with_no_defaults_applied_has_no_defaults_suffix(trace):
    schema = runtime_mod.ConfigSchema([("url", "Str", None)], name="Db")
    schema.resolve({"url": "pg://x"})
    assert trace == ['Db.config {url="pg://x"}']


class _FakeCtx:
    def effect(self, body, label=None):  # pragma: no cover — never installed here
        raise AssertionError("not used")


# ---------------------------------------------------------------------------
# cross-tier drift guard
#
# The other three tiers are compiled/executed by their own suites; what this
# checks is that they still *say* the same thing, so a semantics change in one
# tier cannot land alone.  Rules, not formatting: each assertion is one clause
# of the shared contract.
# ---------------------------------------------------------------------------


def _tier_sources() -> dict:
    """The other tiers' host runtimes, as the text they actually ship.

    TypeScript ships `runtime.ts` verbatim; rust and java *emit* their host
    runtime into every module, so those two are read back from the emitters'
    output over IR that uses both `Pool` and `Job`.
    """
    ts = REPO / "backends" / "typescript" / "runtime.ts"
    irs = [REPO / "examples" / "user_cache.ir.json", REPO / "examples" / "migrator.ir.json"]
    if not ts.exists() or not all(path.exists() for path in irs):
        pytest.skip("tier sources are not in this checkout")

    documents = [json.loads(path.read_text(encoding="utf-8")) for path in irs]
    sources = {"typescript": ts.read_text(encoding="utf-8")}
    for tier in ("rust", "java"):
        emitter = _load_emitter(tier)
        sources[tier] = "\n".join(emitter.emit(document) for document in documents)
    return sources


def _load_emitter(tier: str):
    path = REPO / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_{tier}_emit_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_tier_points_at_the_one_semantics_definition():
    for tier, source in _tier_sources().items():
        assert "_pool-job-semantics:" in source, f"{tier} does not reference the contract"


def test_every_tier_refuses_a_pool_size_below_one():
    for tier, source in _tier_sources().items():
        assert "pool size must be an integer >= 1" in source, tier


def test_every_tier_reports_exhaustion_the_same_way():
    for tier, source in _tier_sources().items():
        assert "exhausted (size=" in source, tier
        assert "in_use=" in source, tier


def test_every_tier_refuses_use_after_close():
    for tier, source in _tier_sources().items():
        assert "after close" in source, tier


def test_every_tier_refuses_releasing_a_connection_that_is_not_checked_out():
    for tier, source in _tier_sources().items():
        assert "is not checked out" in source, tier


def test_every_tier_exposes_the_capacity_accounting():
    sources = _tier_sources()
    assert (Pool.open("pg://x", 1).capacity(), Pool.open("pg://x", 1).available()) == (1, 1)
    for tier, source in sources.items():
        for accessor in ("capacity", "available"):
            assert f"{accessor}(" in source, f"{tier}: {accessor}"
        assert ("inUse(" in source) or ("in_use(" in source), tier


def test_every_tier_agrees_on_the_job_tick_count():
    sources = _tier_sources()
    assert Job.TICKS == 5
    assert "JOB_TICKS = 5" in sources["typescript"]
    assert "pub const TICKS: u32 = 5u32;" in sources["rust"]
    assert "public static final int TICKS = 5;" in sources["java"]


def test_every_tier_has_the_three_job_states_and_a_cancel_returning_bool():
    sources = _tier_sources()
    assert Job.run("x").state() == "pending"
    for tier, source in sources.items():
        for state in ("pending", "done", "cancelled"):
            quoted = (f'"{state}"', f"'{state}'")  # TS quotes its string union
            assert any(form in source for form in quoted), f"{tier}: {state}"
    assert "cancel(): boolean" in sources["typescript"]
    assert "pub fn cancel(&self) -> bool" in sources["rust"]
    assert "public synchronized boolean cancel()" in sources["java"]


def test_the_rust_tier_documents_its_entry_point_split():
    """`Job.run` hands back the handle on py/ts/java.  Rust splits it —
    `Job::spawn` returns the handle, `Job::run` is the async shorthand the
    emitted `await Job.run(name)` call site uses — so emitted rust stays a
    plain `.await`.  The state machine is the same on both paths."""
    rust = _tier_sources()["rust"]
    assert "pub fn spawn(name: String) -> JobHandle" in rust
    assert "pub async fn run(name: String) -> String" in rust
    assert "Self::spawn(name).await" in rust


def test_every_tier_fails_an_await_of_a_cancelled_job():
    sources = _tier_sources()
    assert "JobCancelledError(`job " in sources["typescript"]
    assert 'panic!("job \\"{}\\" cancelled", this.name)' in sources["rust"]
    assert 'IllegalStateException("job \\"" + name + "\\" cancelled")' in sources["java"]


def test_every_tier_reports_one_row_affected_from_execute():
    """The divergence this guard was written for: cordis4j's Pool.execute
    used to return 0 while every other tier returned 1."""
    sources = _tier_sources()
    assert Pool.open("pg://x", 1).execute("INSERT") == 1
    # rows affected is a revl `Int`, so this tier answers a bigint (`1n`) —
    # the same one-row contract as rust's `1i64` and java's `1L`.
    assert "return 1n\n" in sources["typescript"]
    assert "        1i64\n" in sources["rust"]
    assert "return 1L;" in sources["java"]


def test_every_tier_returns_no_rows_from_query():
    sources = _tier_sources()
    assert Pool.open("pg://x", 1).query("SELECT 1") == []
    assert "return []" in sources["typescript"]
    assert "Vec::new()" in sources["rust"]
    assert "return java.util.List.of();" in sources["java"]
