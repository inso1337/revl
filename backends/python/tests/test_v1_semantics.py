"""IR v1 semantics against cordis-py: A1 (`await` = divert boundary),
A5 (`compensate` joins the accumulator), A8 (mid-body failure = L-Raise).

The Migrator IR mirrors examples/migrator.rvl; the Db provider is a minimal
hand-built v1 component so the suite is self-contained.
"""

from __future__ import annotations

import asyncio

from cordis import Context
from cordis.fiber import FiberState

import emit
import runtime as runtime_mod
from conftest import flush, load_module, ops

_SERVICES = {
    "Database": {
        "methods": {
            "query": {"params": [{"name": "sql", "type": "Str"}], "returns": "List[Row]", "emission": False},
            "execute": {"params": [{"name": "sql", "type": "Str"}], "returns": "Int", "emission": True},
        }
    }
}


def _call(target, method, *args):
    return {"kind": "call", "target": target, "method": method,
            "args": [{"kind": "lit", "value": a} if not isinstance(a, dict) else a for a in args]}


_DB = {
    "name": "Db",
    "config": [],
    "requires": {},
    "provides": {"db": "Database"},
    "body": [
        {"step": "let-effect", "bind": "pool",
         "acquire": {"kind": "host", "fn": "Pool.open",
                     "args": [{"kind": "lit", "value": "postgres://v1:5432/app"}, {"kind": "lit", "value": 2}]},
         "undo": _call({"kind": "name", "id": "pool"}, "close")},
        {"step": "provide", "name": "db", "service": "Database", "methods": [
            {"name": "query", "params": ["sql"],
             "body": [{"step": "return", "expr": _call({"kind": "name", "id": "pool"}, "query", {"kind": "name", "id": "sql"})}]},
            {"name": "execute", "params": ["sql"],
             "body": [{"step": "return", "expr": _call({"kind": "name", "id": "pool"}, "execute", {"kind": "name", "id": "sql"})}]},
        ]},
    ],
}

_REQ_DB = {"kind": "req", "name": "db"}

_MIGRATOR = {
    "name": "Migrator",
    "config": [],
    "requires": {"db": "Database"},
    "provides": {},
    "body": [
        {"step": "let-effect", "bind": "lock",
         "acquire": _call(_REQ_DB, "query", "SELECT pg_advisory_lock(42)"),
         "undo": _call(_REQ_DB, "query", "SELECT pg_advisory_unlock(42)")},
        {"step": "await", "expr": {"kind": "host", "fn": "Job.run",
                                   "args": [{"kind": "lit", "value": "migrations"}]}},
        {"step": "emit", "expr": _call(_REQ_DB, "execute", "INSERT INTO migration_log VALUES (42)"),
         "compensate": _call(_REQ_DB, "execute", "DELETE FROM migration_log WHERE id = 42")},
    ],
}


def _ir(*components):
    return {"ir_version": 1, "services": _SERVICES, "components": list(components)}


def _module():
    return load_module(emit.emit(_ir(_DB, _MIGRATOR)), "v1_module")


# ---------------------------------------------------------------------------
# A5 — two-phase teardown: compensation is discharged on a clean unload
# ---------------------------------------------------------------------------
# A5 respec (2026-08-26; items 247/316, docs/contract-errata.md). The old
# "compensation joins the single accumulator and reverts interleaved with the
# inverses on every teardown" was the v0 placeholder. Under the two-phase
# contract (docs/design/247-compensate.md) a compensation is DISCHARGED on a
# clean unload/commit — it never runs, and the forward emission it would offset
# stands as the deliverable; only the bracket inverse (advisory unlock) replays.
# The a5b abort path (compensation runs in Phase 2, LIFO within its class,
# AFTER the earlier bracket unlock) is owed by item 316's TCK respec.


async def test_a5a_compensation_discharged_on_clean_unload(trace):
    module = _module()
    root = Context()
    db = root.plugin(module.Db)
    migrator = root.plugin(module.Migrator)
    await flush()
    assert migrator.state is FiberState.ACTIVE

    executed = [e for e in ops(trace) if "pool.execute" in e]
    assert executed == ["pool.execute INSERT INTO migration_log VALUES (42)"]

    migrator.dispose()
    await flush()

    events = ops(trace)
    assert "pool.execute DELETE FROM migration_log WHERE id = 42" not in events, \
        "a compensation is discharged on a clean unload, never run (a5a)"
    assert "pool.query SELECT pg_advisory_unlock(42)" in events, \
        "the bracket inverse still replays on a clean unload"
    # the INSERT the compensation would have offset stands as the deliverable
    assert [e for e in ops(trace) if "pool.execute" in e] == \
        ["pool.execute INSERT INTO migration_log VALUES (42)"]
    assert db.state is FiberState.ACTIVE


# ---------------------------------------------------------------------------
# A1 — a divert during the await skips every later step
# ---------------------------------------------------------------------------

async def test_a1_divert_during_await_skips_emission(trace):
    module = _module()
    root = Context()
    root.plugin(module.Db)
    migrator = root.plugin(module.Migrator)
    # advance until the job iteration is in flight (it takes five further
    # ticks to land), then dispose mid-await: the iteration lands (inertia),
    # the boundary yield closes it, and the divert skips the emission
    for _ in range(50):
        await asyncio.sleep(0)
        events = ops(trace)
        if "job.run migrations start" in events:
            assert "job.run migrations done" not in events
            break
    else:
        raise AssertionError("migrator never reached its await step")
    migrator.dispose()
    await flush()

    events = ops(trace)
    assert "pool.query SELECT pg_advisory_lock(42)" in events
    assert "pool.query SELECT pg_advisory_unlock(42)" in events, "accumulated inverse must run"
    assert not any("pool.execute" in e for e in events), \
        "the emission after the boundary must never execute"
    assert migrator.state is not FiberState.ACTIVE


# ---------------------------------------------------------------------------
# A8 — a refusing acquisition reverts what came before and fails the fiber
# ---------------------------------------------------------------------------

async def test_a8_mid_body_failure_reverts_and_contains(trace):
    failing = {
        "name": "Failing",
        "config": [],
        "requires": {},
        "provides": {},
        "body": [
            {"step": "let-effect", "bind": "scratch",
             "acquire": {"kind": "host", "fn": "Map.new", "args": []},
             "undo": _call({"kind": "name", "id": "scratch"}, "drop")},
            {"step": "let-effect", "bind": "pool",
             "acquire": {"kind": "host", "fn": "Pool.open",
                         "args": [{"kind": "lit", "value": "boom://nope"}, {"kind": "lit", "value": 1}]},
             "undo": _call({"kind": "name", "id": "pool"}, "close")},
        ],
    }
    module = load_module(emit.emit(_ir(_DB, failing)), "v1_failing")
    root = Context()
    db = root.plugin(module.Db)
    failing_fiber = root.plugin(module.Failing)
    await flush()

    events = ops(trace)
    assert "pool.open refused boom://nope" in events
    assert "map.drop" in events, "effects before the failure must revert (L-Raise)"
    assert failing_fiber.state is FiberState.FAILED
    assert db.state is FiberState.ACTIVE, "siblings are unaffected by a failing fiber"
