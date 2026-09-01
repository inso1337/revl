# Design: a Temporal emission target (item 253)

Status: design proposed. No implementation. This note fixes the mapping,
states exactly what revl proves that Temporal cannot, decides the target-variant
mechanism, draws the honest boundary, records two rounds of adversarial review,
and slices the work. The exit test named by the roadmap (one saga demo on a real
Temporal dev server with an injected mid-saga fault compensating in derived
order) is the acceptance gate for Slice 1, refined by the revision below.

## Revision (adversarial review 2026-09-01)

An independent adversarial review of the first draft found two new CRITICALs, two
HIGHs, and two MEDIUMs. All six are folded in here, and Slice 1 is re-sliced to
bake in every fix (still the TS SDK, still a mappable-allowlist subset). The
corrected model, in one place:

1. **Compensation-before-await, write-ahead saga registration (CRITICAL 1).** The
   first draft pushed each compensation AFTER its forward activity's `await`
   resolved. Temporal activities are at-least-once with an unreliable ack: an
   activity can commit its host side effect and still report failure (start-to-close
   timeout, worker crash after the effect but before the completion ack, lost
   result). With the Slice-1 `maximumAttempts: 1` default Temporal does not retry;
   it throws `ActivityFailure` at the `await`, control jumps to `catch`, and the
   compensation was never pushed. The result is a charged card with no refund on
   the stack, the exact orphaned non-idempotent effect the target claims to derive
   away. `revl recover` does not have this hole because it is write-ahead:
   `recovery.py::_roll_back` (~490-497) seeds the world with every WAL-recorded
   boundary referent that outlives the process, then drains inverses even across a
   mid-effect crash; the emitted saga held only successfully-acked effects, so it
   did strictly less than `revl recover` for Temporal's routine failure mode.
   `maximumAttempts: 1` is what closes double-apply (CRITICAL from review 1) and
   also what opens this orphan (it removes the retry that would re-drive the
   forward activity to a known outcome). Fix: push a provisional, witnessed
   compensation onto `saga[]` BEFORE awaiting the forward activity, keyed so it is
   a safe no-op if the forward effect never landed (mirroring the referent-seeded,
   no-op-if-absent drain in `recovery.py`); this matches `transactional`'s eager
   durable descriptor written ahead of the effect (`runtime.ts:949-960`). See §1
   and attack 2.

2. **Derived closed-allowlist refusal (CRITICAL 2).** The first draft refused by
   an open blocklist (spawn/realm/reactive/lease/hot-swap only). That silently
   leaks every construct not on the list, most dangerously `await approval[C]`
   (item 246, `LetApprovalStmt` at `lower.py:115`, `APPROVAL_KEY` at `lower.py:273`):
   a first-class activation-body gate that maps ONLY to a Temporal signal, which
   §5 puts out of v1 scope. "Reuses the IR walk verbatim, changes only the sink"
   means an approval node with no Temporal sink either crashes the emitter or
   renders as an ordinary activity that returns, silently dropping a human gate
   that guards irreversible crossings (`"production.payment"`). The roadmap forbids
   silent narrowing. Fix: the refusal set is DERIVED, not hand-listed. For
   `--target temporal`, refuse any activation whose IR contains a node kind outside
   an explicit Slice-1 ALLOWLIST, with a why-trace naming the construct and line. A
   closed allowlist is the only safe posture under the "refused with a why-trace,
   never silently narrowed" contract. See §5.

3. **Workflow-side budget check, per-call `startToCloseTimeout` (HIGH 1).** The
   first draft mapped revl's between-compensation deadline to the compensation
   activity's schedule-to-close timeout. That is wrong three ways against
   `runtime.ts::runPhase2` (1215-1244): revl's `budgetMs` (default 5000,
   `runtime.ts:880`) is a SINGLE Phase-2 total budget computed once at entry and
   checked BETWEEN compensations, where a per-activity schedule-to-close gives each
   of N its own budget (N times too much); revl's deadline does not interrupt an
   in-flight compensation (`perCallMs` is read but cannot cut off an in-flight
   call, :1230-1232) whereas schedule-to-close aborts it; and the residue shape
   differs (revl records each remaining compensation as
   `compensation-residue`/`deadline-expired`/`not-attempted` and stops, :1223-1228).
   Fix: map `perCallMs` to the compensation activity's `startToCloseTimeout`
   (per-call, Temporal-honored), and map `budgetMs` to an explicit workflow-side
   budget check in the emitted drain loop, a byte-for-byte port of `runPhase2`.
   This is replay-safe (the TS SDK freezes `Date.now()` to workflow-task time in
   workflow position). See §6 attack 2 and Slice 1.

4. **Host-affinity for host-local inverses (HIGH 2).** A `witnessed[fs]` inverse
   (item 243, "delete the file I created") is host-local and must run on the SAME
   worker that did the forward mutation. `proxyActivities` dispatches to any worker
   on the task queue, so the compensation can land on a worker that never had the
   file (silent no-op or error, misreported residue). Decision for Slice 1: REFUSE
   `witnessed[fs]` and any host-pinned coeffect for `--target temporal` with a
   why-trace (consistent with the closed allowlist; the Slice-1 exit saga is
   remote-resource, so nothing in scope needs it). The host-specific-task-queue
   affinity pattern (pin the forward activity AND its compensation to a
   host-scoped queue) is the eventual lift, recorded for a later slice. See §5 and
   attack 6.

5. **Determinism as a standing guard (MEDIUM/HIGH).** The review confirmed no
   current pure builtin (`typecheck.py::_BUILTIN_SIG`, ~854-873) is
   non-deterministic (no wall-clock/random/uuid; map iteration is canonical order),
   so Slice 1's shipped corpus is safe. But the table grows. Fix: ship a guard test
   asserting `_BUILTIN_SIG.keys()` is a subset of a reviewed deterministic
   allowlist, fail-closed on any new builtin until it is classified. The attack-4
   OPEN is downgraded to "closed by guard." See §6 attack 4.

6. **Residue sink for a failed workflow (MEDIUM).** The `catch` records residue
   workflow-side then `throw`s, but a thrown Temporal workflow surfaces only
   failure details, so revl's `AbortReport` envelope (`Frame.report()`,
   `runtime.ts:1264`) has nowhere to go. Fix: specify the sink so the emitted saga
   preserves the `outstanding`/`worldRemaining`/`proof` envelope `revl recover`
   guarantees, via a final `recordResidue` activity plus the same envelope on the
   workflow-failure `details` and a Temporal query handler for live inspection. See
   §6 attack 7.

**Validated by the review, kept and stated explicitly:** `maximumAttempts: 1` on
forward activities is FAITHFUL to revl (the TS runtime does not auto-retry forward
emissions; a fault aborts and drains), so users are not surprised by a
non-retrying forward activity. Sequential-await emission ordering is faithful
(it over-sequences at worst); a forward constraint is added so that if revl ever
gains concurrent emissions the mapping must carry the emission dependency, not a
blanket sequential await.

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
| any IR node kind outside the Slice-1 allowlist | (none) refused with a why-trace naming the construct and line | derived closed allowlist (§5) |

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
// relaxes this per activity from the item-44 idempotency register. This is
// FAITHFUL to revl: the TS runtime does not auto-retry forward emissions either
// (a fault aborts and drains); users are not surprised by a non-retrying forward.
const { flightsReserve, flightsCancel,
        hotelsReserve,  hotelsCancel,
        paymentsCharge, paymentsRefund } = proxyActivities<typeof activities>({
  startToCloseTimeout: '1 minute',
  retry: { maximumAttempts: 1 },
})

// Phase-2 total budget (revl `budgetMs`, runtime.ts:880). Checked workflow-side
// BETWEEN compensations, never mapped to a per-activity timeout (HIGH 1).
const COMPENSATION_BUDGET_MS = 5000

export async function BookTrip(req: BookTripInput): Promise<void> {
  // The compensation stack IS the derived saga. WRITE-AHEAD: each provisional
  // compensation is pushed BEFORE its forward activity is awaited, so a forward
  // that commits its host effect and then reports failure (at-least-once with an
  // unreliable ack) is still covered. Each compensation carries a witness key so
  // it is a safe no-op if the forward effect never landed (mirrors recovery.py's
  // referent-seeded, no-op-if-absent drain). Popped LIFO on abort: the order
  // recovery.py drains, not hand-written.
  const saga: Array<{ name: string; run: () => Promise<void> }> = []
  try {
    // provisional inverse registered ahead of the effect; witness resolved by the
    // forward's idempotency key so cancel() is a no-op if nothing was reserved.
    saga.push({ name: 'flights.cancel', run: () => flightsCancel(req.itineraryKey) })
    const flight = await flightsReserve(req.itinerary)

    saga.push({ name: 'hotels.cancel', run: () => hotelsCancel(req.datesKey) })
    const hotel = await hotelsReserve(req.dates)

    saga.push({ name: 'payments.refund', run: () => paymentsRefund(req.card, req.chargeKey) })
    await paymentsCharge(req.card, req.total, req.chargeKey)
    // clean completion: the saga stack is discharged, never run (Frame.drain)
  } catch (err) {
    // ABORT. Phase 2 (best-effort compensation): pop LIFO, continue-and-record,
    // honor the workflow-side budget. Byte-for-byte port of runtime.ts::runPhase2.
    const residue: Array<Record<string, unknown>> = []
    const deadline = Date.now() + COMPENSATION_BUDGET_MS  // frozen to task-time; replay-safe
    for (const step of saga.reverse()) {
      if (Date.now() >= deadline) {
        residue.push({ kind: 'compensation-residue', name: step.name,
                       reason: 'deadline-expired', outcome: 'not-attempted' })
        continue  // record and skip; DO NOT abort the loop
      }
      // per-call cutoff lives on the compensation activity's startToCloseTimeout,
      // NOT on this budget (HIGH 1); each compensation gets maximumAttempts: 1 so
      // a non-idempotent inverse (a refund) cannot double-apply (review-1 CRITICAL).
      try { await step.run() }
      catch (e) { residue.push({ kind: 'compensation-residue', name: step.name, error: String(e) }) }
    }
    await recordResidue(sagaReport(residue))   // residue sink (MEDIUM): see §6 attack 7
    throw new ApplicationFailure('saga aborted', 'SagaAbort', false, [sagaReport(residue)])
  }
}
```

Three things in that output are derived, not authored, and are the whole point:
the compensation is registered write-ahead (before the forward await) so an
acked-but-lost forward effect is still compensated; the stack is drained LIFO
(the G7 order) with a workflow-side budget check between compensations; and the
compensation phase continues past a failed compensation, records residue, and
routes that residue to a durable sink rather than aborting or losing it. A
Temporal user writes all three by hand and, per the roadmap, gets them wrong.

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
The refusal is a DERIVED CLOSED ALLOWLIST, not a hand-maintained blocklist
(CRITICAL 2). An open blocklist leaks every construct added to the language later,
and it already leaked `await approval` in the first draft. For `--target temporal`
the emitter walks the activation IR and refuses any node kind that is not on an
explicit Slice-1 allowlist, with a why-trace naming the construct and its source
line. This is the only posture consistent with the roadmap's "refused with a
why-trace, never silently narrowed" contract.

- **Slice-1 allowlist (mappable):** pure and effect statements in workflow
  position; an emission crossing rendered as an activity call; `effect ... undo ...`
  and `emit ... compensate ...` (the bracket/transactional/compensation Frame
  entries) rendered as write-ahead saga registration plus Phase-2 drain. That is
  it.
- **Refused now, with a why-trace:** `await approval` (item 246, `LetApprovalStmt`;
  maps only to a signal, out of v1 scope, dropping it drops a human gate on
  irreversible crossings); `session` commit/abort; any `cache external` node in
  workflow position (replay-nondeterministic); `witnessed[fs]` and any host-pinned
  coeffect (host affinity, HIGH 2); `spawn` (instance-parametric components);
  realms; reactive withdrawal and live coeffect re-resolution; leases; hot-swap;
  and anything that needs signals or timers-as-control-flow. Workflow versioning
  and drift-checked upgrades are a later, separate design note.

A composition that uses any refused construct is rejected for `--target temporal`
with the specific construct and location named, exactly as the roadmap requires.
It is never compiled to a workflow that quietly drops the unmapped behavior. A new
language construct that reaches the IR is refused by default until it is added to
the allowlist, so the boundary cannot silently widen.

## 6. Adversarial self-review

Every prior design review here found a CRITICAL. Review 1's is attack 2. Review 2
(2026-09-01) added a second CRITICAL, folded into attack 2 below, plus attacks 6
and 7; its findings and the corrected model are summarized in the Revision section
at the top.

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
This is review 1's CRITICAL: the target could ship a saga that looks derived but
obeys Temporal's semantics, not revl's.

**Review 2 CRITICAL (push-after-await orphan).** The first draft's sketch made a
deeper mistake than the retry idiom: it registered each compensation AFTER its
forward activity's `await` resolved (`const x = await act(); saga.push(inverse(x))`).
Temporal activities are at-least-once with an UNRELIABLE ACK: an activity can
COMMIT its host side effect and still report failure (start-to-close timeout,
worker crash after the effect but before the completion ack, lost result). With
the Slice-1 `maximumAttempts: 1` default Temporal does NOT retry; it throws
`ActivityFailure` at the `await`, control jumps to `catch`, the saga drains, but
the compensation was NEVER pushed (the push line never ran). The result is a
charged card or reserved resource with no refund on the stack, a silent orphaned
non-idempotent effect, the exact bug the target claims to derive away. `revl
recover` does not have this hole because it is WRITE-AHEAD: `recovery.py::_roll_back`
(~490-497) seeds the world with every WAL-recorded boundary referent that outlives
the process, then drains inverses even across a mid-effect crash; the draft's
`saga[]` held only successfully-acked effects, so it did strictly LESS than `revl
recover` for Temporal's routine failure mode. The `maximumAttempts: 1` mitigation
that closes double-apply is exactly what OPENS this orphan (it removes the retry
that would re-drive the forward activity to a known outcome).

**Mitigation (all in Slice 1, not deferred):**

- **Write-ahead registration.** Do NOT push after the await. Push a PROVISIONAL
  compensation onto `saga[]` BEFORE awaiting the forward activity, keyed and
  witnessed so it is a safe no-op if the forward effect never landed (mirroring
  `recovery.py`'s referent-seeded, no-op-if-absent drain), OR emit the forward
  activity and its inverse-registration as a single durable step whose descriptor
  is written ahead of the effect (matching `transactional`'s eager durable WAL
  descriptor, `runtime.ts:949-960`). The section 1 sketch shows the write-ahead
  form.
- **Explicit two-phase drain, no generic helper.** Emit revl's two-phase drain as
  explicit workflow code: a loop that pops LIFO, catches a compensation failure,
  records residue workflow-side, and CONTINUES. Give each compensation its OWN
  retry policy derived from ITS inverse's idempotency register (Slice 2),
  defaulting to `maximumAttempts: 1` so a non-idempotent compensation cannot
  double-apply.
- **Correct deadline mapping (HIGH 1).** Map revl's `perCallMs` to the compensation
  activity's `startToCloseTimeout` (per-call, and Temporal honors it), and map
  `budgetMs` to an explicit WORKFLOW-SIDE budget check in the emitted drain loop
  (`const deadline = Date.now() + BUDGET_MS` at Phase-2 entry, then
  `if (Date.now() >= deadline) { recordResidue('deadline-expired','not-attempted'); continue }`
  before each compensation), a byte-for-byte port of `runPhase2` (`runtime.ts:1215-1244`).
  Do NOT map the deadline to schedule-to-close: `budgetMs` is a single total
  computed once at Phase-2 entry, a per-activity schedule-to-close gives each of N
  its own budget (N times too much); revl's deadline does not interrupt an
  in-flight compensation (`perCallMs` read but cannot cut off an in-flight call,
  :1230-1232) whereas schedule-to-close aborts it; and the residue shape differs
  (:1223-1228). The workflow-side check is replay-safe because the TS SDK freezes
  `Date.now()` to workflow-task time in workflow position.

The exit test's injected fault MUST include a forward-activity start-to-close
timeout WHOSE EFFECT COMMITTED at the host, and assert the resource is compensated
(not only a mid-saga fault after clean acks), plus continue-and-record and no
double-compensation. Because Slice 1 defers evidence-derived retries and defaults
everything to at-most-once (attack 3), the double-apply half is closed by
construction; the write-ahead registration, the continue-and-record drain, and
the workflow-side budget check must all be emitted explicitly in Slice 1.

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
function of the key set"), so map order does not leak. Review 2 audited the table
(`typecheck.py::_BUILTIN_SIG`, ~854-873) and confirmed no current pure builtin is
non-deterministic: the shipped set is string and list operations, none
wall-clock/random/uuid-shaped, so Slice 1's corpus is safe TODAY. **Mitigation
(standing guard, not a one-time read):** the table grows, so a one-time audit is
not enough. Slice 1 ships a guard test asserting `_BUILTIN_SIG.keys()` is a subset
of a reviewed deterministic allowlist, FAIL-CLOSED on any new builtin until it is
classified; a non-deterministic builtin that must be added is reclassified as an
emission (activity) or refused for this target, and time or randomness is reached
only through Temporal's workflow-safe APIs. Status: closed by guard (the OPEN is
retired: the risk is now a test that fails the day an unclassified builtin lands,
not a note someone must remember to re-read).

### Attack 5: reactive coeffect / lease / live `req` re-resolution
`req` reads the fiber's committed view; a reactive coeffect can update during
activation, and the cordis emitter re-resolves a live per-realm worker on every
call (failover). A Temporal workflow is deterministic replay; a dependency that
changes mid-activation has no equivalent short of a signal (item 48), which is
out of v1 scope. **Mitigation:** refuse a composition whose activation depends on
a reactive/live coeffect or a lease for this target, with a why-trace. Status:
refused honestly in v1, per the scope boundary.

### Attack 6 (review 2, HIGH): a host-local inverse dispatched to any worker
A `witnessed[fs]` inverse (item 243, "delete the file I created") is HOST-LOCAL:
it must run on the SAME worker that performed the forward mutation. `proxyActivities`
dispatches to ANY worker on the task queue, so the compensation can land on a
worker that never had the file, producing a silent no-op or error and a misreported
residue. **Mitigation (decided for Slice 1):** REFUSE `witnessed[fs]` and any
host-pinned coeffect for `--target temporal` with a why-trace naming the crossing
and line (consistent with the closed allowlist; the Slice-1 exit saga uses only
remote resources, so nothing in scope needs it). The eventual lift, recorded for a
later slice, is the Temporal host-affinity pattern: pin the forward activity AND
its compensation to a host-specific task queue so the inverse runs where the effect
landed. Status: refused honestly in Slice 1; affinity pinning deferred.

### Attack 7 (review 2, MEDIUM): a failed workflow has nowhere to put its residue
The `catch` records residue workflow-side and then `throw`s. But a thrown Temporal
workflow surfaces only failure details, so revl's `AbortReport` envelope
(`Frame.report()`, `runtime.ts:1264`, carrying `outstanding`/`worldRemaining`/`proof`)
has nowhere to go, and the residue proof `revl recover` guarantees is lost.
**Mitigation:** specify the sink. Before the terminal `throw`, the emitted drain
runs a final `recordResidue` activity that durably writes the merged residue
envelope; the same envelope is attached to the workflow-failure `details` (via a
typed `ApplicationFailure`) so a failed run still carries it; and a Temporal query
handler exposes the current residue for live inspection of an in-flight or
just-failed workflow. The emitted saga therefore preserves the same
`outstanding`/`worldRemaining`/`proof` envelope revl recover produces, rather than
collapsing it into an opaque failure. Status: sink specified; implemented in
Slice 1.

## 7. Sliced implementation plan

**Slice 1 (smallest landable core, all six review-2 fixes baked in).** Activation
to workflow, emission to activity, and `effect ... undo ...` / `emit ... compensate ...`
to a WRITE-AHEAD saga compensation stack drained in G7 LIFO order, emitting to the
Temporal TS SDK, for the derived-allowlist subset of revl constructs. Concretely:

- thread `target="temporal"` into `backends/typescript/emit.py::emit`, reusing
  the IR walk and the three `Frame` entry kinds; render an activation as an
  exported workflow function plus an `activities.ts` of crossing implementations;
- **write-ahead saga registration (CRITICAL 1):** push each provisional, witnessed
  compensation onto `saga[]` BEFORE awaiting its forward activity, keyed so it is a
  no-op if the forward effect never landed, so an acked-but-lost forward effect is
  still compensated;
- emit the explicit two-phase compensation loop (no generic saga helper): LIFO
  pop, continue-and-record on a failed compensation, and a WORKFLOW-SIDE budget
  check (`budgetMs`) between compensations with `perCallMs` as each compensation
  activity's `startToCloseTimeout` (HIGH 1), a byte-for-byte port of `runPhase2`;
- default EVERY activity and compensation to `maximumAttempts: 1` (attack 3): no
  evidence-derived retries yet, so nothing can double-apply;
- **derived closed-allowlist refusal (CRITICAL 2):** refuse any IR node kind
  outside the Slice-1 allowlist with a why-trace naming the construct and line;
  the refused set explicitly includes `await approval`, `session` commit/abort,
  `cache external` in workflow position, `witnessed[fs]` and host-pinned coeffects
  (attack 6, HIGH 2), `spawn`, realms, reactive/lease coeffects, hot-swap, and
  anything needing signals or timers-as-control-flow (attacks 1, 5, 6);
- **determinism guard (attack 4):** ship a fail-closed test asserting
  `_BUILTIN_SIG.keys()` is a subset of a reviewed deterministic allowlist;
- **residue sink (attack 7):** on abort, run a final `recordResidue` activity, put
  the `AbortReport` envelope on the workflow-failure `details`, and expose it via a
  query handler, so `outstanding`/`worldRemaining`/`proof` survive a failed run;
- add `revl emit --target temporal` to the CLI as a rendering of the TS emitter,
  orthogonal to `--backend`;
- forward constraint: emissions are lowered to sequential awaits (faithful,
  over-sequences at worst); if revl gains concurrent emissions, the mapping must
  carry the emission dependency rather than keep a blanket sequential await.
- Exit test: the roadmap's saga demo on a real Temporal dev server with an
  injected fault that INCLUDES a forward-activity start-to-close timeout WHOSE
  EFFECT COMMITTED at the host, asserting that resource is compensated (write-ahead
  coverage), plus derived LIFO order, continue-and-record, workflow-side budget
  behavior, and no double-compensation.

**Slice 2 (deferred): retry policies from evidence.** Derive each activity's and
each compensation's `RetryPolicy` from the item-44 idempotency register
(`keyed`/`shape-proven` earn retries, `declared`/absent stay at-most-once); keep
the item-257 completion retry distinct from item-44 idempotent-delivery retry.

**Slice 3 (deferred): the Python SDK target.** A `--target temporal` rendering of
the Python emitter, once the TS target and its semantics are proven.

**Later, separate note:** workflow versioning and drift-checked upgrades.
