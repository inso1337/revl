# Design: a Temporal emission target (item 253)

Status: design proposed. No implementation. This note fixes the mapping,
states exactly what revl proves that Temporal cannot, decides the target-variant
mechanism, draws the honest boundary, records an adversarial self-review with one
CRITICAL, and slices the work. The exit test named by the roadmap (one saga demo
on a real Temporal dev server with an injected mid-saga fault compensating in
derived order) is the acceptance gate for Slice 1, refined by the CRITICAL below.

## The one thing to get right

Temporal has two cardinal rules that its own documentation can only ask users to
follow: workflow code must be deterministic, and all IO must live in activities.
revl already decides both at compile time. The stratum discipline splits every
composition into pure/effect code (no boundary crossing) and emissions (the
crossings). Map pure/effect onto the workflow and emissions onto activities and
the two rules stop being a runtime hope: revl refuses, as a compile error, the
exact program Temporal would accept and then trip on in production during replay.

The target is therefore not a new capability bolted onto revl. It is a rendering
of the discipline revl already enforces, aimed at an SDK whose users hand-write
the parts revl derives: the workflow/activity split, the compensation order, and
the decision of which activities are safe to retry.

## 1. The mapping

| revl | Temporal (TS SDK) | derived from |
|---|---|---|
| a component's `activation` body | a `workflow` function | the stratum split |
| pure / effect code in the body | ordinary deterministic workflow code | frontend refuses IO here |
| an `emit`/`let-effect` crossing | an `activity` call (`proxyActivities`) | emission = boundary crossing |
| `effect X undo Y` (243 witnessed / bracket) | activity `X`, inverse `Y` pushed on the saga stack | the `Frame` bracket/transactional entry |
| `emit X compensate Y` (247) | activity `X`, best-effort offset `Y` pushed on the saga stack | the `Frame` compensation entry |
| the G7 LIFO teardown order | the order compensations run on abort | `recovery.py` reverse-seq drain |
| an activation fault / abort | a workflow error triggering the saga | `Frame.drain` never reached |
| idempotency evidence (item 44) | each activity's `RetryPolicy` | `_idempotent_register` (Slice 2) |
| a refused construct (spawn, realm, reactive, lease, hot-swap) | (none) refused with a why-trace | the honest-scope boundary |

The load-bearing observation is that the TS emitter already builds this shape for
its native cordis runtime. `backends/typescript/emit.py` lowers a whole
activation body into a single generator with one `Frame` per activation, and the
`Frame` carries a single LIFO disposer stack with exactly three entry kinds
(`backends/typescript/runtime.ts`, the Frame section):

- `bracket` an acquisition's `undo`, replays on every teardown;
- `transactional` a `witnessed` extern's declared inverse (item 243);
- `compensation` an `emit ... compensate ...` best-effort offset (item 247),
  run in a second phase after every bracket/transactional inverse has finished.

That is a saga. The two-phase abort (`begin` yielded first so it disposes last;
`drain` yielded last so it disposes first and only on a clean unload) is the same
two-phase saga Temporal users write by hand. The Temporal target reuses the IR
walk and the LIFO accounting verbatim and changes only the sink: where the cordis
target renders a `Frame` entry against `ctx.effect`, the Temporal target renders
it against `proxyActivities` plus an explicit compensation stack.

### A concrete component and its emitted workflow

Source (illustrative, sketch):

```revl
// sketch: illustrative, not a compiled corpus fixture
component BookTrip {
  activation {
    // each crossing declares how to offset itself; the ORDER is source order
    let flight = emit flights.reserve(req.itinerary) compensate flights.cancel(flight)
    let hotel  = emit hotels.reserve(req.dates)      compensate hotels.cancel(hotel)
    emit payments.charge(req.card, req.total)         compensate payments.refund(req.card, req.total)
  }
}
```

Emitted Temporal TS workflow (illustrative, sketch of the intended output):

```typescript
// sketch: illustrative of the emit --target temporal shape, not verbatim output
import { proxyActivities } from '@temporalio/workflow'
import type * as activities from './activities'

// Slice 1 default: every activity at-most-once (maximumAttempts: 1). Slice 2
// relaxes this per activity from the item-44 idempotency register.
const { flightsReserve, flightsCancel,
        hotelsReserve,  hotelsCancel,
        paymentsCharge, paymentsRefund } = proxyActivities<typeof activities>({
  startToCloseTimeout: '1 minute',
  retry: { maximumAttempts: 1 },
})

export async function BookTrip(req: BookTripInput): Promise<void> {
  // The compensation stack IS the derived saga. Pushed in source order,
  // popped LIFO on abort, the order recovery.py drains, not hand-written.
  const saga: Array<{ name: string; run: () => Promise<void> }> = []
  try {
    const flight = await flightsReserve(req.itinerary)
    saga.push({ name: 'flights.cancel', run: () => flightsCancel(flight) })

    const hotel = await hotelsReserve(req.dates)
    saga.push({ name: 'hotels.cancel', run: () => hotelsCancel(hotel) })

    await paymentsCharge(req.card, req.total)
    saga.push({ name: 'payments.refund', run: () => paymentsRefund(req.card, req.total) })
    // clean completion: the saga stack is discharged, never run (Frame.drain)
  } catch (err) {
    // ABORT. Phase 2 (best-effort compensation): pop LIFO, continue-and-record.
    for (const step of saga.reverse()) {
      try { await step.run() }
      catch (e) { /* record compensation-residue; DO NOT abort the loop */ }
    }
    throw err
  }
}
```

Two things in that output are derived, not authored, and are the whole point:
the compensation stack is populated in source order and drained LIFO (the G7
order), and the compensation phase continues past a failed compensation and
records residue rather than aborting. A Temporal user writes both by hand and,
per the roadmap, gets them wrong.

## 2. Determinism: the rule Temporal only documents

Temporal enforces determinism at runtime, inside a replay sandbox that detects
divergence after the fact, in production, on the history of an already-running
workflow. revl enforces it at compile time. The mechanism is the stratum
discipline, unchanged: a boundary crossing is legal only in emission/effect
position, and the frontend refuses IO anywhere else (the same refusal that keeps
a `witnessed` call out of a plain `fn` body, `lower.py`
`_refuse_witnessed_outside_effect_position`). Everything that survives into the
workflow function is either pure computation or an emission node in the IR. The
emitter renders every emission node as an `await activity(...)` and has no path
to render an IO call inline in workflow position, because there is no such node
in the workflow stratum to render.

What revl proves that Temporal cannot:

1. **No IO in workflow position.** Temporal cannot know an activity from a raw
   host call until replay diverges; revl knows because the crossing is a distinct
   IR node the checker admitted only in emission position. This is a compile
   error in revl and a production incident in hand-written Temporal.
2. **The compensation order is correct by derivation.** Temporal cannot check
   that a hand-written saga pops in the right order; revl derives the LIFO order
   from source order via the same accounting `recovery.py` uses, so the emitted
   order is not a thing a human can get wrong.
3. **Which activities are safe to retry** (Slice 2). Temporal defaults every
   activity to unlimited retries and trusts the author to narrow it; revl carries
   per-emission idempotency evidence and can set `maximumAttempts` from proof.

The honest limit of claim 1: revl proves the SOURCE has no illegal IO in workflow
position. It does not and cannot prove Temporal's runtime replay determinism (see
the boundary, and self-review attack 4 for the one way a leak could still occur
through a non-deterministic builtin).

## 3. Retry policies from evidence (Slice 2)

Item 44's idempotency evidence rides on every emission and its inverse:
`idempotent`, `idempotency_key`, `undo_idempotent`, folded by
`lower.py::_idempotent_register` into a register with a partial order:

- `keyed` (an `idempotency_key` is present) dedup-safe by construction;
- `shape-proven` (a 244 revl-expressed restore-to-recorded-value inverse) proven
  safe to re-deliver, peer of `keyed`;
- `declared` (a bare `idempotent` claim over an opaque host body) the author's
  claim, machine-checked for shape only, NOT proven;
- absent (no claim) at-most-once, must not be re-delivered.

The map to a Temporal `RetryPolicy`:

| register | `RetryPolicy` | rationale |
|---|---|---|
| `keyed` / `shape-proven` | retries enabled, bounded backoff, high/unbounded `maximumAttempts` | proven safe to re-deliver |
| `declared` | `maximumAttempts: 1` in v1 (opt-in to relax) | an unverified claim is not proof |
| absent | `maximumAttempts: 1` | non-idempotent: a retry double-applies |

The subtlety the roadmap phrase "retries only where the type system says retry is
sound" hides: `declared` is a claim, not a proof (`_idempotent_register` says so
in its own docstring). Enabling Temporal retries on a `declared` activity trusts
an unverified author claim and, because Temporal WILL retry on transient failure,
amplifies a false claim into a production double-apply. So v1 treats `declared`
as at-most-once and only `keyed`/`shape-proven` earn retries. This is stricter
than a naive reading of the roadmap and it is deliberate.

Interaction with item 257: a `retry N` on a `validated` model emission is NOT
item 44's idempotent-delivery retry (`lower.py` says so explicitly: a completion
is a read-with-a-cost, not an idempotent write). It re-issues the completion
thunk revl-side and must not be lowered to a Temporal `RetryPolicy`; the model
activity keeps `maximumAttempts: 1` and the 257 retry, if rendered at all, is
workflow-side re-invocation. Slice 2 must keep these two retries distinct or it
will hand a completion to Temporal to retry as if idempotent.

## 4. The target-variant mechanism

`revl emit --target temporal` is a rendering mode of the existing TypeScript
emitter, not an eighth runtime and not a new tier under `backends/`.

Why TypeScript: Temporal's TS SDK is first-class (workflows are ordinary async
functions, activities are proxied functions), so the emitter's existing IR walk
maps onto it with the least impedance, and the TS emitter already carries the
saga machinery (the `Frame` with its three entry kinds and LIFO drain) that the
Temporal target reuses.

The seam: the backend contract is `emit(ir: dict) -> str` (`docs/backend-ir.md`),
loaded by `revl.bundle._emitter` and `revl.test._emitter`. The Temporal target
threads a target selector into that entry point, for example
`emit(ir, *, target="cordis"|"temporal")`, and reuses:

- the IR walk (`_expr`, the activation-body lowering, the method-body walk);
- the LIFO accounting, the three `Frame` entry kinds, and the two-phase order.

It changes only the sink: a `bracket`/`transactional` entry renders as a
compensation-stack push with its inverse; a `compensation` entry renders as a
Phase-2 push; an `emit` renders as `await proxyActivities(...)`; the activation
header renders as an exported workflow function plus an `activities.ts` of the
crossing implementations. The type checks, the stratum refusal, and every
per-emission evidence flag are inherited unchanged, because the IR is the same IR
every other backend consumes.

Honest note on precedent: there is no existing `--target` dimension and no `revl
emit` subcommand today. Emission happens through `revl bundle` (per-backend
`emit.py`) and `revl run` (a runtime tier from `KNOWN_BACKENDS`). This item adds
`emit --target` as a NEW dimension orthogonal to `--backend`: a target selects a
rendering of a backend's emitter, defaulting to that backend's native runtime,
with `temporal` available only for the TS emitter in v1. Wiring it as a target
(not a seventh `KNOWN_BACKENDS` entry) is what keeps it a variant rather than a
runtime: nothing new to boot, nothing new to place, no new tier in `run.py`.

## 5. The honest boundary

This target is a code-generation target. It emits a Temporal workflow and its
activities. It does not provide durable execution, and it must not be sold as if
it did.

- The durable execution, the history/WAL (item 47's WAL, operated by someone
  else), the timers (57), the signals (48), and the production retry loop are
  Temporal's. revl generates code that declares the shape; Temporal runs it.
- revl guarantees the SHAPE and the compile-time discipline: the workflow/activity
  split, the derived LIFO compensation order, the two-phase continue-and-record
  drain, and (Slice 2) the evidence-derived retry policy. revl does NOT guarantee
  that a running Temporal cluster honored any of it, does not verify at runtime
  that Temporal retried only what was marked retryable, and cannot observe
  Temporal's own replay determinism (only that the source admits no illegal IO).
- Out of scope for v1, refused for this target with a why-trace, never silently
  narrowed: realms, hot-swap, reactive withdrawal, `spawn` (instance-parametric
  components), leases, and any coeffect that updates during activation. Workflow
  versioning and drift-checked upgrades are a later, separate design note.

A composition that uses a refused construct is rejected for `--target temporal`
with the specific construct and location named, exactly as the roadmap requires.
It is never compiled to a workflow that quietly drops the unmapped behavior.

## 6. Adversarial self-review

Every prior design review here found a CRITICAL. This one is attack 2.

### Attack 1: spawn / instance-parametric components have no clean mapping
`spawn(ctx, Worker, cfg, realms)` returns a disposable handle and isolates each
provision into a fresh local realm under a supervision tree
(`backends/typescript/emit.py` spawn lowering). Temporal has child workflows, but
revl's spawn semantics (local-realm isolation, disposable handle, supervision)
do not correspond to child-workflow lifecycle. **Mitigation:** refuse a
composition using `spawn` for this target with a why-trace naming the spawn site.
Status: refused honestly in v1, per the scope boundary. Not a defect; a declined
mapping.

### Attack 2 (CRITICAL): the saga's best-effort semantics diverge from Temporal's retry-happy compensation idiom
revl's compensation contract is specific: Phase 2 runs compensations at their
stack position, continues past a failed compensation recording
`compensation-residue`, and honors a between-compensation deadline
(`runtime.ts` `_revlBudgetMs`, `ResidueKind`). Temporal's idiomatic saga does the
opposite by default: a compensation is just another activity, subject to its own
`RetryPolicy`, and the common saga helper retries compensations aggressively and
aborts the saga if one keeps failing. Two concrete failures result if the target
naively emits "call Temporal's saga helper" or reuses the forward activities'
retry policy for compensations:

1. A **non-idempotent compensation** (a refund) inheriting retries double-applies
   under Temporal's retry, refunding twice. revl's own recovery never does this.
2. An **unbounded-retry compensation** that is stuck blocks the whole
   compensation phase past the deadline revl promises, so later compensations in
   the LIFO stack never run, violating continue-and-record.

Either way the emitted saga does not match what `revl recover` would do for the
same composition, which is precisely the guarantee the target claims to derive.
This is the CRITICAL: the target could ship a saga that looks derived but obeys
Temporal's semantics, not revl's.

**Mitigation (must be in Slice 1, not deferred):** do not delegate to a generic
saga helper. Emit revl's two-phase drain as explicit workflow code (as in the
section 1 sketch): a compensation loop that pops LIFO, catches a compensation
failure, records residue workflow-side, and CONTINUES. Give each compensation its
OWN retry policy derived from ITS inverse's idempotency register, defaulting to
`maximumAttempts: 1` so a non-idempotent compensation cannot double-apply, and
encode revl's between-compensation deadline as the compensation activity's
schedule-to-close timeout. The exit test's injected mid-saga fault must assert
continue-and-record and no double-compensation, not merely "compensations ran."
Because Slice 1 defers evidence-derived retries entirely and defaults everything
to at-most-once (attack 3), the double-apply half is closed by construction in
Slice 1; the continue-and-record half must be emitted explicitly in Slice 1.

### Attack 3: an activity marked retryable that is non-idempotent at the host
The `declared` register is an unverified author claim over an opaque host body
(`_idempotent_register`). Temporal retries on transient failure, so a false
`declared` becomes a production double-apply. **Mitigation:** Slice 2 gives
retries only to `keyed`/`shape-proven`; `declared` and absent both get
`maximumAttempts: 1`. Slice 1 gives EVERYTHING `maximumAttempts: 1`. Status:
mitigated; the stricter-than-roadmap reading is deliberate and stated in section
3.

### Attack 4: determinism leaking via a non-deterministic pure builtin
revl proves no IO in workflow position, but a pure-looking builtin that is
non-deterministic (wall-clock now, random, uuid, non-canonical map iteration)
would lower to a plain JS call (`Date.now()`, `Math.random()`) inside the
workflow function and break Temporal determinism while revl still calls it
"pure." One reassuring data point: map key iteration is pinned to ascending
canonical `Str` order (`typecheck.py`, "ascending canonical Str order ... a pure
function of the key set"), so map order does not leak. **Mitigation / OPEN:**
Slice 1 must enumerate the pure builtin set and confirm none are
wall-clock/random/uuid-shaped; any that are must be reclassified as emissions
(activities) or refused for this target, and time/randomness must be reached only
through Temporal's workflow-safe APIs. Status: OPEN until the builtin audit is
done; low risk given the canonical-order evidence, but it must be verified, not
assumed.

### Attack 5: reactive coeffect / lease / live `req` re-resolution
`req` reads the fiber's committed view; a reactive coeffect can update during
activation, and the cordis emitter re-resolves a live per-realm worker on every
call (failover). A Temporal workflow is deterministic replay; a dependency that
changes mid-activation has no equivalent short of a signal (item 48), which is
out of v1 scope. **Mitigation:** refuse a composition whose activation depends on
a reactive/live coeffect or a lease for this target, with a why-trace. Status:
refused honestly in v1, per the scope boundary.

## 7. Sliced implementation plan

**Slice 1 (smallest landable core).** Activation to workflow, emission to
activity, and `effect ... undo ...` / `emit ... compensate ...` to a saga
compensation stack drained in G7 LIFO order, emitting to the Temporal TS SDK, for
the subset of revl constructs with a clean mapping. Concretely:

- thread `target="temporal"` into `backends/typescript/emit.py::emit`, reusing
  the IR walk and the three `Frame` entry kinds; render an activation as an
  exported workflow function plus an `activities.ts` of crossing implementations;
- emit the explicit two-phase compensation loop (attack 2 mitigation): LIFO pop,
  continue-and-record on a failed compensation, deadline as schedule-to-close;
- default EVERY activity and compensation to `maximumAttempts: 1` (attack 3): no
  evidence-derived retries yet, so nothing can double-apply;
- refuse `spawn`, realm, reactive/lease coeffects, and hot-swap for this target
  with a why-trace naming the construct and location (attacks 1 and 5); refuse
  or reclassify any non-deterministic pure builtin found by the attack-4 audit;
- add `revl emit --target temporal` to the CLI as a rendering of the TS emitter,
  orthogonal to `--backend`.
- Exit test: the roadmap's saga demo on a real Temporal dev server with an
  injected mid-saga fault, asserting derived LIFO order AND continue-and-record
  with no double-compensation.

**Slice 2 (deferred): retry policies from evidence.** Derive each activity's and
each compensation's `RetryPolicy` from the item-44 idempotency register
(`keyed`/`shape-proven` earn retries, `declared`/absent stay at-most-once); keep
the item-257 completion retry distinct from item-44 idempotent-delivery retry.

**Slice 3 (deferred): the Python SDK target.** A `--target temporal` rendering of
the Python emitter, once the TS target and its semantics are proven.

**Later, separate note:** workflow versioning and drift-checked upgrades.
