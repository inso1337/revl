from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Hashable, Iterable

_SHARED_REALM = ""

def _realm_of(entry: dict, key: str) -> str:
    return (entry.get("isolate") or {}).get(key, _SHARED_REALM)

def local_prereqs(entries: Iterable[dict],
                  subset: Iterable[str] | None = None) -> dict[str, list[str]]:
    entries = list(entries)
    by_name = {e["name"]: e for e in entries}
    members = set(subset) if subset is not None else set(by_name)

    provider_of: dict[tuple[str, str], str] = {}
    for entry in entries:
        if entry["name"] not in members:
            continue
        for key in entry.get("provides") or ():
            provider_of[(key, _realm_of(entry, key))] = entry["name"]

    prereqs: dict[str, list[str]] = {name: [] for name in members}
    for name in members:
        entry = by_name[name]
        seen: set[str] = set()
        for key in entry.get("inject") or ():
            provider = provider_of.get((key, _realm_of(entry, key)))
            if provider is not None and provider != name and provider not in seen:
                seen.add(provider)
                prereqs[name].append(provider)
    return prereqs

def sequential_prereqs(order: Iterable[str]) -> dict[str, list[str]]:
    order = list(order)
    return {name: ([order[i - 1]] if i else []) for i, name in enumerate(order)}

async def activate_concurrent(
    order: Iterable[Hashable],
    prereqs: dict,
    activate: Callable[[Hashable], Awaitable],
    *,
    on_event: Callable[[str, Hashable], None] | None = None,
) -> tuple[list[tuple[Hashable, object]], list[tuple[Hashable, BaseException]]]:
    order = list(order)
    done: dict[Hashable, asyncio.Event] = {n: asyncio.Event() for n in order}
    failed: set[Hashable] = set()
    completion: list[tuple[Hashable, object]] = []
    errors: list[tuple[Hashable, BaseException]] = []

    def emit(kind: str, name: Hashable) -> None:
        if on_event is not None:
            on_event(kind, name)

    async def run(name: Hashable) -> None:
        for prereq in prereqs.get(name, ("")):
            if prereq in done:
                await done[prereq].wait()
        if any(prereq in failed for prereq in prereqs.get(name, ())):
            failed.add(name)
            emit("skip", name)
            done[name].set()
            return
        emit("start", name)
        try:
            handle = await activate(name)
        except BaseException as exc:
            failed.add(name)
            errors.append((name, exc))
            emit("error", name)
            done[name].set()
            return
        completion.append((name, handle))
        emit("done", name)
        done[name].set()

    pending_tasks = set(order)
    running_tasks = {}

    while pending_tasks:
        ready_to_run = [n for n in pending_tasks if all(
            prereq in done and done[prereq].is_set() for prereq in prereqs.get(n, []))]

        if not ready_to_run:
            raise RuntimeError("Deadlock detected in dependency graph")

        for n in ready_to_run:
            running_tasks[n] = asyncio.create_task(run(n))
            pending_tasks.remove(n)

        await asyncio.wait(running_tasks.values(), return_when=asyncio.FIRST_COMPLETED)

    return completion, errors

async def teardown_lifo(
    completion: list[tuple[Hashable, object]],
    dispose: Callable[[Hashable, object], Awaitable],
) -> None:
    for name, handle in reversed(completion):
        await dispose(name, handle)
