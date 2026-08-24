"""Parallel activation of independent DAG branches (roadmap §46).

Activation used to be a single strict chain ``A -> B -> C`` no matter whether
the components depended on one another. G3 proves the dependency graph is a
checked DAG, so independent branches are provably independent and may activate
concurrently while real dependency edges stay ordered; teardown stays LIFO
within each chain (revert semantics). These tests prove all three without the
cordis runtime, by driving the runtime-agnostic scheduler in
``src/revl/activation.py`` directly.

The concurrency proof is deadlock-shaped, not timing-shaped: two independent
branches each block until the *other* has started, so they can only both make
progress if they are genuinely in flight at the same time. Were activation
sequential, the first would block forever and the whole run would time out —
the test would fail, never flake green.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.activation import (  # noqa: E402
    activate_concurrent,
    local_prereqs,
    sequential_prereqs,
    teardown_lifo,
)


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


# --------------------------------------------------------------------------- #
# 1. the DAG reconstructed from the compiler's inject/provides structure (G3)
# --------------------------------------------------------------------------- #

def test_local_prereqs_matches_g3_edges():
    entries = [
        {"name": "A", "inject": [], "provides": ["k"]},
        {"name": "B", "inject": [], "provides": ["m"]},         # independent branch
        {"name": "C", "inject": ["k"], "provides": []},          # C depends on A
        {"name": "D", "inject": ["k", "m"], "provides": []},     # D depends on A and B
    ]
    prereqs = local_prereqs(entries)
    assert prereqs["A"] == [] and prereqs["B"] == []
    assert prereqs["C"] == ["A"]
    assert sorted(prereqs["D"]) == ["A", "B"]


def test_local_prereqs_restricted_to_subset():
    """A cross-process provider is resolved as a proxy before local activation,
    so an edge to a provider outside the subset imposes no intra-process order."""
    entries = [
        {"name": "Ext", "inject": [], "provides": ["k"]},   # lives in another process
        {"name": "C", "inject": ["k"], "provides": []},
    ]
    # C alone in its process: the k-edge points outside the subset -> no local dep
    assert local_prereqs(entries, subset=["C"]) == {"C": []}


def test_realm_isolation_breaks_the_edge():
    """Same key, different realms is the multi-tenancy feature, not a dep — the
    edge only exists when the consumer's realm matches the provider's (G2/G3)."""
    entries = [
        {"name": "P", "inject": [], "provides": ["x"], "isolate": {"x": "r1"}},
        {"name": "Q", "inject": ["x"], "provides": [], "isolate": {"x": "r2"}},
    ]
    assert local_prereqs(entries) == {"P": [], "Q": []}


# --------------------------------------------------------------------------- #
# 2. independent branches activate concurrently; the chain stays ordered
# --------------------------------------------------------------------------- #

def test_independent_branches_activate_concurrently_chain_stays_ordered():
    # A, B independent roots; C depends on A. B must be able to start while A is
    # still activating (concurrency); C must start only after A finished (order).
    prereqs = {"A": [], "B": [], "C": ["A"]}
    order = ["A", "B", "C"]

    a_started = asyncio.Event()
    b_started = asyncio.Event()
    events: list[str] = []

    async def activate(name):
        events.append(f"start {name}")
        if name == "A":
            a_started.set()
            # A only completes once B has also started => they overlapped.
            await asyncio.wait_for(b_started.wait(), timeout=5)
        elif name == "B":
            b_started.set()
            await asyncio.wait_for(a_started.wait(), timeout=5)
        events.append(f"done {name}")
        return f"handle-{name}"

    completion, errors = _run(activate_concurrent(order, prereqs, activate))

    assert errors == []
    # C only started after A was fully done: the dependency edge serialized it.
    assert events.index("done A") < events.index("start C")
    # A and B overlapped: B started before A finished (a real interleaving).
    assert events.index("start B") < events.index("done A")
    # completion is a valid topological order (A before C).
    names = [n for n, _ in completion]
    assert names.index("A") < names.index("C")
    assert set(names) == {"A", "B", "C"}


def test_boot_latency_is_depth_bounded_not_size_bounded():
    """A wide fan of independent components boots in ~one unit (depth 1), not
    N units (size N): the concrete §46 claim, timed."""
    import time

    n = 12
    unit = 0.05
    order = [f"C{i}" for i in range(n)]
    prereqs = {name: [] for name in order}  # all independent

    async def activate(name):
        await asyncio.sleep(unit)
        return name

    start = time.monotonic()
    completion, errors = _run(activate_concurrent(order, prereqs, activate))
    elapsed = time.monotonic() - start

    assert errors == []
    assert len(completion) == n
    # sequential would be n*unit (0.6s); concurrent is ~unit. Generous ceiling.
    assert elapsed < unit * (n / 2), f"looks sequential: {elapsed:.3f}s for {n} units"


# --------------------------------------------------------------------------- #
# 3. teardown stays LIFO within every chain (revert semantics preserved)
# --------------------------------------------------------------------------- #

def test_teardown_is_lifo_consumers_before_providers():
    # Chain A -> C -> E, plus an independent branch B -> D.
    prereqs = {"A": [], "B": [], "C": ["A"], "D": ["B"], "E": ["C"]}
    order = ["A", "B", "C", "D", "E"]

    async def activate(name):
        return f"h-{name}"

    completion, errors = _run(activate_concurrent(order, prereqs, activate))
    assert errors == []

    torn: list[str] = []

    async def dispose(name, handle):
        assert handle == f"h-{name}"
        torn.append(name)

    _run(teardown_lifo(completion, dispose))

    # every consumer is torn down before the provider it depended on (LIFO).
    assert torn.index("E") < torn.index("C") < torn.index("A")
    assert torn.index("D") < torn.index("B")
    # teardown is exactly the reverse of the activation (completion) order.
    assert torn == [n for n, _ in reversed(completion)]


# --------------------------------------------------------------------------- #
# 4. a failed provider cascades: its consumers are skipped, not booted blind
# --------------------------------------------------------------------------- #

def test_failed_provider_skips_dependents_independent_branch_survives():
    prereqs = {"A": [], "B": [], "C": ["A"], "D": ["C"]}
    order = ["A", "B", "C", "D"]
    seen = []

    async def activate(name):
        seen.append(name)
        if name == "A":
            raise RuntimeError("boom")
        return name

    completion, errors = _run(activate_concurrent(order, prereqs, activate))

    booted = {n for n, _ in completion}
    assert booted == {"B"}                      # independent branch still boots
    assert [n for n, _ in errors] == ["A"]      # A recorded its failure
    assert "C" not in seen and "D" not in seen  # cascade: never activated blind


# --------------------------------------------------------------------------- #
# 5. the conservative fallback: no DAG supplied => strict sequential chain
# --------------------------------------------------------------------------- #

def test_sequential_fallback_is_strictly_ordered():
    order = ["A", "B", "C"]
    prereqs = sequential_prereqs(order)
    assert prereqs == {"A": [], "B": ["A"], "C": ["B"]}

    live = []

    async def activate(name):
        live.append(("start", name))
        await asyncio.sleep(0)
        live.append(("done", name))
        return name

    completion, errors = _run(activate_concurrent(order, prereqs, activate))
    assert errors == []
    # strict A -> B -> C: no interleaving, each done before the next starts.
    assert live == [("start", "A"), ("done", "A"),
                    ("start", "B"), ("done", "B"),
                    ("start", "C"), ("done", "C")]
    assert [n for n, _ in completion] == ["A", "B", "C"]
