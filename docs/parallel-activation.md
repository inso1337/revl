# Parallel activation — boot scales with the graph's depth, not its size

Roadmap §46. Implemented in `src/revl/activation.py`, consumed by
`src/revl/_process_runner.py` (per-process activation) and driven by
`src/revl/placement.py` (which reconstructs the DAG the runner obeys).

## The problem

Activation was strictly sequential. A process brought its components up one at a
time in `loadOrder` — a single chain `A -> B -> C` — regardless of whether `B`
had any dependency on `A`. Boot latency was therefore bound by the **size** of
the composition (the number of components), even when most of them were
independent and could have come up at once.

## The G3 argument (why concurrency is safe by construction)

We do **not** discover dependencies at runtime. The compiler already knows the
whole structure:

- **G3** (`src/revl/lower.py::_build_manifest`) proves the dependency graph is a
  checked, acyclic DAG: a cycle can never link. Every provider → consumer edge
  is the link-time fact `provider_of[(key, realm)]` — component *B* depends on
  *A* iff *A* provides, in *B*'s realm, a key *B* injects.
- **G2** provision disjointness means two components with no dependency edge
  between them touch **disjoint keys** — same key in different realms is the
  multi-tenancy feature, not a shared dependency.

So two independent DAG branches are *provably* independent: activating them
concurrently cannot race, because there is no shared mutable resolution between
them. Independence is a compile-time property, read straight off `inject` /
`provides`, not a runtime guess.

`activation.local_prereqs` reconstructs exactly `lower.py`'s edge rule
(`provider_of[(key, realm)]`) for a subset of components. It is a **read-only
consumer** of the G3 structure — the ordering itself stays owned by `lower.py`.
Edges to providers *outside* the subset (a key served by another placement
process) are omitted on purpose: those are resolved as proxies **before** local
activation begins, so within the process they impose no order.

## What now happens

`activation.activate_concurrent(order, prereqs, activate)` schedules each
component as an async task that awaits only its real prerequisites and then
activates. Independent branches run at the same time; a dependency chain stays
serialized along its edges. Boot latency becomes bound by the graph's **depth**
(its longest dependency chain), not its **size**.

The same argument parallelizes the placement boot **across** processes:
`placement.py` already spawns every process concurrently, and each proxy retries
its provider's seam, so cross-process boot was already depth-bounded. §46 closes
the remaining gap — the strictly-sequential activation *inside* each process.

### Two levels

| level | file | before | after |
|-------|------|--------|-------|
| within a process | `_process_runner.py` step 2 | sequential over `loadOrder` | concurrent over the intra-process DAG (`spec["depends"]`) |
| across processes | `placement.py` | already concurrent spawn | unchanged (already depth-bounded) |

`placement.py` computes `spec["depends"]` per process from the manifest's G3
structure and ships it in the process spec. A spec written before §46 (no
`depends` field) falls back to `activation.sequential_prereqs` — the old strict
`A -> B -> C` chain — so nothing ever becomes *less* ordered than before. The
non-Python tiers ignore the field and keep their existing sequential runners;
§46's concurrency is implemented on the py tier.

## The LIFO-teardown invariant (G7 revert semantics preserved)

`activate_concurrent` returns the components in **completion order** — the order
their activations actually finished. Because a task appends to that list only
after every prerequisite has appended, the completion order is **always a valid
topological order**, even though activation ran concurrently.

`activation.teardown_lifo` disposes that list in **reverse**. Reversing a valid
topological order yields **consumers-before-providers**, which is exactly LIFO
within every chain — the revert semantics G7 requires. Teardown itself stays
**sequential**: the ordering guarantee, not teardown speed, is the invariant §46
must preserve. A failed provider **cascades** — its dependents are skipped, not
booted against a missing provider — so teardown only ever runs over components
that genuinely came up.

The exit test (`tests/test_parallel_activation.py`) proves all of it without the
cordis runtime: two independent branches each block until the other has started
(a deadlock-shaped concurrency proof — sequential activation would time out),
a dependent stays ordered behind its provider, a wide fan boots in ~depth time,
and teardown comes back in strict consumer-before-provider LIFO order.

## R2 assumption (flagged, not decided here)

**R2** is the runtime resolution contract. The roadmap raises the question:
*does R2 permit concurrent resolution?*

We implement **within what R2 allows today**: we only ever overlap branches that
G2/G3 prove touch **disjoint keys**, so concurrent resolution never races on a
shared registry entry *by construction*. Under a single event loop, the tasks
interleave only at `await` boundaries, and each resolves its own disjoint keys.

What is **not** settled — and is deliberately left to the TCK / item-42, **not**
changed unilaterally here — is whether R2 *formally guarantees* that a
conforming runtime may resolve two disjoint keys concurrently (versus merely not
forbidding it). Our assumption: **resolution of provably-disjoint keys is safe to
overlap.** If a runtime's `resolve`/`plugin` shares mutable state across disjoint
keys, that runtime would need to serialize internally; the scheduler's contract
(never overlap keys that share a dependency edge) is what keeps the assumption
sound. This should be pinned down as an explicit R2 clause in the runtime
contract (item 42) — flagged here rather than assumed silently.
