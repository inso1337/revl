# `revl analyze` — Petri-net reachability liveness

`revl analyze` derives a Petri net from a composition's IR and searches it for a
reachable dead state: a marking from which some component can never activate. It
answers a REACHABILITY question the structural guarantees do not. G2 keeps
provisions disjoint per `(key, realm)`, G3 keeps the provider graph acyclic, G7
makes teardown LIFO-complete — none of them can see an interleaving in which a
composition that is structurally admissible still reaches a dead state at
runtime. This is roadmap item 438.

The engine (`src/revl/petri.py`) is dependency-free structural mathematics —
nets, markings, enablement, bounded reachability, and P-semiflows by
Martinez-Silva signed elimination — and knows nothing about revl. The derivation
and the report (`src/revl/liveness.py`) are the only revl-aware half.

## What it catches

A single-consumer coeffect contended by two consumers. One provider mints one
token; two consumers each need it; whichever activates first strands the other.
That interleaving is exactly the gap item 130's pointwise admission rules 3.1 and
3.6 cannot see (each source checked in isolation), and G3 does not model it
because it is not a cycle. The report names both stranded activations and the
rival that took the token:

```
DEADLOCK: a reachable state strands one or more activations
  @ConsumerA never activates: waits on `ticks`, a single-consumer coeffect taken by @ConsumerB
  @ConsumerB never activates: waits on `ticks`, a single-consumer coeffect taken by @ConsumerA
```

## The three open questions the issue named

### 1. What is a place — a key, a `(key, realm)` pair, or a coeffect?

A `(key, realm)` pair. This is G2's own unit of disjointness (426 §1.1): the
same key in two realms is the sanctioned multi-tenant shape, the same key in one
realm is the conflict G2 refuses. A bare key would fuse two providers that G2
keeps apart into one place and invent contention that does not exist; a coeffect
is finer than the composition graph resolves. The realm is `<shared>` when
unnamed, mirroring `lower.py`'s `_realm`.

A provision carries one of two arc kinds into a consumer's activation:

- an ordinary **service** is SHARED — the consumer READS it (a test arc), so any
  number of consumers coexist. An acyclic (G3-clean) graph of shared provisions
  is monotone and every activation completes, which is why every composition in
  `examples/` analyzes LIVE.
- a **consumable coeffect** — a single-consumer stream/subscription — is a real
  token the consumer CONSUMES. Two consumers of a capacity-1 coeffect contend.

An activation is a fire-once transition: it consumes a per-component control
token seeded in the initial marking, READS its shared injects, CONSUMES its
consumable coeffects, and PRODUCES its own provisions plus a `done` token. A
`requires` with no in-composition provider is a host-injected ambient coeffect
(item 350's boot contract), seeded available in the initial marking — an unmet
requirement is admission's job, not liveness's, so seeding it keeps a consumer of
an ambient capability (`log`, a clock) from being a false positive.

A composition is LIVE when every reachable quiescent marking has spent every
control token (every component activated). A reachable dead marking that still
holds a control token is a DEADLOCK — the component that never activated.

### 2. The bound

The marking graph is searched by bounded BFS with two caps: a state count
(`--max-states`, default 20000) and a per-place token count (`--max-tokens`,
default 64). A verdict reached strictly within the bound is exact. A search that
HITS the bound reports **inconclusive** and says so — it never returns "no
deadlock" as a guarantee (item 418: a check must not claim more than it
establishes). Where the graph is too large to exhaust, P-semiflows certify
structural boundedness, so the report can at least state the search space is
finite even when it was not fully walked:

```
inconclusive: search hit the bound before the marking graph was exhausted
no deadlock found WITHIN the bound (certified bounded via P-semiflows) -- this is a partial result, not a liveness guarantee (item 418)
```

### 3. Refuse or warn

Report-only. `revl analyze` is NOT wired into the admission gate. A false
positive that blocks a legal composition is worse for adoption than the deadlock
it would have prevented, and the false-positive rate is not yet measured across a
large corpus — only that it is zero on the one shipped here. So it reports the
cycle and lets the operator decide. It exits nonzero on a PROVEN dead state (so
CI can consume it) and zero on an inconclusive bound. It becomes a gate only once
the false-positive rate is measured and shown to be zero, exactly the sequencing
items 427 and 428 followed for their checks.

## Why the corpus is all LIVE (and that is the honest result)

Cross-component consumable provisions are not yet expressible in revl. A header
requires/provides cycle is refused by G3 before this analysis runs; a stream is
component-local and single-consumer, and rule 3.1 refuses a second subscription
of one source pointwise. So no composition in `examples/` derives a net with
contention, and all 35 analyze LIVE — zero false positives, which is the result,
not a gap. The consumable classification (`consumable` on a provider entry) is
the seam this analysis is READY for: the day a multicast bridge (item 130 §4.1)
lets a provided stream be shared, its provider marks the key consumable and this
analysis is the guard that keeps two consumers from silently starving. The
`tests/data/deadlock_consumable.ir.json` fixture is that shape, and
`tests/test_liveness.py` proves the analysis catches it and — removing the
classification — that the report is the modeling doing real work, not an
artifact.

## Usage

```
revl analyze FILE.rvl [FILE.rvl ...]     # compile and analyze
revl analyze --ir DOC.json               # analyze a precompiled composition IR
revl analyze FILE.rvl --json             # machine-readable
revl analyze FILE.rvl --max-states N --max-tokens N
```
