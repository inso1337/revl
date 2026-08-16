"""revl cordis-py backend demo.

Emits `examples/user_cache.ir.json`, loads PgDatabase + UserCache into a
cordis Context, exercises cache.put/get, swaps the Database provider for a
second instance at runtime, then unloads everything — printing an event log
that shows R2 (reactive resolution) and R3 (withdrawal ordering), and
asserting R1 (LIFO recovery) and R4 (no residue) at the end.

Run:  python demo.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import types

BACKEND = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

import emit  # noqa: E402
import runtime  # noqa: E402

from cordis import Context  # noqa: E402
from cordis.fiber import FiberState  # noqa: E402


def load_ir() -> dict:
    for candidate in (
        BACKEND.parent.parent / "examples" / "user_cache.ir.json",  # revl worktree layout
        BACKEND / "tests" / "user_cache.ir.json",  # vendored reference copy
    ):
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise SystemExit("reference IR not found")


def load_module(source: str) -> types.ModuleType:
    module = types.ModuleType("user_cache_emitted")
    module.__dict__["__file__"] = "<emitted>"
    exec(compile(source, "user_cache_emitted.py", "exec"), module.__dict__)
    return module


async def flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def main() -> None:
    log: list[str] = []

    def note(event: str) -> None:
        log.append(event)
        print(f"  [{len(log):02}] {event}")

    module = load_module(emit.emit(load_ir()))
    root = Context()
    runtime.set_trace(lambda event: note(f"host    | {event}"))
    root.on(
        "internal/status",
        lambda this, fiber, old: note(
            f"fiber   | {fiber.name}: {FiberState(old).name} -> {FiberState(fiber.state).name}"
        ),
    )

    def hook_snapshot() -> dict:
        return {name: len(cbs) for name, cbs in root.events._hooks.items() if cbs}

    baseline_hooks = hook_snapshot()
    baseline_disposables = root.fiber._disposables.length

    print("== load PgDatabase(primary) + UserCache ==")
    fiber_db = root.plugin(module.PgDatabase, {"url": "postgres://primary:5432/app"})
    fiber_cache = root.plugin(module.UserCache)
    await fiber_db
    await fiber_cache
    await flush()

    print("== exercise cache.put / cache.get ==")
    cache = root.get("cache")
    cache.put("alice", "42")
    cache.put("bob", "7")
    note(f'demo    | cache.get("alice") -> {cache.get("alice")!r}')

    print("== swap Database provider (R2/R3): unload primary … ==")
    await fiber_db.dispose()
    await flush()
    note(f'demo    | root.get("cache") while db missing -> {root.get("cache")!r}')

    print("== … and load the replacement (replica) ==")
    fiber_db2 = root.plugin(module.PgDatabase, {"url": "postgres://replica:5432/app"})
    await fiber_db2
    await flush()
    cache = root.get("cache")
    note(f'demo    | after swap cache.get("alice") -> {cache.get("alice")!r} (fresh store)')
    cache.put("alice", "43")

    print("== unload everything (R1/R4) ==")
    await fiber_cache.dispose()
    await fiber_db2.dispose()
    await flush()

    # R4 — no residue: the runtime's introspection shows nothing left behind
    assert root.registry.size == 0, "runtime registry still holds plugin runtimes"
    assert root.reflect.store == {}, "service registry still holds provisions"
    assert root.fiber._disposables.length == baseline_disposables, "root fiber holds leaked effects"
    assert hook_snapshot() == baseline_hooks, f"leaked listeners: {hook_snapshot()}"
    print("R4 OK — no bindings, listeners, or effects left behind")

    # R1 — the final teardown ran newest-first: put-undo before store.drop,
    # and the whole dependent teardown before the provider's pool.close
    tail = [event.split("| ", 1)[1] for event in log if event.startswith("host")][-3:]
    ops = [event.split(" ")[0].split(".")[-1] for event in tail]
    assert ops == ["remove", "drop", "close"], tail
    print(f"R1 OK — final teardown order: {ops} (LIFO)")


if __name__ == "__main__":
    asyncio.run(main())
