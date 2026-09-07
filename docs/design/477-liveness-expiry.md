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

## Follow-up 1: the declared liveness expectation (grammar and gate) — LANDED

Operator-visible and gate-enforced means a source-level declaration, a lease
ceiling attached per activation, in the shape `cache ... ttl` already
established: a duration literal, lowered to additive IR metadata, refused by a
G-rule when it is declared on an activation that cannot hang (one with no
emission or host-call reach has nothing to be silent about, so a ceiling on it
is a category error worth refusing rather than silently ignoring). This is
grammar plus parser plus lowering plus one admission check, the same surface
item 310 touched.

This slice landed, together with the runtime producer that wires the declaration
into the vocabulary the note above added:

* **Grammar + parser.** A contextual `liveness <dur>` clause in the component
  header (after `requires`/`provides`, before `{`), read only in that slot so the
  word is untouched as an ordinary name elsewhere. It funnels through the same
  `policy._parse_ttl` duration surface as `cache ... ttl` (a shared
  `_duration_literal`), and a non-positive ceiling is refused at parse.
* **Lowering.** The ceiling lowers to the additive IR key `liveness_ceiling_ms`,
  conditionally present, so a component that declares none is byte-identical
  through the IR and every emitter.
* **The admission G-rule.** `lower._activation_can_hang` decides whether the
  activation body reaches an emission (`emit` step) or a host-call
  (`effect`/`let-effect`), pruning `provide` methods (they run per call, not at
  the activation transition). A ceiling on an activation that cannot hang is
  refused (`category="liveness"`).
* **The runtime producer.** `run._Driver._perform_liveness_expiry(component,
  silent_ms)` reads the declared ceiling from the IR, consults the landed
  `why_runtime.liveness_expired` gate (so a partial world fabricates no expiry),
  and — when the silence genuinely breaks the ceiling — withdraws the hung
  provider with a `cause_liveness_expired` ROOT while its dependents cascade
  through the ordinary `provider-withdrawn` edge. `_withdraw_cause` gained an
  optional `root_cause` override for the target so the QUIET root replaces the
  operator `trigger` without touching the dependent edges. One small vocabulary
  wiring change went with it: `why_runtime.actual_cascade` now recognises a
  `liveness-expired` root as an ORIGINATING withdrawal (as it already did the
  operator `trigger`), so the differential oracle observes a hung-provider
  expiry cascade exactly as it observes an operator withdrawal.

`tests/test_477_liveness_ceiling.py` pins all four: the ceiling lowers and its
units match the `cache ttl` surface, a ceiling on a non-hangable activation is
refused, `liveness` stays a plain name outside the header slot, and the producer
withdraws a hung provider with a liveness-expiry root that is DISTINCT from a
fault (which carries a diagnostic `code`) while the dependent still cascades. The
producer's firing path runs with the fiber-settle boundary stubbed (the boundary
cordis owns); a `@needs_cordis` end-to-end boots it on the real driver in CI.

Silence DETECTION — a running heartbeat that measures `silent_ms` and calls the
producer on its own — was deliberately not in the slice above. It is now WIRED
(issue #622).

## Production silence detection — LANDED (issue #622)

`run._LivenessMonitor` is the OWNED observer. It enrols every declared-ceiling
provider the composition can be watched for, times each against an injectable
clock (`time.monotonic` in production, a hand-advanced clock under test), and
when a provider makes no sign of life for longer than its declared ceiling it
invokes `_perform_liveness_expiry` with a `silent_ms` IT computes — no test
supplies the duration. The driver arms it BEFORE `_load` so its background poll
interleaves with each `_drive_activation` await and can time a provider that
never reaches ACTIVE; `_dispose_all` cancels and AWAITS the poll task, so a
cancelled or unloaded generation leaves no independent watcher behind (a bare
asyncio task on the reactive loop, never an OS timer or a thread).

The honest scope is stated, not over-promised. The observer bounds ACTIVATION
silence: a provider that reaches ACTIVE has answered (its ACTIVE transition ends
the watch), a provider merely waiting on an unmet peer require is not charged its
silence (that hang is the peer's), and steady-state per-call silence is out of
scope (it needs a per-call progress signal the runtime does not emit). Two shapes
are REFUSED rather than watched with a clock that would lie about them
(`_liveness_observability`): a multi-realm `routes` bind (its liveness spans
realms this process does not drive — a **shared-clock** case), and an activation
that can hang on a cross into a required service (a **blocking** wait on a peer,
not this provider's own silence). `tests/test_622_liveness_monitor.py` pins the
classifier, the enrolment/refusal split, the compute-its-own-duration expiry, the
healthy/unrelated/waiting non-expiry, and the no-watcher-left settle.

## Follow-up 2: reconcileLivenessFromWorld on restart — LANDED (issue #624)

On restart the in-memory expected-liveness map is stale: it describes the world
the crashed process believed in, not the one now running.
`reconcile.reconcile_liveness_from_world` (the design's `reconcileLivenessFromWorld`)
rebuilds only the EXPECTED liveness of the restarting composition's
declared-ceiling providers from durable world evidence — the tier-agnostic WAL
(`wal.read_wal`), the E-Stop latch (`estop.read_latch`, item 443) and a durable
causal trace (`why_runtime`) — rather than trusting the stale map. It is pure and
side-effect free: it reads the world and returns a verdict, withdrawing nothing
and adopting nothing.

The one rule: it never re-adopts a provider as freshly live and never touches an
owner it does not manage. The strongest thing durable evidence yields about a
managed provider is a NEGATIVE or an UNKNOWN — `expired` (a durable trace attests
a `LIVENESS_EXPIRED` withdrawal), `halted` (an armed E-Stop latch), or
`unresolved` (everything else: valid, stale, partial and contradictory state all
collapse here and must be re-observed by the #622 observer once the generation
re-activates). There is deliberately no `live` verdict: "the crashed process
recorded activation-complete" means it BELIEVED the generation live, a belief
stale the instant the process died, so it yields `unresolved`. A version-
unsupported or mid-file-corrupt WAL FAILS CLOSED (every managed provider
`unresolved`, the integrity error as the reason) rather than reading a possibly-
forged world; a component named in residue but not managed here is reported
under `foreign` and left alone. `tests/test_624_liveness_reconcile.py` exercises
the valid/stale/partial/unreadable crash cases, the expired/halted negatives and
the no-cleanup-of-foreign-owners rule.

## Relates to

* Item 438 (Petri reachable-dead-state analyzer): the static sibling. A hung
  provider is a reachable dead state 438 would flag at compile time; this is the
  runtime observation of the same shape.
* Item 443 (operator E-Stop): the durable residue records the E-Stop strands
  are the world follow-up 2 reconciles against.
* Item 310 (capability-aware caching): the lease/ttl the spike distinguishes
  this from, and the grammar shape follow-up 1 mirrors.
