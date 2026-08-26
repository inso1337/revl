# Design: `compensate`, typed best-effort compensation (item 247)

Status: design proposed. Failure semantics and accumulator ordering decided
here; implementation queues behind item 243 Slice 2 (the runtime teardown
seam). Design-first per the item's own blocker (what happens when compensation
itself fails) and per the roadmap note that compensate is a third accumulator
behavior.

## The one thing to get right

`compensate` is a **third reversal form**, a sibling of `undo` and of `243`'s
`witnessed`, not a variant of either. The three do not differ by degree; they
differ by which surface they land on and whether they may emit.

| | `undo` (bracket / effect) | `witnessed` (243, transaction) | `compensate` (247) |
|---|---|---|---|
| surface | proof (G4/G7) | proof (G4, reversible crossing) | audit (G8 intent, paper §6.1) |
| may emit in teardown | **no** (G5) | **no** (host-local inverse) | **yes** (that is the point) |
| may fail | no, must be infallible | inverse fallible, feeds 246 | yes, best-effort |
| accumulator entry kind | `bracket` | `transactional` | `compensation` |
| replays on abort | yes | yes (the inverse) | yes (best-effort, second phase) |
| replays on clean unload | yes (release the handle) | no (discharge + GC the witness) | **no** (discharge, never run) |
| moves the reversible boundary | no (releases a handle) | **yes** (irreversible -> reversible) | **no** (stays irreversible; records intent) |

The load-bearing line is the last one. A `witnessed` effect is *made*
reversible by an engineering witness, so its inverse restores the pre-state and
the effect leaves the irreversible column (docs/design/243-witnessed-externs.md).
A `compensate` does not. The original `emit` stays an emission on the boundary
surface, counted as a real crossing that already left the system; the
compensation is a *second* crossing chosen to offset the first. This is the
`erase_report` invariant already written into the codebase: compensation is not
inversion, and the report enumerates the exposure rather than claiming to undo
it (src/revl/erase_report.py:27-38, the `HONEST_SCOPE` header). Item 247 makes
that distinction a type judgment and gives it a runtime, without ever letting a
best-effort emission masquerade as a proof.

## What already exists (and the gap)

The surface and a placeholder runtime are already in the tree; 247 is not a
greenfield feature, it is the correct semantics for a form that currently has
naive ones.

Present today:

- Parse. `emit <call> compensate <expr>` at a call site (src/revl/parser.py:1398-1402)
  and an extern-level `emission ... compensate <expr>` slot (parser.py:1022-1028).
- Check. `compensate` is legal only on an `emission` extern, not on
  `pure`/`acquire`/`witnessed` (lower.py:1577-1602), not on an `async` extern
  (lower.py:1674-1678), and its expression is checked by the shared extern-slot
  walk with `result` unbound (lower.py:1364-1373, `_check_extern_undo(..., "compensate")`).
- Lower. A component-body `emit ... compensate` lowers the compensation with
  `mode="undo"`, which is what permits a bare emission inside it (lower.py:4693-4695).
- Emit. The Python backend writes the compensation as `yield lambda: <expr>`,
  joining the single teardown accumulator exactly like an inverse
  (backends/python/emit.py:939-942, 1133-1137).
- Record and recover. The replay log carries a `compensation` step kind and a
  `compensatedBy` back-reference identified by source adjacency
  (backends/python/replay.py:39-43, 73-76, Step.compensation), the audit
  surface tags each crossing compensated-vs-bare (src/revl/query.py:850-851,
  erase_report.py:183-189), and `revl recover` already models a compensation as
  a further crossing that records rather than clears its referent
  (src/revl/recovery.py:105-109, 287-293).

The gap is the runtime contract, which the placeholder gets wrong in three
ways:

1. **It runs on every teardown, not only on abort.** A `yield lambda:` on the
   accumulator drains at withdraw whether the activation succeeded or failed
   (emit.py:942). So today the DB `compensate db.delete(row.id)` fires on a
   clean, successful withdrawal too, deleting the row the insert was supposed to
   deliver. Best-effort cleanup on success is wrong; the forward emission is the
   deliverable.
2. **It is interleaved with proof-grade inverses.** Because it sits in the one
   LIFO stack, a fallible best-effort emission runs between two infallible
   host-local inverses, so a failure in it can interrupt the provable rollback.
3. **A failure is unguarded.** If the compensation lambda raises, it propagates
   out of the teardown drain and turns a clean rollback into a crash. That is
   the exact thing a best-effort form must never do, and it is the item's stated
   blocker.

The rest of this doc fixes those three, in that order.

## Model: three accumulator entry kinds

243 established that the teardown accumulator is one data structure with more
than one entry behavior: `bracket` (acquire, replays on clean unload and abort)
and `transactional` (witnessed, replays on abort only, discharged and
GC'd on commit). 247 adds the third:

- `compensation`: **abort-only**, runs in a **second phase after all proof
  replay**, **best-effort** (guarded, may fail without failing the abort),
  **discharged on clean unload/commit** (never run on success). It is not a
  proof entry; nothing about a guarantee depends on it running.

Making `compensation` a first-class entry kind, carried as an IR descriptor on
the `emit` step, replaces today's source-adjacency heuristic (replay.py:39-43)
with an explicit mark. The runtime no longer infers "this yielded inverse is
really a compensation because its line number is one past an emission"; the IR
says so.

## Decision 1: abort teardown runs in two phases

On an abort (an A8 mid-body failure / L-Raise unwind, or a `:back` that crosses
the activation), teardown runs in two ordered phases:

- **Phase 1, proof replay.** Every `bracket` and `transactional` inverse, LIFO
  by registration order, interleaved exactly as today. These are host-local and
  infallible by construction (G5 forbids them from emitting), so this phase is
  deterministic and cannot be interrupted. It restores the maximal reversible
  pre-state.
- **Phase 2, intent.** Every `compensation` entry, LIFO within the compensation
  class. These may emit and may fail. Phase 2 begins only after Phase 1 has run
  to completion.

Rationale for the split (this resolves the roadmap's open "undos first, then
compensations, both LIFO within their class?" with a yes):

- **A best-effort failure must never interrupt proof-grade recovery.** If the
  fallible emission is interleaved with the infallible inverses (today's stack),
  a compensation that raises can leave later inverses un-run. Draining all proof
  entries first guarantees the provable rollback always completes in full,
  regardless of what the best-effort tail does.
- **Compensations offset only the residue proof replay could not reverse.** A
  `witnessed` effect is fully reversed in Phase 1, so it needs no compensation
  (see Decision 4). Running compensations last means they act against the world
  as it stands *after* every reversible thing has already been put back, which
  is the smallest, most honest set of irreversible crossings left to offset.
- **LIFO within the class** is the saga contract: compensate the most recent
  forward emission first, unwinding the causal order.

The arguments a compensation computes are **captured at registration**, not
re-read in teardown: the compensation closes over (or, for recovery, serializes,
see Decision 5) the values in scope when the `emit` ran. So the phase ordering
changes only *when* the offsetting emissions fire relative to the inverses, not
*what* they compute. This keeps the two-phase split sound with no data hazard
between the phases.

On clean unload / commit, `compensation` entries are **discharged, never run**,
mirroring the `transactional` discharge-and-GC (243). A clean success means the
forward emission was the deliverable; there is nothing to offset.

## Decision 2: failure semantics (the item's blocker)

A Phase-2 compensation may raise, or its emission may fail at the host. Because
it is best-effort it **cannot abort the abort**. The rule:

- Each compensation runs **guarded**. On a raise or a failed emission, Phase 2
  **records an `unresolved-compensation-residue` fact on the audit surface** and
  continues with the remaining compensations. The record names the original
  emission it was offsetting (key, method, args, site), the compensation that
  was attempted, and the error.
- The abort itself still **succeeds**. A compensation failure never fails the
  teardown, never re-enters Phase 1, and never touches the proof surface. The
  provable rollback from Phase 1 stands exactly as proven.
- It is **never silently swallowed.** The residue is enumerable on the same G8
  audit surface that already tags crossings compensated-vs-bare
  (query.py:850-851, erase_report.py:183-189). A third state joins those two:
  `bare` (nothing attached), `compensated` (attached and completed), and
  `unresolved` (attached, attempted, failed).
- The residue **surfaces a prompt at the session boundary** through item 246,
  the same way 243 requires a restore-residue to surface a prompt rather than
  let "auto-approved because revertible" silently degrade
  (docs/design/243-witnessed-externs.md, correctness rule 6). Where 243's
  residue is "an inverse we promised was infallible was not," 247's is "a
  best-effort offset we attempted did not land." Both are proof-backed by the
  audit enumeration; neither is discovered by reading logs.

The honest verdict language already exists for this. `revl recover`'s residue
proof states outstanding boundary state plainly and never claims a dead closure
ran (recovery.py:280-293); the abort-time residue reuses that voice: it reports
what was owed and not completed, and does not pretend the world is clean.

## Decision 3: type surface

Keep the existing spelling. v1 is `emit <call> compensate <expr>` at a call
site and `emission ... compensate <expr>` on an extern. No new grammar.

What may appear inside a `compensate`:

- **Emissions are allowed.** This is the one place a teardown-position
  expression may cross the boundary, and it is why the form exists. The lowering
  already permits it via `mode="undo"` (lower.py:4693-4695). Prefer a single
  emission call whose arguments are serializable data, so the compensation has a
  WAL descriptor (Decision 5).

What is forbidden inside a `compensate` (checker obligations, some new):

- **No new reversible-effect registration.** A compensation may not `acquire`,
  open a `witnessed` effect, or run an `effect ... undo`. Those would push a
  `bracket` or `transactional` entry onto the accumulator during teardown, and
  nothing would ever run their inverse (G5/G7). A compensation emits and
  returns; it does not accumulate.
- **No `witnessed` call.** A best-effort form may not borrow a proof. A
  witnessed crossing inside a compensation would claim reversibility the
  compensation cannot keep.
- **No `await`.** The compensation seam is synchronous on every tier, already
  enforced for `async` externs (lower.py:1674-1678); the same holds for the
  call-site form.
- **No `result`.** The extern slot already binds nothing (lower.py:1364-1373);
  a compensation follows a one-way emission, which acquires no value.

How the checker marks it best-effort: it **does not change the emission's
classification**. The `emit` stays an `emission` on the G8 boundary surface and
is counted as an irreversible crossing. `compensate` attaches an audit
annotation (`compensatedBy`) and, in the new IR, a `compensation` entry
descriptor with `best_effort: true`. There is no `G`-guarantee that a
compensation ran; no proof may depend on it. This is the whole point of the
audit-vs-proof split, and it is what keeps `compensate` from weakening G5: a
proof-grade inverse still may never emit, and the sanctioned emission-in-teardown
is quarantined to Phase 2, best-effort, audit-only.

Decision: v1 keeps the **single-expression** form. A multi-statement
`compensate { ... }` block is deferred (see Open questions), because a block
raises a per-statement failure-granularity question that the expression form
sidesteps: an author who needs several offsetting emissions writes one helper
`fn` and calls it, and the whole helper is one compensation that either lands or
becomes one residue record.

## Decision 4: boundary against 243/witnessed, and against G4/G5

The author's decision tree, stated so the three forms do not blur:

- The mutation has a **host-local, infallible inverse** (close a handle, rename
  a file back): use `effect ... undo` or a `witnessed` extern (243). Phase 1,
  proof surface, moves nothing or moves the effect into the reversible column.
- The crossing is **irreversible** but a **best-effort offsetting emission**
  exists (a DB insert whose offset is `emit db.delete(id)`, a sent message whose
  offset is `emit mail.recall(id)`): use `emit ... compensate`. Phase 2, audit
  surface, offsets but does not un-issue.
- The crossing is **irreversible** and nothing can be done: bare `emit`. It
  stays `bare` on the audit surface, which is the honest state.

Two consequences that resolve the roadmap's composition questions:

- **`compensate` exists only for emissions that were not witnessed.** A
  witnessed effect is fully reversed in Phase 1; you never also compensate it,
  and the checker's ban on a `witnessed` call inside a compensation keeps the
  two from nesting. Compensation is for the irreversible tail that witnessed
  effects, by construction, cannot cover.
- **A committed transaction's compensations are discharged, not run.** On a
  clean commit the `transactional` inverses discharge and GC (243) and the
  `compensation` entries discharge alongside them. Compensation is abort-only in
  the same sense witnessed rollback is.

G5 stays exactly as strong as before. It forbids a *proof-grade* inverse from
emitting, because an emission in teardown cannot be replayed exactly and would
collapse recovery exactness. 247 does not relax that. It adds a separate,
explicitly labelled, best-effort channel that the proof never trusts, which is
precisely why the emission it carries is safe to allow: no guarantee rests on
it.

## Decision 5: recovery and the WAL

A `compensation` entry is WAL-logged as a **boundary descriptor**: a named
emission call with captured serializable arguments, the same data constraint
243 puts on a witnessed inverse (docs/design/243-witnessed-externs.md,
correctness rule 4; recovery.py:28-38). The current `yield lambda:` closure
(emit.py:942) does not satisfy this. A closure cannot be re-issued in a fresh
process, so after a crash it is residue, not recovery. The slice work must lower
a compensation to a descriptor exactly as 243 Slice 2 did for witnessed
inverses.

On a crash mid-activation, `revl recover` rolls back
(recovery.py:210-277): Phase 1 reconstructs and runs the boundary inverses LIFO
(existing plus 243), then a new Phase 2 re-issues each `compensation` descriptor
against the `World` adapter. `recovery.DictWorld` already treats a compensation
verb as a further crossing that records rather than clears the referent
(recovery.py:99-109), which is the correct "not inversion" behavior, so the
recovery half composes with what is there.

Honest reporting is mandatory and already has a shape:

- A compensation whose descriptor **cannot be reconstructed** (closure-only, or
  non-serializable args) is reported as residue, never as run
  (recovery.py:247-254, `_residue_proof`).
- A compensation whose re-issued emission **fails** is reported as residue with
  its error, same channel as Decision 2's abort-time residue.
- The verdict is honest: a rollback that owed a compensation it did not complete
  is `rolled-back` with compensation residue, never clean. The residue proof
  names exactly what is still out in the world.

Because abort-time Phase 2 can itself crash and `revl recover` will re-attempt,
a compensation should be **idempotent** or carry an idempotency key, the same
requirement 243 puts on inverse replay (correctness rule 5). v1 states the
requirement and relies on author-supplied idempotency; a typed idempotency key
is item 309's territory and is flagged below.

## Slice plan

- **Slice 0 (this doc).**
- **Slice 1: frontend + IR.** Promote the compensation from a sub-expression
  lowered with `mode="undo"` to a first-class `compensation` entry descriptor on
  the `emit` step (`entry_kind: "compensation"`, `best_effort: true`, plus the
  WAL emission descriptor). Tighten the checker to the Decision 3 obligations:
  keep emissions allowed, forbid new reversible-effect registration, forbid
  `witnessed`/`await`, keep the emission's own classification unchanged. Additive
  at the IR level; the backends keep emitting the placeholder until Slice 2, so
  the suite stays green. Files: src/revl/lower.py, parser.py (no grammar change),
  diagnostics.py (the new residue category). Tested at parse/check/IR level.
- **Slice 2: six-tier runtime seam** (queues behind 243 Slice 2, same teardown
  loop). Split abort teardown into the two phases (Decision 1); make
  `compensation` abort-only and discharged on clean unload (Decision 1); guard
  each Phase-2 compensation and route a failure to the audit residue record
  (Decision 2); emit the WAL descriptor and add the `revl recover` Phase-2 attempt
  (Decision 5). This is the behavioral change away from today's
  interleaved-every-teardown placeholder, so it lands with a migration sweep
  (Open question 2). Async-extern-scale rollout across tiers.
- **Slice 3: 246 integration + metrics.** Wire the residue to the session-boundary
  prompt (Decision 2), and report per-session compensations owed / completed /
  residue, feeding item 246's prompts-per-session number.

## Open questions

1. **`compensate { }` block.** Deferred to keep v1's failure granularity simple
   (one compensation = one residue record). If a multi-statement block is later
   wanted, decide whether each statement is an independent compensation entry
   (independent failure, independent residue) or the block is one entry that
   fails whole. The helper-`fn` workaround covers the common case in the
   meantime.
2. **Migration of the placeholder's clean-unload behavior.** Today a compensation
   runs on every teardown (emit.py:942); Slice 2 makes it abort-only. Any
   existing program, example, or test that relies on a compensation firing on a
   clean withdrawal changes behavior. A corpus sweep is needed before Slice 2 to
   confirm none does (and to convert any that does to an explicit `undo`/`witnessed`
   if the intent was bracket cleanup). Flagged for the implementer.
3. **Typed idempotency key.** Recovery re-attempt (Decision 5) assumes
   idempotent compensations. Whether the language should require an idempotency
   key on a recoverable compensation, rather than trust the author, is item 309's
   scope; 247 states the requirement and defers the mechanism.
