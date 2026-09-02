# Design: the unified teardown and audit contract

Status: design locked (decisions approved 2026-08-26). This is the single
contract the shared Slice-2 runtime seam implements on all six tiers.

## Why this doc exists

Three work streams restructure the same six-tier teardown accumulator:

- item 243 (docs/design/243-witnessed-externs.md) adds the `transactional`
  entry kind, abort-only replay with commit-time discharge;
- item 247 (docs/design/247-compensate.md) adds the `compensation` entry kind
  and the two-phase abort;
- the existing acquire bracket already owns clean-unload replay.

Each of those docs specifies its own delta to the loop. Nobody owned the
combined loop, and a quiet disagreement between the deltas becomes six
divergent runtimes the moment Slice 2b fans out. This doc IS the combined
loop: the entry kinds, the abort algorithm, the failure and bound rules, the
commit path, the merged residue schema (which item 246 freezes against), and
the WAL descriptor. 243 Slice 2a proved the seam on the py reference tier
(backends/python/runtime.py, `Frame` / `_Transactional` / `drain`); a
Slice-2b agent builds the remaining five tiers' loops from this document
without reopening a decision.

Sequencing note: 243's slice plan gated the rust part of Slice 2 behind item
278. Item 278 is landed, so rust Slice 2b proceeds alongside the other four
tiers; nothing waits on it any more.

What this doc does NOT own, with pointers:

- the `witnessed` surface, classification, and checker rules: 243;
- the `compensate` type surface and checker obligations: 247;
- confinement: item 244 is a separate slice; the teardown loop neither
  depends on it nor changes under it, cross-referenced only;
- taint/provenance interaction with classification: 249;
- the 246 approval-policy spec: 246 consumes the residue schema defined here
  (see "Downstream consumer" below), the policy itself is 246's item;
- the attended `:back` firing policy: py replay/REPL layer, see "The `:back`
  split" below.

## The three entry kinds, one stack

One LIFO disposer stack per activation. Every entry is one of three kinds;
they interleave freely in registration order, and both teardown phases walk
the same stack. There is no second list (243 Slice 2a decision 2: the
distinction lives in the entry, not in a separate structure).

| | `bracket` (acquire) | `transactional` (witnessed, 243) | `compensation` (247) |
|---|---|---|---|
| surface | proof (G4/G7) | proof (reversible crossing) | audit (G8 intent) |
| replays on clean unload | **yes** (release the handle) | **no** (discharge + witness GC) | **no** (discharge, never run) |
| replays on abort | yes, Phase 1 | yes, Phase 1 | yes, Phase 2, best-effort |
| **on an E-Stop (443)** | **stranded** | **stranded** | **stranded** |
| may emit in teardown | no (G5) | no (host-local inverse) | yes (that is the point) |
| may fail | no (G5-infallible by contract) | yes, anticipated (243 rule 6) | yes, best-effort |
| failure severity on abort | `bracket-fault` (contract-grade) | `restore-residue` (anticipated) | `compensation-residue` (anticipated) |
| registration | at acquisition | on the `Ok` branch only (243) | at the `emit` |
| args/witness | captured at acquisition | witness captured at `Ok`, WAL-serializable | args captured at registration, WAL-serializable |

## The teardown algorithm

Abort-vs-commit discrimination is the tier's own concern; the py reference
uses "did `drain` run" (`Frame._committed`, flipped synchronously at drain
entry, 243 Slice 2a decision 1). The contract below is what every tier must
observably do given that bit.

```
teardown(stack, committed, budget):

  if committed:                    # clean unload; implicit commit until 245
      for e in reverse(stack):     # LIFO, one pass
          bracket:        run e.inverse()          # release; still runs
          transactional:  discharge(e)             # drop inverse, GC witness,
                                                   # WAL discharge record
          compensation:   discharge(e)             # never runs; the forward
                                                   # emission was the deliverable
      return CLEAN

  # ---- abort ----
  residue = []

  # Phase 1: proof replay. LIFO over the whole stack, compensations skipped.
  # Runs to completion no matter what fails.
  for e in reverse(stack):
      bracket:
          try e.inverse()
          catch err: residue.add(record(kind=bracket-fault, e, err))      # CONTINUE
      transactional:
          try e.undo(e.witness)
          catch err: residue.add(record(kind=restore-residue, e, err))    # CONTINUE
      compensation:
          skip                       # deferred to Phase 2

  # Phase 2: intent. LIFO over the same stack, compensations only.
  # Best-effort, bounded, guarded; the abort never fails and never blocks
  # on this phase beyond the budget (see the bound rule for tier honesty).
  deadline = now() + budget
  for e in reverse(stack) where e.kind == compensation:
      if now() >= deadline:
          residue.add(record(kind=compensation-residue, e,
                             error=deadline-expired,      # the reason, in `error`
                             attempted=false))            # attempted.call is null
          continue                   # record every skipped one; run none of them
      try run_bounded(e.emit, e.args, min(per_call_bound, deadline - now()))
      catch err | timeout:
          residue.add(record(kind=compensation-residue, e, err))          # CONTINUE

  surface(residue)                   # merged schema below; the abort SUCCEEDS
  return ABORTED(residue)
```

### The third column: E-Stop (item 443)

Amendment ([443-estop.md](443-estop.md)): an OPERATOR HALT is a third verdict,
`halted`, and the table's third row above is its whole content. It replays
nothing, discharges nothing, and STRANDS every registered entry — a third
disposition meaning registered, not run, and NOT dropped.

Stranded is not a synonym for discharged, and the difference is the point.
Discharge releases the inverse and the witness so no rollback state survives;
stranding KEEPS both, because `revl recover` is what reads them back. Since the
halt also writes no `discharge` record, the discharge descriptors with nothing
behind them are exactly the entries still owed — which is exactly what a crash
leaves. An E-Stop is deliberately shaped to look like a crash to the recovery
path.

The teardown algorithm below is therefore NOT run at all under `halted`. There
is no Phase 1, no Phase 2, no budget: the halt's cost is a latch flip, which is
the entire reason the verb exists. What it produces instead is the in-flight
inventory — two residue kinds, in the merged schema below.

Mechanism is free, observable order is not. A tier may implement the phase
split as two literal stack walks, or (natural on cordis tiers, where the
runtime unwinds disposers in stack position) by having the compensation
disposer enqueue itself when invoked during an abort unwind and draining the
queue in a post-unwind hook. Either way the observable contract is: all
Phase-1 inverses complete before any compensation starts, and each phase is
LIFO within itself.

The phase rule is scoped PER ACTIVATION. The stack is per-activation ("one
LIFO disposer stack per activation", above), so "all Phase-1 inverses
complete before any compensation starts" binds within one activation's
teardown, not across the process. In a cascading abort, a dependent
activation's Phase 2 may run before a provider activation's Phase 1 has
started: each activation tears down its own stack in its own two phases, in
whatever activation order the runtime unwinds the cascade. There is no
global phase barrier across activations and none is intended; a tier that
adds one is diverging, not being safe.

### Why two phases (the three load-bearing reasons)

1. **A best-effort failure must never interrupt proof recovery.** Interleaved
   in one pass (today's placeholder), a compensation that raises can leave
   later proof inverses un-run. Draining every `bracket` and `transactional`
   entry first guarantees the provable rollback always completes in full,
   regardless of what the best-effort tail does.
2. **Compensations offset only what proof replay could not reverse.** A
   witnessed effect is fully reversed in Phase 1, so it never needs a
   compensation (247 decision 4). Running compensations last means they act
   against the world after every reversible thing is already back: the
   smallest, most honest set of irreversible crossings left to offset.
3. **No data hazard.** Compensation arguments are captured at registration
   (and serialized for the WAL), never re-read at teardown. The phase split
   changes only when the offsetting emissions fire relative to the inverses,
   not what they compute, so reordering is sound.

### Phase-1 failure: continue-and-record, uniform, two severities

A failed inverse never skips the remaining Phase-1 inverses. Skipping
strictly increases residue: every un-run inverse downstream of a failure is
state that was recoverable and is now not. So the rule is uniform, one
mechanism: catch, record into the merged residue schema, continue. Phase 2
still runs afterward (its whole purpose is offsetting what Phase 1 left).

The two severities, same mechanism:

- **A failed `bracket` inverse is a CONTRACT-GRADE FAULT** (`bracket-fault`).
  A bracket inverse claimed G5 infallibility (host-local, non-emitting,
  cannot fail); a raise here means the program's own proof surface lied. The
  runtime still continues (stopping would only add residue), but the record
  is tagged contract-grade and 246 must treat it as such: nothing downstream
  may keep trusting the activation's "revertible" label.
- **A failed `witnessed` restore is the ANTICIPATED case**
  (`restore-residue`). 243 rule 6 says the inverse is fallible by design
  (TOCTOU, disk-full on restore) and its failure feeds 246's prompt. Same
  catch, same record shape, lower severity.

### Phase-2 bound: honest about synchronous tiers

Best-effort also means bounded: "never fail the abort" includes "never block
it". A hung `mail.recall` must not hold the teardown open indefinitely. But
the compensation seam is synchronous (247 decision 3, no `await`), and
several tiers cannot preempt a synchronous host call. The bound is therefore
specced at the strength every tier can actually deliver, no stronger:

- **Normative on all six tiers:** a Phase-2 deadline (the budget) checked
  BETWEEN compensations. Before starting each compensation the runtime checks
  the remaining budget; if expired, it starts no further compensation and
  records each un-started one as `compensation-residue` with
  `error: deadline-expired`, `attempted: false`. Every skip is recorded;
  nothing is silently dropped.
- **In-call preemption only where the tier supports it** (table below). Where
  supported, a compensation exceeding its per-call bound is cut off in flight
  and recorded as `compensation-residue` with the timeout as its error; where
  the cut-off is an abandonment (the call keeps running detached), the record
  additionally carries `outcome: unknown`, because the emission may still
  land after the abort completed.
- **The honest limitation, stated once:** on a tier with no in-call
  preemption, one hung synchronous compensation holds the abort until the
  host call returns. The deadline then cuts off everything after it. Hosts on
  those tiers should give compensation-bearing externs their own internal
  timeouts; the contract does not pretend the runtime can do it for them.

Per-tier in-call preemption, normative for Slice 2b:

| tier | in-call preemption of a sync compensation | notes |
|---|---|---|
| python | none | a sync host call cannot be safely interrupted (signals are main-thread-only and unsafe mid C call); between-compensation check only |
| typescript | none | single-threaded event loop; nothing runs to observe a deadline during a sync call |
| go | abandon-the-wait | run the call in a goroutine, select on a timer; on timeout stop waiting and proceed. The call keeps running detached: record `outcome: unknown` |
| rust | none | compensation closures are not guaranteed `Send`, so the runtime cannot hand one to a helper thread to wait on with a timeout; between-compensation check only |
| java | interruptible blocking points only | `Thread.interrupt` preempts interruptible IO/waits; arbitrary sync code is not preemptible (`Thread.stop` is forbidden) |
| wasm | guest code yes, host imports no | the first-party runtime may bound guest execution via its epoch/fuel interruption; a host import call in flight is not preemptible |

Per-tier loop obligations, normative for Slice 2b (these complete the table;
each is a sentence the tier's loop is built against, not a new decision):

- **go.** The guarded goroutine MUST `defer recover()` and route the
  recovered panic (or the call's error) over the result channel. An
  unrecovered panic in a detached goroutine kills the whole process, which
  turns a best-effort phase into a crash; the guard is what keeps "may fail"
  meaning a residue record. And abandonment relaxes seriality: after the
  loop abandons one compensation and starts the next, the abandoned call is
  still running, so two compensations may run CONCURRENTLY. "LIFO within
  itself" pins the START order of Phase-2 compensations only, not mutual
  exclusion; a compensation author on go cannot assume the previous
  compensation has finished.
- **java.** `Thread.interrupt` preempts only interruptible blocking points;
  arbitrary sync code ignores it. The loop runs each compensation as a task
  and bounds it with `Future.get(timeout)` + `cancel(true)`. When the
  interrupt is honored, the cut-off is a true in-call preemption. When it is
  NOT honored, that pattern is an ABANDONMENT: the task thread keeps running
  detached after `cancel(true)`, exactly go's shape, and the record carries
  `outcome: unknown`. So java may abandon go-style, and whenever it does,
  go's concurrency caveat applies on java too (start order pinned, mutual
  exclusion not).
- **rust.** The "none" needs its real reason, because "no cancellation of
  arbitrary sync code" also describes go (go never cancels the call either;
  it abandons the wait). The distinguishing reason is that compensation
  closures are not guaranteed `Send`: the runtime cannot move one onto a
  helper thread to run or wait on it under a timer at all, so even the
  abandon-the-wait shape is unavailable. Between-compensation check only.
- **wasm.** Two qualifications. First, the wasm accumulator is fixed at
  ACTIVATION TIME; a method-time compensation is a hard `EmitError` on this
  tier today. Exit test 3's "mixed-entry LIFO in both phases" therefore
  reads, on wasm, over activation-registered entries only; the method-time
  half of the contract is not owed until that restriction lifts (its own
  item, not Slice 2b). Second, the table's "guest code yes" is a wasmtime
  CAPABILITY, not an existing feature: the first-party runtime has no
  epoch/fuel wiring today. Slice 2b must WIRE it first-party (an epoch
  deadline or fuel meter armed around guest execution in Phase 2) before
  claiming guest-code preemption; until that wiring lands, wasm is in
  practice a between-compensation-check-only tier.

### wasm Slice 2b as implemented

Landed in `backends/wasm/emit.py` (the compiled accumulator) and
`backends/wasm/lifecycle.py` (the first-party wasmtime driver;
`test_witnessed_teardown.py` proves it against a live module).

1. **The abort-vs-commit discriminator is "did the body run to every
   segment."** `activate_step` flips a new `$__committed` global the moment
   the LAST segment completes without trapping — a mid-activation trap never
   reaches that line, so an abort leaves it at its default 0. A new
   `committed()` export reads it back, the wasm analogue of the py reference
   tier's `Frame._committed` ("did `drain` run").
2. **Additive: the whole scaffold is gated on actually needing it.** A
   component with no `witnessed` extern and no `emit ... compensate ...`
   step registers only `bracket` entries, which replay on commit AND abort
   alike — nothing for a commit/abort split to distinguish. `_module`
   computes `needs_teardown_scaffold` (any `transactional`/`compensation`
   entry present) and, when false, renders `deactivate` in the EXACT
   pre-Slice-2b single-pass shape (no `$__committed`/`$__dstep` globals, no
   `committed()`/`deactivate_step()` exports) — byte-identical emission for
   every program that does not use the feature, mirroring the go tier's
   `_COMP_NEEDS_TEARDOWN` and the rust tier's `_body_has_witnessed`/
   `_body_has_compensate` gates. Verified against `origin/main`: Beacon,
   Auditor, Pulse and canonical_service's goldens are byte-identical.
3. **Three entry kinds, one LIFO stack, no second list** — carried exactly as
   the shared table above specifies. A witnessed acquisition's registration
   is genuinely Ok-conditional at RUNTIME (not just compile time): two
   globals per entry (`$g_wit_val_<n>`, `$g_wit_flag_<n>`) hold the extracted
   Ok payload and whether Ok was actually returned, since the wasm state
   machine has no other way to carry a value across the `activate_step`/
   `deactivate` call boundary (a wasm `local` does not survive between
   exported-function calls). `_witnessed_effect_step` (`emit.py`) is the
   codegen.
4. **The two-phase abort is a compiled dispatch chain, not a single pass.**
   `deactivate_step() -> i32` processes exactly one accumulator entry per
   call — the same per-call idiom `activate_step` already used for the
   forward path — with `$__dstep` advanced BEFORE the entry's own
   inverse/compensate runs, so a trap inside one entry leaves the cursor
   already past it. `deactivate()` keeps its old signature (a host like
   cordis-wasm's `Runtime.unplug` needs no changes) and is now a thin loop
   calling `deactivate_step` to completion in one call — same all-or-nothing
   trap behavior as before this slice for that path, since core wasm still
   has no catch/continue across an internal `call`; genuine per-entry
   continue-and-record needs the caller driving `deactivate_step` itself,
   which is exactly what `lifecycle.drive_teardown` does.
5. **Epoch/fuel is wired first-party, at the per-entry granularity the
   compiled dispatch makes possible**, closing the open capability gap the
   paragraph above named: `lifecycle.drive_teardown` arms a wasmtime epoch
   deadline around each Phase-2 (compensation) call individually — real
   in-call guest-code preemption, verified against a live spin-loop
   compensation (`test_epoch_deadline_bounds_a_hung_compensation`), not
   merely the between-compensation check. The honest limitation stands as
   specced: a compensation blocking entirely inside a host import (a `req`
   call) is not preemptible this way, because wasmtime only checks the
   deadline in guest code.
6. **The WAL descriptor is a static compile-time index, not a runtime log.**
   `_teardown_section` emits a `revl:teardown` custom section (seq, kind,
   `deactivate_step` dispatch position per transactional/compensation entry)
   — the honest ceiling on a tier whose teardown loop is compiled state with
   zero host bookkeeping (backends/wasm/README.md): runtime witness/argument
   VALUES live in the component's own linear memory, and nothing here claims
   to persist them durably. A host wanting genuine crash recovery on this
   tier builds its own WAL against this section as a starting index; this
   slice does not claim recovery itself.
7. **Not done in this slice, scoped elsewhere:** the cross-tier a5
   respec/exit-test sweep (exit tests 1-2 above) is sequenced with the py
   tier and the other Slice-2b tiers, not owned by the wasm landing alone;
   method-time compensation stays refused (its own item, not Slice 2b); the
   `revl:teardown` section carries no witness/argument VALUES, by design (6).

Budget values (Phase-2 total and per-call) are host configuration with
tier-uniform defaults; the contract fixes the check points and the residue
records, not the numbers. The config SURFACE is pinned now, so six tiers do
not invent six plumbings: two environment variables in the existing `REVL_*`
convention, read once at activation, identical spelling on every tier:

- `REVL_COMPENSATION_BUDGET_MS`: the Phase-2 total budget, milliseconds;
- `REVL_COMPENSATION_PER_CALL_MS`: the per-call bound, milliseconds.

Provisional defaults, standing until the first Slice-2b landing proposes the
final numbers and this doc pins them (open question 1): budget 5000 ms,
per-call 1000 ms. Unset means the default; `0` means no bound (the
between-compensation check still runs and records nothing expired). The
names and the read-at-activation rule are fixed here; only the two numbers
remain open.

## Commit path (clean unload)

Amendment (item 245, [245-session-commit.md](245-session-commit.md)): under a
registered session owner the commit MOVES from unload time to an explicit
SESSION commit point, and a mid-session withdrawal's `transactional`/
`compensation` entries are held in the session's discharge escrow rather than
discharged at drain. `revl recover` gains a superset skip for the session-owned
case: a durable `commit-approved` record is the commit proof even before the one
consolidated `discharge` record lands (the approved-to-discharged window), and a
new `flush-residue` residue kind reports a deferred emission whose flush the
crash interrupted. The clause below is the compatibility case - a bare `Frame`
with no owner keeps exactly this implicit-commit semantics.

Without a session owner (the compatibility clause), a clean successful unload IS
the commit (implicit). On it, in one LIFO pass:

- every `bracket` inverse RUNS, unchanged: releasing an acquired handle is
  always right, success or abort;
- every `transactional` entry is DISCHARGED: the inverse is dropped, the
  witness reference is dropped (witness GC), and a discharge record is
  WAL-logged (below). The mutation is the deliverable and persists;
- every `compensation` entry is DISCHARGED: it never runs. The forward
  emission (the insert, the sent mail) is the deliverable; best-effort
  cleanup on success is wrong.

The WAL discharge record is load-bearing: without it, `revl recover` after a
post-commit crash would find the logged inverse descriptors and replay a
committed transaction's rollback. Discharge must be durable before the
activation reports success.

## The merged residue schema (246 freezes this)

One schema, one channel. 243 rule 6 (restore-residue feeds 246's prompt) and
247 decision 2 (compensation failure is enumerable on the audit surface,
surfaced through 246) both already point at "the same channel"; this section
makes it literal. The envelope is recovery.py's returned shape, extended, and
the per-crossing audit tag is 247's three-state.

Envelope (the shape `revl recover` and the abort path both return; see
src/revl/recovery.py, `residue` in the report):

```
"residue": {
  "clean":          bool,        # true iff outstanding is empty
  "outstanding":    [Record...], # the merged records, schema below
  "worldRemaining": [...],       # boundary state still out, by referent
  "proof":          str          # the honest one-paragraph verdict, in
                                 # recovery.py's voice: what ran, what is
                                 # still out, never claiming a dead
                                 # closure ran
}
```

Per-record (`Record`), minimal and closed. A field a consumer needs that is
not here is a change to THIS doc, not a tier-local addition:

```
{
  "kind":      "restore-residue"      # witnessed inverse failed on abort (anticipated, 243 rule 6)
             | "bracket-fault"        # bracket inverse failed on abort (CONTRACT-GRADE)
             | "compensation-residue" # compensation failed / timed out / not attempted
             | "unreconstructible"    # recovery only: WAL descriptor could not be rebuilt
             | "estop-stranded"       # item 443: registered, NEVER attempted, still owed.
                                      # outcome "not-attempted", attempted null,
                                      # error.type "estop"
             | "estop-ambiguous",     # item 443: dispatched and unconfirmed when the
                                      # operator halt landed, so it MAY have landed.
                                      # outcome "unknown" — item 440's ambiguous tier,
                                      # created deliberately. At most one per activation
  "crossing":  {                      # the ORIGINAL effect this entry belonged to
      "key":    str,                  # capability / service key
      "method": str,
      "args":   [...],               # as captured, serialized
      "site":   str                   # source site, file:line
  },
  "attempted": {                      # the reversal/offset that was tried
      "call":  str | null,            # named call (inverse or compensation); null iff not attempted
      "args":  [...],
      "phase": 1 | 2
  },
  "error":     {"type": str, "message": str} | null,   # the failure OR skip reason;
                                      # carries `deadline-expired`/`unreconstructible`
                                      # even when attempted.call is null. null only
                                      # when there is nothing to report (never for a
                                      # residue record — a record exists because
                                      # something failed, timed out, was not attempted,
                                      # or could not be rebuilt)
  "attemptedFlag": bool,              # false: skipped (deadline expired / unreconstructible)
  "outcome":   "failed" | "unknown" | "not-attempted",  # "unknown" only for abandoned in-flight calls
  "referent":  str,                   # what is still out in the world (row id, path, message id)
  "hint":      str                    # recovery hint for the operator (what to check, how to finish by hand)
}
```

Alongside the records, every emission crossing on the G8 audit surface
carries the 247 three-state tag, unchanged from 247 decision 2:

- `bare`: no compensation attached;
- `compensated`: attached and completed;
- `unresolved`: attached, attempted (or owed), did not land. Every
  `compensation-residue` record has exactly one `unresolved` crossing behind
  it and vice versa.

Both the abort path (six tiers) and `revl recover` (recovery.py) emit this
same schema. Recovery adds nothing except the `unreconstructible` kind: a
WAL entry whose descriptor cannot be re-issued (closure-only, args that did
not serialize) is residue, never reported as run (recovery.py,
`_residue_proof`).

### Downstream consumer (246), stated but not specified here

Item 246's approval-class assignment reads this schema and nothing else.
The intended shape, recorded so the schema provably carries enough: approval
class is a total function of the checked effect type; witnessed crossings
auto-approve silently but a `restore-residue` on abort prompts; a compensated
emission enters commit enumeration labeled "already sent, best-effort" when
attempted vs "will send" when deferred; `endorse` prompts. Policy escalates,
never de-escalates. The full 246 spec, including prompt wording and the
per-session accounting, is 246's item and is deliberately NOT fixed here.

## WAL descriptor

Every `transactional` inverse and every `compensation` is WAL-logged at
registration as a NAMED-CALL DESCRIPTOR with captured serializable
arguments, never a closure (243 rule 4, 247 decision 5; recovery.py's rule
that a closure after a crash is residue, not recovery):

```
{
  "record":   "discharge-descriptor",  # the discriminator recovery.py reads;
                                   # the reader keys records off `record`, so the
                                   # discharge-descriptor MUST carry it (an earlier
                                   # draft omitted it and the reader could not
                                   # distinguish it from a legacy `effect` record)
  "seq":      int,                 # registration order on the shared stack;
                                   # replay order is reverse-seq within phase
  "entry":    "transactional" | "compensation",
  "call":     {                    # what `revl recover` re-issues
      "receiver": str,             # capability / service key. NAMED `receiver`, not
                                   # `key`: recovery.py's `World.key`/`apply_inverse`
                                   # (and `record_boundary`'s reconstructible `op`)
                                   # already key off `receiver`; writer and reader
                                   # agree on this one spelling — no adapter shim
      "method":   str,             # the declared inverse, or the compensation emission
      "args":     [...]            # serialized; for transactional this includes
                                   # (or is) the witness payload
  },
  "origin":   {                    # the forward crossing it reverses/offsets;
                                   # audit-facing, keyed by capability `key`
      "key": str, "method": str, "args": [...], "site": str
  },
  "witness":  {...} | null,        # transactional only: durable data (paths,
                                   # refs), never a host handle
  "idempotency": str | null        # author-supplied key where present (item
                                   # 309 owns the typed mechanism)
}
```

And the discharge record (commit path), its own `record` discriminator:

```
{ "record": "discharge", "discharged": [seq...] }   # durable before success is
                                   # reported; recover SKIPS every seq named here
```

`revl recover` re-issues from the descriptor alone: Phase 1 reconstructs and
runs boundary inverses reverse-seq (bracket + transactional, skipping
discharged seqs), then Phase 2 re-issues compensation descriptors against the
`World` adapter through its DEDICATED `apply_compensation` path (recovery.py):
that path always records a compensation as a further crossing and never clears
its referent. The correction the review flagged: the GENERIC `World.apply_inverse`
(recovery.py, `DictWorld`) name-matches a `_REMOVE` verb set — and several
compensation verbs live in it (`delete`, `revoke`, `rollback`, `compensate`) —
so routing a compensation through it would POP the forward referent and report a
best-effort offset as CLEAN. Phase 2 therefore uses `apply_compensation`, not
`apply_inverse`. Reporting is honest, in recovery.py's existing voice: a
rollback that owed a compensation it did not complete is `rolled-back` with
residue, never clean. Because recover can re-attempt what an abort already
attempted, inverses must be idempotent-on-replay (243 rule 5) and
compensations should be idempotent or carry the idempotency key.

### Owned deliverable: the recovery.py/replay.py WAL migration (py tier, landed)

The WAL discharge-descriptor, the discharge record, discharged-seq skipping, the
dedicated compensation apply path, and the merged residue records above are a
single OWNED migration on the py reference tier, not something each Slice-2b tier
re-derives. It is landed by the `agent/witnessed-wal-recover` slice in:

- `backends/python/replay.py` — the writer: `WriteAheadLog.record_discharge_descriptor`
  (a `transactional` inverse or a `compensation` as a named-call descriptor) and
  `WriteAheadLog.record_discharge` (`{"record":"discharge","discharged":[seq...]}`);
- `src/revl/recovery.py` — the reader: `_roll_back` walks the descriptors in two
  phases (Phase 1 transactional inverses reverse-seq, SKIPPING discharged seqs;
  Phase 2 owed compensations through `DictWorld.apply_compensation`), and emits
  the merged residue records (`restore-residue` / `bracket-fault` /
  `compensation-residue` / `unreconstructible`).

Three defects this migration fixed, each a divergence trap for Slice 2b if left
unowned: (1) the descriptor needed a `record` discriminator and the `receiver`
named-call spelling before recovery.py's reader could parse it; (2) the discharge
record was specified but recover had no discharged-seq skip, so a COMMITTED
transactional inverse would have been replayed on recover; (3) recover's
compensation apply used the generic `apply_inverse`, whose `_REMOVE` name-match
would clear a `revoke`/`delete`-named compensation's referent and report a
best-effort offset as CLEAN. Slice 2b builds the other five tiers' loops against
this contract and the landed py reader/writer, and does not reopen these.

## The `:back` split (separate decision, deferred)

The attended `:back` REPL path (backends/python/replay.py, `step_back`) is
NOT part of the six-tier teardown loop; it is the py replay layer. The entry
kinds are shared, the FIRING policy differs: an unattended abort auto-fires
Phase 2 (this doc); an attended `:back` enqueues compensations as OWED and
lets the operator decide, reporting them in `compensationsRan` /
crossed-vs-offset terms as replay.py already does. The full `:back` policy
(when owed compensations fire, how `force` interacts with bare emissions) is
a separate decision on the replay layer and is deferred; nothing in the
six-tier loop depends on it.

## Exit tests (the conformance gate for Slice 2b)

1. **TCK A5 respec.** `tck/spec.py` `a5_compensate_lifo` asserts the OLD
   single-interleaved-LIFO behavior (the compensation DELETE fires before the
   earlier bracket unlock, on every teardown). The two-phase contract changes
   it on two axes, ordering AND firing condition. Respec as two clauses,
   recorded as a dated amendment in docs/contract-errata.md ("TCK A5
   respec"):
   - **a5a (discharge on clean unload):** a clean successful unload
     DISCHARGES the compensation. It never runs; the forward emission (the
     insert) survives. Observable: no `DELETE` in the trace, row present.
   - **a5b (two-phase abort):** an abort runs Phase-1 proof inverses LIFO to
     completion, THEN Phase-2 compensations LIFO. Observable: every bracket /
     transactional inverse in the trace precedes the first compensation; the
     compensation DELETE now fires AFTER the earlier bracket unlock, the
     exact inversion of the old a5 assertion.
2. **Per-backend golden + adapter sweep, sequenced.** `pytest tests/` does
   NOT run the per-backend golden tests, so a green root suite proves nothing
   here. The respec must explicitly sweep `backends/*/golden`, the TCK
   adapters (`tck/adapters/`), AND the executed per-tier scenario suites,
   because that is where old-a5 behavior actually lives and executes: the go
   scenarios under `go test` (backends/go/scenarios), the typescript suite
   under `npm test` (backends/typescript), and the java scenario runner
   (backends/java/scenarios). Two facts about the adapter directory so
   nobody sweeps a phantom: there is exactly one adapter,
   `tck/adapters/py_adapter.py`; the other tiers exercise the spec through
   their own scenario harnesses above, not through per-tier adapters. And
   a5b needs a NEW fixture: the current adapter's a5 case only disposes a
   cleanly activated component; it has no abort-AFTER-the-compensated-emit
   path, and a5b's observable (proof inverses before the first compensation
   on abort) cannot be asserted without one. The sweep is SEQUENCED with the
   py tier: land the a5a/a5b spec change together with the py runtime flip
   and the py golden/adapter update first, then flip each remaining tier
   against the new assertion. At no point are the five non-py backends built against the old
   a5; a tier that has not flipped yet is a pinned divergence in tck/spec.py
   (the existing `Divergence` mechanism), not a silently red or silently
   stale golden.
3. **Loop conformance per tier.** Each tier's Slice-2b landing demonstrates,
   against this doc: mixed-entry LIFO in both phases (on wasm, over
   activation-registered entries only; see the per-tier obligations under
   the bound rule), continue-and-record on
   a Phase-1 failure (both severities), discharge of transactional and
   compensation entries on clean unload (bracket still runs), the
   between-compensation deadline check with skipped-not-silent records, the
   WAL descriptor + discharge records round-tripping through `revl recover`,
   and the residue envelope byte-shape above.
4. **Migration sweep before the behavior flip** (247 open question 2): the
   corpus check that no existing program relies on a compensation firing on a
   clean unload, done once, before the first tier flips.

## Open questions (left deliberately)

1. **Phase-2 budget defaults.** The check points, the records, and the
   config surface are fixed here (`REVL_COMPENSATION_BUDGET_MS` /
   `REVL_COMPENSATION_PER_CALL_MS`, with provisional defaults 5000/1000 ms,
   see the bound section); only the final NUMBERS are open, for the first
   Slice-2b landing to propose and this doc to then pin.
2. **`bracket-fault` escalation surface.** This doc fixes the record and its
   contract-grade severity; whether a bracket-fault should ALSO mark the
   session/fiber in a way distinct from 246's prompt (e.g. refusing further
   auto-approval for the whole session rather than the one activation) is a
   246 policy question, flagged there.
3. **Go abandonment residue.** `outcome: unknown` is specced for the
   abandoned in-flight call; whether recover should later reconcile an
   unknown outcome (query the referent to learn whether the emission landed)
   is open and touches the `World` adapter contract.
4. **Everything already deferred above:** the `:back` firing policy, the 246
   spec, the 245 explicit commit, the item-309 typed idempotency key.
