"""Roadmap item 160: the py emitter's OTHER reserved bare-names no longer fail
late at emit (follow-on to item 156).

Item 156 namespaced the injected activation scaffolding (`ctx`/`config`/`frame`
-> `_revl_*`) so those user identifiers compile and run. It left the rest of the
`from runtime import …` surface sharing the same late-at-emit failure mode: a
user identifier the checker accepts collided with a module-level runtime import
and either raised an `EmitError` deep in this backend (`fmt`, `Frame`,
`ConfigSchema`) or — worse — was silently, inconsistently mishandled (`Job` was
absent from the guard entirely; a spurious `from runtime import Job` was emitted
for a user `Job` and shadowed it).

Item 160 handles the whole set coherently, by kind:

  * the pure-emitter runtime imports (`fmt`, `Frame`, `ConfigSchema`, `spawn`,
    the timer and lifecycle drivers) are import-aliased to `_revl_<name>` and
    referenced through the alias — a namespace `_ident` forbids any user
    identifier from entering — so a user identifier of that name now WORKS;

  * the host-root triple `Pool`/`Map`/`Job` cannot be aliased (they are
    language builtins written verbatim, indistinguishable from a same-named user
    var in a v3 fn body), so they stay guarded, now UNIFORMLY — the previously
    unguarded `Job` rejects with the same early diagnostic as its siblings.

These tests drive the mutated reference IR through a real cordis Context, proving
the aliased names round-trip end to end (not merely that emit does not raise),
and pin the guarded names' early rejection.
"""

from __future__ import annotations

import copy

import pytest
from cordis import Context

import emit
from conftest import Errors, flush, load_module, ops


def _rename(node, old: str, new: str) -> None:
    """Rewrite every name reference / bind / param of ``old`` to ``new``."""
    if isinstance(node, dict):
        if node.get("kind") == "name" and node.get("id") == old:
            node["id"] = new
        if node.get("bind") == old:
            node["bind"] = new
        if isinstance(node.get("params"), list):
            node["params"] = [new if p == old else p for p in node["params"]]
        for value in node.values():
            _rename(value, old, new)
    elif isinstance(node, list):
        for item in node:
            _rename(item, old, new)


async def _load_pair(module, root, url="postgres://primary:5432/app"):
    fiber_db = root.plugin(module.PgDatabase, {"url": url})
    fiber_cache = root.plugin(module.UserCache)
    await fiber_db
    await fiber_cache
    await flush()
    return fiber_db, fiber_cache


# --- the aliased names: user identifiers that now WORK ----------------------

# every pure-emitter runtime import that item 160 aliases and the reference IR
# can carry as a rebound body local (it uses `Map.new()`, a `Frame`, a
# `ConfigSchema`, and `fmt(...)`, so all four reference sites coexist with the
# user binding in the emitted module)
ALIASED = ["fmt", "Frame", "ConfigSchema", "spawn", "schedule_every"]


@pytest.mark.parametrize("name", ALIASED)
def test_aliased_name_emits_and_execs(reference_ir, name):
    """A component body local renamed to a runtime-import name compiles, the
    user binding is emitted verbatim, and the emitted module still imports the
    runtime helper under its `_revl_*` alias — no collision, no EmitError."""
    ir = copy.deepcopy(reference_ir)
    _rename(ir["components"][1], "store", name)
    source = emit.emit(ir)  # must not raise
    assert f"{name} = Map.new()" in source          # the user's own binding
    assert emit._IMPORT_ALIAS[name] not in (name,)   # sanity: it is aliased
    load_module(source)                              # execs (imports resolve)


async def test_aliased_fmt_runs_end_to_end(reference_ir, trace):
    """The full R1 LIFO-recovery scenario runs with the migratable-Map local
    named `fmt` — the user `fmt` and the runtime formatter `_revl_fmt` (used by
    the same component's `emit … fmt(...)` log line) coexist without capture."""
    ir = copy.deepcopy(reference_ir)
    _rename(ir["components"][1], "store", "fmt")
    source = emit.emit(ir)
    assert "fmt = Map.new()" in source               # user binding
    assert "_revl_fmt('INSERT INTO cache_log" in source  # runtime formatter
    module = load_module(source)

    root = Context()
    errors = Errors(root)
    _fiber_db, fiber_cache = await _load_pair(module, root)

    cache = root.get("cache")
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    assert cache.get("k1") == "v1"
    mark = len(trace)

    await fiber_cache.dispose()
    await flush()

    assert ops(trace[mark:]) == ["map.remove k2", "map.remove k1", "map.drop"]
    assert errors.calls == []


async def test_aliased_frame_parameter_runs_end_to_end(reference_ir):
    """A provide-method parameter named `Frame` compiles, emits, and runs: the
    `get(key)` op is rebound to `get(Frame)` and reads back the stored value,
    while the component's own `_revl_Frame(...)` scaffolding is unaffected."""
    ir = copy.deepcopy(reference_ir)
    get_method = ir["components"][1]["body"][1]["methods"][0]
    assert get_method["name"] == "get"
    _rename(get_method, "key", "Frame")
    source = emit.emit(ir)  # must not raise
    assert "def get(self, Frame):" in source
    assert "_revl_Frame(_revl_ctx, 'UserCache')" in source

    module = load_module(source)
    root = Context()
    _fiber_db, fiber_cache = await _load_pair(module, root)

    cache = root.get("cache")
    cache.put("alpha", "beta")
    assert cache.get("alpha") == "beta"

    await fiber_cache.dispose()
    await flush()


# --- the guarded host roots: uniform early rejection ------------------------

@pytest.mark.parametrize("name", ["Pool", "Map", "Job"])
def test_host_root_rejected_early(reference_ir, name):
    """Pool/Map/Job stay guarded (cannot be aliased apart from a same-named user
    var), and now reject UNIFORMLY at `_ident` with a clear diagnostic — `Job`
    used to slip through unguarded and silently shadow its spurious import."""
    bad = copy.deepcopy(reference_ir)
    bad["components"][1]["body"][0]["bind"] = name
    with pytest.raises(emit.EmitError, match=f"{name!r} collides with emitter scaffolding"):
        emit.emit(bad)


def test_job_is_in_the_guard():
    """Regression pin for the item-160 inconsistency: `Job` (a host root) must
    live in the same guard set as `Pool`/`Map`, not be silently importable."""
    assert {"Pool", "Map", "Job"} <= emit._RESERVED
    # the aliased names are explicitly NOT reserved — they must be free to use
    assert not (set(emit._IMPORT_ALIAS) & emit._RESERVED)
