"""Roadmap item 156: `ctx` (and its siblings `config` / `frame`) are ordinary
revl identifiers on the py tier.

The emitter used to inject its per-activation scaffolding into user body scope
under the bare names ``ctx`` / ``config`` / ``frame`` and reject any IR
identifier that matched — a backend-specific failure raised LATE at emit for a
name the checker happily accepts (self-host dogfooding finding, item 145). The
scaffolding now lives in the reserved ``_revl_*`` namespace, which no user
identifier can enter (``_ident`` forbids a leading underscore), so those names
are free again.

These tests take the reference IR, rebind a body local / a provide-method
parameter to ``ctx``, and drive the emitted module through a real cordis
Context — proving the round trip end to end, not just that emit does not raise.
"""

from __future__ import annotations

import copy

from cordis import Context

import emit
from conftest import Errors, flush, load_module, ops


def _rename_name(node, old: str, new: str) -> None:
    """Rewrite every ``{"kind": "name", "id": old}`` and ``bind``/``params``
    occurrence of ``old`` to ``new`` inside ``node`` (in place)."""
    if isinstance(node, dict):
        if node.get("kind") == "name" and node.get("id") == old:
            node["id"] = new
        if node.get("bind") == old:
            node["bind"] = new
        if isinstance(node.get("params"), list):
            node["params"] = [new if p == old else p for p in node["params"]]
        for value in node.values():
            _rename_name(value, old, new)
    elif isinstance(node, list):
        for item in node:
            _rename_name(item, old, new)


async def _load_pair(module, root, url="postgres://primary:5432/app"):
    fiber_db = root.plugin(module.PgDatabase, {"url": url})
    fiber_cache = root.plugin(module.UserCache)
    await fiber_db
    await fiber_cache
    await flush()
    return fiber_db, fiber_cache


def test_ctx_local_emits_without_rejection(reference_ir):
    """A component body local named ``ctx`` emits: no scaffolding EmitError,
    the user binding is verbatim ``ctx``, and the real context handle is the
    reserved ``_revl_ctx``."""
    ir = copy.deepcopy(reference_ir)
    _rename_name(ir["components"][1], "store", "ctx")  # the `let-effect` local
    source = emit.emit(ir)  # must not raise
    assert "ctx = Map.new()" in source          # the user's own binding
    assert "def _user_cache_apply(_revl_ctx, _revl_config):" in source
    assert "Frame(_revl_ctx," in source          # scaffolding is namespaced


async def test_ctx_local_runs_end_to_end(reference_ir, trace):
    """The R1 LIFO-recovery scenario still runs when the body local that holds
    the migratable Map is named ``ctx`` — the emitted user ``ctx`` and the
    runtime ``_revl_ctx`` context coexist without capture."""
    ir = copy.deepcopy(reference_ir)
    _rename_name(ir["components"][1], "store", "ctx")
    module = load_module(emit.emit(ir))

    root = Context()
    errors = Errors(root)
    _fiber_db, fiber_cache = await _load_pair(module, root)

    cache = root.get("cache")
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    mark = len(trace)

    await fiber_cache.dispose()
    await flush()

    assert ops(trace[mark:]) == ["map.remove k2", "map.remove k1", "map.drop"]
    assert errors.calls == []


async def test_ctx_method_parameter_runs_end_to_end(reference_ir):
    """A provide-method parameter named ``ctx`` compiles, emits, and runs: the
    `get(key)` op is rebound to `get(ctx)` and reads back the stored value."""
    ir = copy.deepcopy(reference_ir)
    get_method = ir["components"][1]["body"][1]["methods"][0]
    assert get_method["name"] == "get"
    _rename_name(get_method, "key", "ctx")  # rename the parameter and its use
    source = emit.emit(ir)  # must not raise
    assert "def get(self, ctx):" in source

    module = load_module(source)
    root = Context()
    _fiber_db, fiber_cache = await _load_pair(module, root)

    cache = root.get("cache")
    cache.put("alpha", "beta")
    assert cache.get("alpha") == "beta"

    await fiber_cache.dispose()
    await flush()
