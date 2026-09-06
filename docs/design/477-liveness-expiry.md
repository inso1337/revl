# 477: Silence-driven liveness expiry for hung providers

Design note for roadmap item 477. It records the spike that pins the gap, the
one slice that lands with this note, and the two follow-ups that stay design
only until they are scoped on their own.

Item 438 asks a compile-time question: can a structurally admissible
composition reach a dead state. This item is its runtime complement. A hung
provider is exactly a reachable dead state, but one that only the running world
can observe, because nothing in the IR says a live provider will ever answer.

## The spike: what the existing lease gates already enforce

The lease and ttl mechanism lives in `src/revl/lower.py` (the item-310 cache
admission checks, `_check_cache_freshness` and around it). Read directly, what
it governs is GRANT liveness, not provider heartbeat:

* `cache capability` (`lower.py`, the `capability` freshness branch): a
  capability result's freshness bound "IS its authorizing scope (the grant +
  generation liveness), narrowable only by `ttl`". The ttl says how long a
  cached capability read may be served before the grant must be reconsulted.
* `cache external`: an external read must carry `invalidated_by` and/or `ttl`,
  because an external value changes without notice.

Both narrow the window in which a already-produced RESULT may be reused. Both
are decided at the call, before execution, by the seam consent gate. Neither
watches a call that has been dispatched and has not returned. A `ttl` shortens
how long an answer stays fresh; it does not detect a stuck host-call, because
the stuck call produced no answer to age. The lease vocabulary has no term for
"the provider is still there and still has not said anything."

That silence is the gap. revl withdrawal today is FAULT-driven: a provider that
faults settles into `FAILED`, its withdrawal carries a
`why_runtime.cause_trigger` with a classifiable diagnostic `code`, and
dependents tear down through the `provider-withdrawn` cascade
(`run.py._withdraw_cause`). The QUIET case, a provider that neither fails nor
answers (deadlock, stuck host-call, partition with no failure signal), produces
no fault, so nothing withdraws and dependents wait forever on a provider that
will never serve.

## The slice that lands with this note

The runtime complement needs a way to say "withdrawn because it went silent,"
distinct from "withdrawn because it faulted." That distinction is the part that
can land now with no runtime, no grammar and no formal-layer change, because it
is pure vocabulary plus the decision the vocabulary encodes. Two additions to
`src/revl/why_runtime.py`:

1. **A distinct withdrawal cause.** `LIVENESS_EXPIRED` joins the cause kinds,
   and `cause_liveness_expired(ceiling_ms, silent_ms)` builds it. It is a ROOT
   cause, like `boot` and `trigger`: the cause-chain walk stops at it, the
   dependents above it keep their ordinary `provider-withdrawn` edges, and
   `render_chain` names it a root cause. It carries the operator-visible
   accounting, the declared `ceilingMs` and the observed `silentMs`, and it
   carries NO diagnostic `code`. An expiry classifies no `RevlError` because
   there is no error to classify, and a fabricated code would let the QUIET
   case masquerade as a fault, which is exactly the confusion this item exists
   to remove.

2. **The pure gate.** `liveness_expired(ceiling_ms, silent_ms)` is the decision
   a producer of the cause consults: strictly `silent_ms > ceiling_ms`, and
   defensive so a partial world never fabricates an expiry (a non-positive or
   absent ceiling can never expire; an absent silence reading is not-yet-expired
   rather than an infinite silence).

`tests/test_477_liveness_expiry.py` pins both: the gate fires only past the
ceiling and stays quiet on a partial world, the cause is distinct in kind from
a fault and from a provider-withdrawal, a hung provider's withdrawal roots at
the expiry while its dependent still cascades through the ordinary edge, and the
render shows the accounting.

This is deliberately the vocabulary and the decision, not a producer wired into
a running composition. Every consumer of the trace (`revl why`, the withdrawal
oracle, `metrics`, `otel`) already treats an unknown root kind generically, so
the new kind slots in without touching them, and any producer built by the
follow-ups below emits into a vocabulary that already exists and is already
tested.

## Follow-up 1: the declared liveness expectation (grammar and gate)

Operator-visible and gate-enforced means a source-level declaration, a lease
ceiling attached per activation, in the shape `cache ... ttl` already
established: a duration literal, lowered to additive IR metadata, refused by a
G-rule when it is declared on an activation that cannot hang (one with no
emission or host-call reach has nothing to be silent about, so a ceiling on it
is a category error worth refusing rather than silently ignoring). This is
grammar plus parser plus lowering plus one admission check, the same surface
item 310 touched, and it should land as its own slice with its own falsification
tests rather than riding this note.

## Follow-up 2: reconcileLivenessFromWorld on restart

The larger piece. On restart the in-memory expected-liveness map is stale: it
describes the world the crashed process believed in, not the one that is now
running. `reconcileLivenessFromWorld` rebuilds the expected liveness from the
durable world (the WAL and the residue records the E-Stop already writes, item
443) rather than trusting the stale map, so a provider that went silent while
the supervisor was down is not silently re-adopted as live. This touches the
restart path, the WAL schema and the formal accounting, and is comparable in
size to the E-Stop itself. It stays design only until it is scoped on its own.

## Relates to

* Item 438 (Petri reachable-dead-state analyzer): the static sibling. A hung
  provider is a reachable dead state 438 would flag at compile time; this is the
  runtime observation of the same shape.
* Item 443 (operator E-Stop): the durable residue records the E-Stop strands
  are the world follow-up 2 reconciles against.
* Item 310 (capability-aware caching): the lease/ttl the spike distinguishes
  this from, and the grammar shape follow-up 1 mirrors.
