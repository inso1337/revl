"""Worked example: the cordis-py runtime, wired to the TCK.

This is the proof the kit *runs* rather than merely being described. It drives
the real cordis-py runtime in-process (Python is the reference tier), using the
first-party python backend as a library — it emits each scenario's IR with
``backends/python/emit.py`` and loads it onto a real ``cordis.Context``, exactly
as the backend's own R1-R5 suite does, then reduces what happened to a
normalized :class:`~tck.adapter.Observation`.

A new-runtime author writes the analogous adapter for their runtime: either
in-process like this, or by shelling out to their runtime's hand-written
scenario reference and parsing a JSON observation back with
:meth:`Observation.from_json`. Nothing here is cordis-py-specific except the
body of :meth:`run` — the scenario *specifications* live in ``tck/spec.py`` and
are shared.

Run under the python backend's venv so ``cordis`` resolves:

    backends/python/.venv/bin/python -m tck.conformance --adapter py
"""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
import re
import sys
import types

from ..adapter import Observation, RuntimeAdapter

# Locate the first-party python backend and put it on the path so `import emit`,
# `import runtime`, and the emitted module's `from runtime import ...` resolve.
_REPO = pathlib.Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _import_backend():
    import emit  # type: ignore
    import runtime as runtime_mod  # type: ignore
    from cordis import Context  # type: ignore
    from cordis.fiber import FiberState  # type: ignore
    return emit, runtime_mod, Context, FiberState


# ---------------------------------------------------------------------------
# tiny driving helpers (mirrors backends/python/tests/conftest.py, kept local
# so the TCK does not depend on that test-only module)
# ---------------------------------------------------------------------------

def _load_module(emit, source: str) -> types.ModuleType:
    module = types.ModuleType("tck_emitted")
    exec(compile(source, "tck_emitted.py", "exec"), module.__dict__)
    return module


async def _flush() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


def _strip(event: str) -> str:
    return re.sub(r"#\d+", "", event)


# ---------------------------------------------------------------------------
# IR fragments (self-contained; the same shapes the backend's own suites use)
# ---------------------------------------------------------------------------

def _call(target, method, *args):
    return {"kind": "call", "target": target, "method": method,
            "args": [{"kind": "lit", "value": a} if not isinstance(a, dict) else a
                     for a in args]}


_SERVICES_V1 = {
    "Database": {
        "methods": {
            "query": {"params": [{"name": "sql", "type": "Str"}],
                      "returns": "List[Row]", "emission": False},
            "execute": {"params": [{"name": "sql", "type": "Str"}],
                        "returns": "Int", "emission": True},
        }
    }
}

_DB = {
    "name": "Db", "config": [], "requires": {}, "provides": {"db": "Database"},
    "body": [
        {"step": "let-effect", "bind": "pool",
         "acquire": {"kind": "host", "fn": "Pool.open",
                     "args": [{"kind": "lit", "value": "postgres://v1:5432/app"},
                              {"kind": "lit", "value": 2}]},
         "undo": _call({"kind": "name", "id": "pool"}, "close")},
        {"step": "provide", "name": "db", "service": "Database", "methods": [
            {"name": "query", "params": ["sql"],
             "body": [{"step": "return",
                       "expr": _call({"kind": "name", "id": "pool"}, "query",
                                     {"kind": "name", "id": "sql"})}]},
            {"name": "execute", "params": ["sql"],
             "body": [{"step": "return",
                       "expr": _call({"kind": "name", "id": "pool"}, "execute",
                                     {"kind": "name", "id": "sql"})}]},
        ]},
    ],
}

_REQ_DB = {"kind": "req", "name": "db"}

_MIGRATOR = {
    "name": "Migrator", "config": [], "requires": {"db": "Database"}, "provides": {},
    "body": [
        {"step": "let-effect", "bind": "lock",
         "acquire": _call(_REQ_DB, "query", "SELECT pg_advisory_lock(42)"),
         "undo": _call(_REQ_DB, "query", "SELECT pg_advisory_unlock(42)")},
        {"step": "await", "expr": {"kind": "host", "fn": "Job.run",
                                   "args": [{"kind": "lit", "value": "migrations"}]}},
        {"step": "emit",
         "expr": _call(_REQ_DB, "execute", "INSERT INTO migration_log VALUES (42)"),
         "compensate": _call(_REQ_DB, "execute",
                             "DELETE FROM migration_log WHERE id = 42")},
    ],
}

# a component whose undo calls its required service — the R3 readability clause
_DB_LOCK = {
    "name": "DbLock", "config": [], "requires": {"db": "Database"}, "provides": {},
    "body": [
        {"step": "let-effect", "bind": "lock",
         "acquire": _call(_REQ_DB, "execute", "SELECT pg_advisory_lock(42)"),
         "undo": _call(_REQ_DB, "execute", "SELECT pg_advisory_unlock(42)")},
    ],
}


def _failing_body(with_await: bool):
    body = [
        {"step": "let-effect", "bind": "scratch",
         "acquire": {"kind": "host", "fn": "Map.new", "args": []},
         "undo": _call({"kind": "name", "id": "scratch"}, "drop")},
    ]
    if with_await:
        body.append({"step": "await", "expr": {"kind": "host", "fn": "Job.run",
                                               "args": [{"kind": "lit", "value": "warmup"}]}})
    body.append(
        {"step": "let-effect", "bind": "pool",
         "acquire": {"kind": "host", "fn": "Pool.open",
                     "args": [{"kind": "lit", "value": "boom://nope"},
                              {"kind": "lit", "value": 1}]},
         "undo": _call({"kind": "name", "id": "pool"}, "close")})
    return {"name": "Failing", "config": [], "requires": {}, "provides": {},
            "body": body}


def _v1_ir(*components):
    return {"ir_version": 1, "services": _SERVICES_V1, "components": list(components)}


def _reference_ir() -> dict:
    path = _REPO / "examples" / "user_cache.ir.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------

class PyAdapter(RuntimeAdapter):
    name = "cordis-py"

    _CASES = {
        "r1_lifo_recovery", "r2_reactive_resolution", "r3_withdrawal_ordering",
        "r4_no_residue", "r5_derived_withdrawal", "a1_divert_at_boundary",
        "a5_compensate_lifo", "a8_sync_failure_contained", "a8_async_body_failure",
        "g7_lifo_complete_teardown",
    }

    def __init__(self) -> None:
        self._emit, self._rt, self._Context, self._FiberState = _import_backend()
        # best-effort runtime version string
        try:
            import cordis  # type: ignore
            self.runtime_version = getattr(cordis, "__version__", "harden-fiber-lifecycle")
        except Exception:
            self.runtime_version = "unknown"

    # -- adapter protocol ---------------------------------------------------

    def supports(self, case_id: str) -> bool:
        return case_id in self._CASES

    def run(self, case_id: str) -> Observation:
        handler = getattr(self, f"_case_{case_id}", None)
        if handler is None:
            return Observation.unsupported(f"no handler for {case_id!r}")
        return asyncio.run(handler())

    # -- trace plumbing -----------------------------------------------------

    def _new_trace(self) -> list[str]:
        events: list[str] = []
        self._rt.set_trace(events.append)
        return events

    def _ops(self, events: list[str]) -> list[str]:
        return [_strip(e) for e in events]

    def _errors(self, root) -> list:
        calls: list = []
        root.logger.error = lambda *a, **k: calls.append(a)
        return calls

    # -- R1 -----------------------------------------------------------------

    async def _case_r1_lifo_recovery(self) -> Observation:
        events = self._new_trace()
        try:
            module = _load_module(self._emit, self._emit.emit(_reference_ir()))
            root = self._Context()
            errors = self._errors(root)
            fdb = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
            fcache = root.plugin(module.UserCache)
            await fdb
            await fcache
            await _flush()
            cache = root.get("cache")
            cache.put("k1", "v1")
            cache.put("k2", "v2")
            await fcache.dispose()
            await _flush()
            return Observation(trace=self._ops(events), errors=len(errors))
        finally:
            self._rt.set_trace(None)

    # -- R2 -----------------------------------------------------------------

    async def _case_r2_reactive_resolution(self) -> Observation:
        events = self._new_trace()
        try:
            module = _load_module(self._emit, self._emit.emit(_reference_ir()))
            root = self._Context()
            states: dict[str, str] = {}
            fcache = root.plugin(module.UserCache)
            await _flush()
            states["cache@pending"] = fcache.state.name

            fdb = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
            await fdb
            await fcache
            await _flush()
            states["cache@active"] = fcache.state.name
            root.get("cache").put("alice", "42")

            await fdb.dispose()
            await _flush()
            states["cache@withdrawn"] = fcache.state.name

            fdb2 = root.plugin(module.PgDatabase, {"url": "postgres://replica:5432/app"})
            await fdb2
            await fcache
            await _flush()
            states["cache@reactivated"] = fcache.state.name
            # fresh store: the value written before the swap is gone
            if root.get("cache").get("alice") is None:
                events.append("cache.miss:alice")
            return Observation(trace=self._ops(events), states=states)
        finally:
            self._rt.set_trace(None)

    # -- R3 -----------------------------------------------------------------

    async def _case_r3_withdrawal_ordering(self) -> Observation:
        events = self._new_trace()
        try:
            ref = _reference_ir()
            ir = copy.deepcopy(ref)
            ir["components"] = [ref["components"][0], ref["components"][1],
                                copy.deepcopy(_DB_LOCK)]
            module = _load_module(self._emit, self._emit.emit(ir))
            root = self._Context()
            errors = self._errors(root)
            fdb = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
            fcache = root.plugin(module.UserCache)
            flock = root.plugin(module.DbLock)
            await fdb
            await fcache
            await flock
            await _flush()
            root.get("cache").put("alice", "42")
            await fdb.dispose()
            await _flush()
            return Observation(trace=self._ops(events), errors=len(errors))
        finally:
            self._rt.set_trace(None)

    # -- R4 -----------------------------------------------------------------

    async def _case_r4_no_residue(self) -> Observation:
        events = self._new_trace()
        try:
            module = _load_module(self._emit, self._emit.emit(_reference_ir()))
            root = self._Context()
            hooks_before = self._hook_total(root)
            disp_before = root.fiber._disposables.length

            fdb = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
            fcache = root.plugin(module.UserCache)
            await fdb
            await fcache
            await _flush()
            root.get("cache").put("alice", "42")
            await fcache.dispose()
            await fdb.dispose()
            await _flush()

            residue = {
                "store": len(root.reflect.store),
                "registry": root.registry.size,
                "hooks": self._hook_total(root) - hooks_before,
                "disposables": root.fiber._disposables.length - disp_before,
            }
            return Observation(trace=self._ops(events), residue=residue)
        finally:
            self._rt.set_trace(None)

    def _hook_total(self, root) -> int:
        return sum(len(cbs) for cbs in root.events._hooks.values() if cbs)

    # -- R5 -----------------------------------------------------------------

    async def _case_r5_derived_withdrawal(self) -> Observation:
        events = self._new_trace()
        try:
            module = _load_module(self._emit, self._emit.emit(_reference_ir()))
            root = self._Context()
            fdb = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
            fcache = root.plugin(module.UserCache)
            await fdb
            await fcache
            await _flush()
            await fdb.dispose()
            await _flush()
            residue = {"store": len(root.reflect.store)}
            states = {"cache": fcache.state.name}
            return Observation(trace=self._ops(events), residue=residue, states=states)
        finally:
            self._rt.set_trace(None)

    # -- A1 -----------------------------------------------------------------

    async def _case_a1_divert_at_boundary(self) -> Observation:
        events = self._new_trace()
        try:
            self._rt.Job.reset()
            module = _load_module(self._emit, self._emit.emit(_v1_ir(_DB, _MIGRATOR)))
            root = self._Context()
            root.plugin(module.Db)
            migrator = root.plugin(module.Migrator)
            reached = False
            for _ in range(60):
                await asyncio.sleep(0)
                if "job.run migrations start" in self._ops(events):
                    reached = True
                    break
            if not reached:
                return Observation.unsupported("migrator never reached its await step")
            migrator.dispose()
            await _flush()
            return Observation(trace=self._ops(events),
                               states={"migrator": migrator.state.name})
        finally:
            self._rt.set_trace(None)
            self._rt.Job.reset()

    # -- A5 -----------------------------------------------------------------

    async def _case_a5_compensate_lifo(self) -> Observation:
        events = self._new_trace()
        try:
            self._rt.Job.reset()
            module = _load_module(self._emit, self._emit.emit(_v1_ir(_DB, _MIGRATOR)))
            root = self._Context()
            db = root.plugin(module.Db)
            migrator = root.plugin(module.Migrator)
            await _flush()
            migrator.dispose()
            await _flush()
            return Observation(trace=self._ops(events),
                               states={"db": db.state.name,
                                       "migrator": migrator.state.name})
        finally:
            self._rt.set_trace(None)
            self._rt.Job.reset()

    # -- A8 sync ------------------------------------------------------------

    async def _case_a8_sync_failure_contained(self) -> Observation:
        return await self._a8(with_await=False)

    async def _case_a8_async_body_failure(self) -> Observation:
        return await self._a8(with_await=True)

    async def _a8(self, with_await: bool) -> Observation:
        events = self._new_trace()
        try:
            self._rt.Job.reset()
            module = _load_module(
                self._emit, self._emit.emit(_v1_ir(_DB, _failing_body(with_await))))
            root = self._Context()
            db = root.plugin(module.Db)
            failing = root.plugin(module.Failing)
            await _flush()
            # for an async body the failing acquire is behind a Job await; give
            # it enough turns to attempt the pool.open
            for _ in range(60):
                await asyncio.sleep(0)
                if "pool.open refused boom://nope" in self._ops(events):
                    break
            await _flush()
            return Observation(trace=self._ops(events),
                               states={"db": db.state.name,
                                       "failing": failing.state.name})
        finally:
            self._rt.set_trace(None)
            self._rt.Job.reset()

    # -- G7 -----------------------------------------------------------------

    async def _case_g7_lifo_complete_teardown(self) -> Observation:
        events = self._new_trace()
        try:
            module = _load_module(self._emit, self._emit.emit(_reference_ir()))
            root = self._Context()
            errors = self._errors(root)
            fdb = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
            fcache = root.plugin(module.UserCache)
            await fdb
            await fcache
            await _flush()
            cache = root.get("cache")
            cache.put("k1", "v1")
            cache.put("k2", "v2")
            # withdraw the provider: cache tears down reactively, then the
            # provider's own inverse (pool.close) runs last
            await fdb.dispose()
            await _flush()
            return Observation(trace=self._ops(events), errors=len(errors))
        finally:
            self._rt.set_trace(None)


def build() -> PyAdapter:
    return PyAdapter()
