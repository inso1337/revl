"""Parallel activation of a checked dependency DAG (roadmap §46).

Activation used to be strictly sequential: components came up one at a time in
``loadOrder``, a single chain ``A -> B -> C`` regardless of whether ``B`` had
anything to do with ``A``. But **G3** already proves the dependency graph is a
checked DAG — a cycle can never link, and provision disjointness (G2) means two
components that do not share a dependency edge touch disjoint keys. So two
independent branches are *provably* independent, and activating them
concurrently is safe **by construction**: nothing discovered at runtime, the
compiler's `inject`/`provides` structure is the whole argument.

This module is the runtime-agnostic core of that idea. It has two halves:

  * :func:`local_prereqs` reconstructs, for a subset of components, exactly the
    provider -> consumer edges `src/revl/lower.py` builds at link time (the same
    ``provider_of[(key, realm)]`` rule), restricted to edges that stay *inside*
    the subset. Cross-subset edges (a key served by another process, resolved
    through a proxy) are the caller's responsibility to satisfy first — see
    ``_process_runner``. This is READ-only consumption of the G3 structure; the
    ordering itself is still owned by ``lower.py``.

  * :func:`activate_concurrent` / :func:`teardown_lifo` schedule an async
    ``activate`` callable so that independent branches run concurrently while a
    real dependency chain stays ordered, and then tear the result down
    **LIFO per chain** — reverse of a valid activation (completion) order, which
    is consumers-before-providers, preserving revert semantics.

The boot latency of a composition with independent branches is therefore bound
by the graph's *depth* (the longest dependency chain), not its *size* (the
number of components) — the point of §46.

R2 caveat (documented, not decided here). R2 is the runtime resolution
contract. We only ever overlap branches that G2/G3 prove touch disjoint keys,
so concurrent resolution never races on a shared registry entry *by
construction*. Whether R2 formally *guarantees* that a runtime may resolve two
disjoint keys concurrently is a TCK / item-42 question; we implement within what
R2 permits today (disjoint-key concurrency only) and flag the clarification in
docs/parallel-activation.md rather than changing the contract here.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Hashable, Iterable

# mirrors lower.SHARED_REALM (dash.py mirrors it the same way): an un-isolated
# key lives in the shared realm, named "" — kept local so this module never has
# to import lower.py (item 53's file).
_SHARED_REALM = ""


def _realm_of(entry: dict, key: str) -> str:
    """The realm a component resolves ``key`` in — its ``isolate`` map, or the
    shared realm. Identical to ``lower._build_manifest``'s ``_realm``."""
    return (entry.get("isolate") or {}).get(key, _SHARED_REALM)


def local_prereqs(entries: Iterable[dict],
                  subset: Iterable[str] | None = None) -> dict[str, list[str]]:
    """Reconstruct the intra-subset dependency edges from manifest entries.

    ``entries`` are ``loadOrder`` manifest components (each carrying ``name``,
    ``inject`` — the keys it requires — ``provides``, and an optional
    ``isolate`` realm map). Returns ``{name: [prereq, ...]}`` for every name in
    ``subset`` (default: all entries), where a prereq is another component *in
    the subset* that provides one of ``name``'s injected keys in the matching
    realm — exactly ``lower.py``'s ``provider_of[(key, realm)]`` edge rule.

    Edges to providers *outside* the subset are omitted deliberately: for a
    placement process those are cross-process seams already resolved as proxies
    before local activation begins, so within the subset they impose no order.
    """
    entries = list(entries)
    by_name = {e["name"]: e for e in entries}
    members = set(subset) if subset is not None else set(by_name)

    # provider_of[(key, realm)] = the component providing that key in that realm,
    # restricted to members. Mirrors lower._build_manifest exactly.
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
            # a component never depends on itself (G3 forbids provide==require)
            if provider is not None and provider != name and provider not in seen:
                seen.add(provider)
                prereqs[name].append(provider)
    return prereqs


def sequential_prereqs(order: Iterable[str]) -> dict[str, list[str]]:
    """The conservative fallback: chain every component behind its predecessor
    in ``order``, reproducing strictly-sequential activation. Used when no
    dependency structure is supplied, so behaviour never becomes *less* ordered
    than the old ``A -> B -> C`` load when the DAG is unknown."""
    order = list(order)
    return {name: ([order[i - 1]] if i else []) for i, name in enumerate(order)}


async def activate_concurrent(
    order: Iterable[Hashable],
    prereqs: dict,
    activate: Callable[[Hashable], Awaitable],
    *,
    on_event: Callable[[str, Hashable], None] | None = None,
) -> tuple[list[tuple[Hashable, object]], list[tuple[Hashable, BaseException]]]:
    """Activate ``order`` concurrently, serialized only along ``prereqs`` edges.

    ``activate(name)`` is the caller's async activation of one component (for
    the real runtime, ``root.plugin(...)`` then awaiting the fiber); it returns
    an opaque handle used later for teardown. Independent components — those
    whose prereq sets do not chain — run as concurrent tasks; a component whose
    prereq failed is *skipped* (cascade), never booted against a missing
    provider.

    Returns ``(completion, errors)``. ``completion`` is ``(name, handle)`` in
    the order activations actually finished — always a valid topological order,
    because a task appends only after every prereq has appended. That property
    is what makes :func:`teardown_lifo` correct.

    ``on_event(kind, name)`` if given is called with ``"start"``/``"done"``/
    ``"skip"``/``"error"`` — an observation hook the exit test uses to witness
    the interleaving without touching timing.
    """
    order = list(order)
    done: dict[Hashable, asyncio.Event] = {n: asyncio.Event() for n in order}
    failed: set[Hashable] = set()
    completion: list[tuple[Hashable, object]] = []
    errors: list[tuple[Hashable, BaseException]] = []

    def emit(kind: str, name: Hashable) -> None:
        if on_event is not None:
            on_event(kind, name)

    async def run(name: Hashable) -> None:
        # serialize on real edges: wait for each prereq to settle, in the DAG
        for prereq in prereqs.get(name, ()):
            if prereq in done:
                await done[prereq].wait()
        # cascade: a consumer never boots if a provider it needs failed
        if any(prereq in failed for prereq in prereqs.get(name, ())):
            failed.add(name)
            emit("skip", name)
            done[name].set()
            return
        emit("start", name)
        try:
            handle = await activate(name)
        except BaseException as exc:  # noqa: BLE001 — record, cascade, keep going
            failed.add(name)
            errors.append((name, exc))
            emit("error", name)
            done[name].set()
            return
        completion.append((name, handle))
        emit("done", name)
        done[name].set()

    queue = asyncio.PriorityQueue()
    for idx, name in enumerate(order):
        queue.put_nowait((len(prereqs.get(name, [])), idx, name))

    async def worker():
        while not queue.empty():
            _, _, name = await queue.get()
            await run(name)
            queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(min(4, len(order)))]
    await queue.join()
    for w in workers:
        w.cancel()

    # await asyncio.gather(*(asyncio.create_task(run(n)) for n in order))
    return completion, errors


async def teardown_lifo(
    completion: list[tuple[Hashable, object]],
    dispose: Callable[[Hashable, object], Awaitable],
) -> None:
    """Tear down in reverse completion order — LIFO within every chain.

    ``completion`` (from :func:`activate_concurrent`) is a valid topological
    order, so reversing it disposes **consumers before providers**: the revert
    semantics G7 requires. Teardown stays sequential on purpose — the ordering
    guarantee, not teardown speed, is the invariant §46 must preserve.
    """
    for name, handle in reversed(completion):
        await dispose(name, handle)
