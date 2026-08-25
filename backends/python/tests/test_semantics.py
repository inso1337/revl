"""R1–R5 from docs/backend-ir.md §Required semantics, against cordis-py.

Every test loads components emitted from IR (the reference document, plus a
purpose-built one for the R3 teardown-readability clause) into a real cordis
Context and asserts on the host-builtin trace and the runtime's introspection.
"""

from __future__ import annotations

import copy

from cordis import Context
from cordis.fiber import FiberState

import emit
from conftest import Errors, flush, hook_snapshot, load_module, ops


async def _load_pair(module, root, url="postgres://primary:5432/app"):
    fiber_db = root.plugin(module.PgDatabase, {"url": url})
    fiber_cache = root.plugin(module.UserCache)
    await fiber_db
    await fiber_cache
    await flush()
    return fiber_db, fiber_cache


# ---------------------------------------------------------------------------
# R1 — LIFO recovery
# ---------------------------------------------------------------------------


async def test_r1_unload_runs_undos_in_reverse_order(reference_ir, trace):
    """R1: unloading runs accumulated undos newest-first, *including* effects
    accumulated by provide-method calls while the component was active."""
    module = load_module(emit.emit(reference_ir))
    root = Context()
    errors = Errors(root)
    _fiber_db, fiber_cache = await _load_pair(module, root)

    cache = root.get("cache")
    cache.put("k1", "v1")  # effect: insert k1 / undo remove k1  (method-time)
    cache.put("k2", "v2")  # effect: insert k2 / undo remove k2  (method-time)
    mark = len(trace)

    await fiber_cache.dispose()
    await flush()

    # accumulator order was [map.new, provide cache, insert k1, insert k2];
    # recovery must be exactly the reverse
    assert ops(trace[mark:]) == ["map.remove k2", "map.remove k1", "map.drop"]
    assert errors.calls == []


async def test_r1_method_effects_undone_on_deactivation_too(reference_ir, trace):
    """R1: the same LIFO recovery runs when deactivation is *reactive*
    (provider withdrawn) rather than an explicit dispose."""
    module = load_module(emit.emit(reference_ir))
    root = Context()
    errors = Errors(root)
    fiber_db, _fiber_cache = await _load_pair(module, root)

    cache = root.get("cache")
    cache.put("a", "1")
    cache.put("b", "2")
    mark = len(trace)

    await fiber_db.dispose()
    await flush()

    cache_tail = [event for event in ops(trace[mark:]) if event.startswith("map.")]
    assert cache_tail == ["map.remove b", "map.remove a", "map.drop"]
    assert errors.calls == []


# ---------------------------------------------------------------------------
# R2 — reactive resolution
# ---------------------------------------------------------------------------


async def test_r2_activates_only_when_requirements_are_provided(reference_ir, trace):
    """R2: a component with unmet requires stays pending — no effects run."""
    module = load_module(emit.emit(reference_ir))
    root = Context()
    fiber_cache = root.plugin(module.UserCache)
    await flush()

    assert fiber_cache.state == FiberState.PENDING
    assert root.get("cache") is None
    assert "map.new" not in ops(trace)

    root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
    await fiber_cache
    await flush()
    assert fiber_cache.state == FiberState.ACTIVE
    assert root.get("cache") is not None
    assert "map.new" in ops(trace)


async def test_r2_deactivates_on_withdrawal_and_reactivates_on_replacement(reference_ir, trace):
    """R2: withdrawal deactivates the dependent; a replacement provider
    reactivates it (fresh activation, fresh store)."""
    module = load_module(emit.emit(reference_ir))
    root = Context()
    errors = Errors(root)
    fiber_db, fiber_cache = await _load_pair(module, root)
    root.get("cache").put("alice", "42")

    await fiber_db.dispose()
    await flush()
    assert fiber_cache.state == FiberState.PENDING
    assert root.get("cache") is None

    fiber_db2 = root.plugin(module.PgDatabase, {"url": "postgres://replica:5432/app"})
    await fiber_db2
    await fiber_cache
    await flush()
    assert fiber_cache.state == FiberState.ACTIVE
    cache = root.get("cache")
    assert cache is not None
    assert cache.get("alice") is None  # recovered, then re-acquired fresh

    mark = len(trace)
    cache.put("alice", "43")
    # the emission goes to the *replacement* provider's pool
    assert any(event.startswith("pool") and "cache_log" in event for event in trace[mark:])
    assert errors.calls == []


# ---------------------------------------------------------------------------
# R3 — withdrawal ordering
# ---------------------------------------------------------------------------


async def test_r3_dependents_fully_deactivate_before_provider_teardown(reference_ir, trace):
    """R3: when the provider is withdrawn, every dependent undo runs before
    the provider's own inverses (pool.close last)."""
    module = load_module(emit.emit(reference_ir))
    root = Context()
    errors = Errors(root)
    fiber_db, _fiber_cache = await _load_pair(module, root)
    root.get("cache").put("alice", "42")

    await fiber_db.dispose()
    await flush()

    stripped = ops(trace)
    close = stripped.index("pool.close postgres://primary:5432/app")
    for dependent_op in ("map.remove alice", "map.drop"):
        assert stripped.index(dependent_op) < close, stripped
    assert errors.calls == []


# a component whose *undo expression* calls its required service — the R3
# teardown-readability clause ("`undo` expressions may use `req`")
_DB_LOCK_COMPONENT = {
    "name": "DbLock",
    "config": [],
    "requires": {"db": "Database"},
    "provides": {},
    "body": [
        {
            "step": "let-effect",
            "bind": "lock",
            "acquire": {
                "kind": "call",
                "target": {"kind": "req", "name": "db"},
                "method": "execute",
                "args": [{"kind": "lit", "value": "SELECT pg_advisory_lock(42)"}],
            },
            "undo": {
                "kind": "call",
                "target": {"kind": "req", "name": "db"},
                "method": "execute",
                "args": [{"kind": "lit", "value": "SELECT pg_advisory_unlock(42)"}],
            },
        }
    ],
}


async def test_r3_dependent_can_call_required_service_during_own_teardown(reference_ir, trace):
    """R3: the committed view keeps `req` readable while the dependent tears
    down — its undo calls db.execute after withdrawal began, before close."""
    ir = copy.deepcopy(reference_ir)
    ir["components"] = [ir["components"][0], _DB_LOCK_COMPONENT]
    module = load_module(emit.emit(ir))

    root = Context()
    errors = Errors(root)
    fiber_db = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
    fiber_lock = root.plugin(module.DbLock)
    await fiber_db
    await fiber_lock
    await flush()
    assert "pool.execute SELECT pg_advisory_lock(42)" in ops(trace)

    await fiber_db.dispose()
    await flush()

    stripped = ops(trace)
    unlock = stripped.index("pool.execute SELECT pg_advisory_unlock(42)")
    close = stripped.index("pool.close postgres://primary:5432/app")
    assert unlock < close, stripped
    assert errors.calls == []  # the undo's service call did not fault


# ---------------------------------------------------------------------------
# R4 — no residue
# ---------------------------------------------------------------------------


async def test_r4_no_residue_after_unloading_everything(reference_ir, trace):
    """R4: after unloading the whole composition, the runtime's introspection
    shows no bindings, listeners, or effects left behind."""
    module = load_module(emit.emit(reference_ir))
    root = Context()
    hooks_before = hook_snapshot(root)
    disposables_before = root.fiber._disposables.length
    assert root.reflect.store == {}
    assert root.registry.size == 0

    fiber_db, fiber_cache = await _load_pair(module, root)
    root.get("cache").put("alice", "42")

    await fiber_cache.dispose()
    await fiber_db.dispose()
    await flush()

    assert hook_snapshot(root) == hooks_before
    assert root.fiber._disposables.length == disposables_before
    assert root.reflect.store == {}
    assert root.registry.size == 0
    assert root.get("db") is None and root.get("cache") is None


# ---------------------------------------------------------------------------
# R5 — provision withdrawal is derived
# ---------------------------------------------------------------------------


def test_r5_emitted_code_never_hand_rolls_provision_teardown(reference_ir):
    """R5: the emitted module installs provisions through the runtime's
    revertible provide/set and contains no withdrawal code of its own."""
    source = emit.emit(reference_ir)
    assert "_revl_ctx.provide('db')" in source
    assert "_revl_ctx.provide('cache')" in source
    assert "_revl_ctx.set('db'" in source and "_revl_ctx.set('cache'" in source
    # nothing in the emitted module touches the service registry on the way out
    for forbidden in ("reflect.store", "notify", "del ", "pop(", "_isolate", "unregister"):
        assert forbidden not in source, f"hand-rolled teardown fragment: {forbidden!r}"


async def test_r5_withdrawal_happens_although_module_has_no_teardown_code(reference_ir, trace):
    """R5 (behavioral): disposing the provider withdraws the provision and
    deactivates dependents purely through the runtime-derived inverse."""
    module = load_module(emit.emit(reference_ir))
    root = Context()
    fiber_db, fiber_cache = await _load_pair(module, root)
    assert {impl.name for impl in root.reflect.store.values()} == {"db", "cache"}

    await fiber_db.dispose()
    await flush()

    assert {impl.name for impl in root.reflect.store.values()} == set()
    assert fiber_cache.state == FiberState.PENDING
