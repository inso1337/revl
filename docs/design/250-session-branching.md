# Design: session branching, fork a conversation with its side effects rewound (item 250)

Status: design proposed. No implementation. This composes machinery that has
already landed; the value here is composing it correctly and stating, exactly,
what the composition can and cannot honestly claim. Slice 1 is the smallest
landable workload-surface fork on the py runtime. The language `fork` form,
parallel copy-on-write branches, and the LLM-aware replay modes of the item-121
overlap are deferred with rationale.

## The one thing to get right

`fork <session> at <step k>` puts the workspace *actually in* the step-k state
and hands the user or the agent a branch to explore an alternative from there.
The word "actually" is the whole design. The reversible part of the tail above
step k (the witnessed filesystem state, the provisions, the still-held deferred
sends) can be put back and truthfully is. The irreversible part (an emission
that already crossed the boundary between k and head) cannot be put back and
must never be pretended away. A fork is therefore two things at once: an honest
rewind of what is rewindable, and a printed enumeration of what is not. If the
enumeration is ever incomplete or the rewind ever fires a fresh crossing, the
primitive is a lie. Everything below exists to make sure it is not.

Nothing here invents a new primitive. The rewind is the landed
`Timeline.step_back` (item 40 plus the 243/244 witnessed inverses). The
zero-residue guarantee is the landed 245 deferral queue. The snapshot is the
landed item-15 `snapshot`/`restore`. The crash story is the landed `recover`.
The one genuinely new line of code the design asks for is a rewind mode that
does *not* fire compensations (see the headline self-review finding); everything
else is orchestration and reporting over parts that exist.

## What already exists (the landed foundation this builds on)

- **The step-indexed timeline and the live rewind.**
  `Session.step_back(component, to, force)` (src/revl/mcp/session.py) unwinds the
  recorded accumulator to step `k` by running inverses newest-first (LIFO),
  leaving the component LIVE, not torn down (backends/python/replay.py,
  `Timeline.step_back`). It refuses with `IrreversibleStep` when the rewound span
  contains an uncompensated emission, unless `force=True`, in which case it
  crosses the span and returns the crossed emissions explicitly in
  `emissionsCrossed` (bare) and `emissionsCompensated` (offset), with a
  `warning_emissions`. This refuse-or-enumerate behavior is exactly the honesty
  contract a fork needs, already written.
- **Witnessed inverses (243/244).** A `_Transactional` disposer replays
  `undo(witness)` against the recorded preimage; the witnessed filesystem
  preimage/postimage machinery (backends/python/, src/revl/recovery.py
  `_roll_back`) is what makes "the fs is back at step k" true rather than a
  transcript claim. `step_back` runs these same inverses.
- **The deferral queue and the commit protocol (245).** `SessionOwner`
  (backends/python/runtime.py) owns a FIFO `_queue` of class-(b) `_Deferred`
  descriptors, WAL-logged at `enqueue`, flushed (fired once, program order) only
  by `approve`/`_flush` on commit, and DROPPED whole by `begin_abort`. A
  class-(b) send never crosses until the session commit fires it. This is the
  branch-isolation engine.
- **Snapshot and restore (item 15).** `Session.snapshot()` returns
  `{sources, manifest, meta}`, the inputs needed to re-admit the live
  composition through the gate (src/revl/mcp/persist.py). `restore`/`resume`
  re-admit it. A snapshot is a re-admission recipe, not a runtime-object dump.
- **The WAL and crash recovery.** The append log (backends/python/replay.py,
  src/revl/wal.py) carries `deferred-emission`, `commit-approved`, `flushed`,
  `flush-residue`, `discharge`, `aborted`, and the terminal `activation-complete`.
  `recover(wal_path)` (src/revl/recovery.py) reads it tier-agnostically: a
  complete WAL rolls forward, a `commit-approved`-present WAL is a committed
  session rolled forward, otherwise it rolls back LIFO.
- **Session identity and non-replayable approvals.** Each session mints
  `self._session_id = uuid.uuid4().hex` and a WAL at
  `default_wal_path(session_id)`; a standing `Approval[C]` token is bound to that
  session id and is refused in any other session (item 246 invariant 5). This is
  what keeps a branch from silently inheriting the parent's granted consent.

## Decision 1: the fork surface is an MCP verb, two-step and hash-bound

Slice 1 ships `revl_fork` as an MCP verb (session API `Session.fork` /
`Session.fork_confirm`). It is deliberately shaped like the 245 commit: a
two-step, hash-bound protocol, because the crossed-emission residue MUST be seen
before the rewind happens.

- `revl_fork(at: k)` ENUMERATES. It computes the rewound span (steps > k),
  partitions it, and returns the honest fork report (Decision 3) plus a `hash`
  binding the exact rewound span and the live composition. Nothing is rewound
  yet, no fs state changes, no branch is minted.
- `revl_fork_confirm(hash)` PERFORMS. It re-derives the hash; any drift (a new
  effect, a swap) since enumeration refuses with a fresh report, exactly as
  `SessionOwner.approve` refuses a stale commit hash. On match it runs the
  non-emitting rewind to k (Decision 2), snapshots the step-k state, mints the
  branch identity, and writes the WAL fork bracket (Decision 5).

Why not fire immediately on one call: a fork inherently crosses backward over
emissions that cannot be undone. Making the caller acknowledge the enumerated
crossed set through a hash is the same discipline 245 applies to the forward
commit, and it closes the "fork silently rewound past a send" attack by
construction.

The language `fork` form is deferred (Decision 8). The "sessions view offering
branch from here" is a workload UI that calls `revl_fork`/`revl_fork_confirm`;
it is not part of the compiler surface and needs no language change.

### Inputs and the sequence

```
fragment
revl_fork(at = k)                      # step 1: enumerate, returns report + hash
  -> partition tail(>k) into
       reversible   : witnessed effects, provisions            (will be rewound)
       held         : class-(b) deferred sends still in _queue  (will be dropped)
       crossed-bare : uncompensated emissions                  (CANNOT rewind)
       crossed-comp : compensated emissions                    (CANNOT rewind)
  -> hash binds (rewound-span identity, live composition)

revl_fork_confirm(hash)                # step 2: perform
  1. re-derive hash; refuse on drift
  2. non-emitting rewind to k   (run reversible inverses LIFO; DO NOT fire
                                 compensations; enumerate crossed set)
  3. drop the parent deferral queue    (held sends never crossed)
  4. snapshot() the step-k state       (item 15 re-admission recipe)
  5. mint branch identity: new session_id + new WAL; standing approvals do
     NOT carry across (246 invariant 5); restore the snapshot into the branch
  6. write fork-begin / fork-complete WAL bracket naming parent, k, crossed set
```

## Decision 2: the rewind is non-emitting (the honest rewind, not a teardown)

A fork's rewind must never itself cross the boundary. This is where a naive reuse
of `step_back(force=True)` is wrong, and it is the headline correctness point.

`Timeline.step_back` runs every step in the rewound span, and a
`KIND_COMPENSATION` step's inverse is, by its own recorded note, "a second
boundary crossing chosen to offset the first" (backends/python/replay.py,
`record_yield`). Running a compensation during the rewind therefore SENDS
something (a cancellation, a refund, an offsetting call). During a real teardown
that is the sanctioned G5 exception, because teardown is the end of the world.
During an exploratory fork it is a live external emission in the middle of a
primitive whose entire selling point is zero external residue. Firing it leaks.

Slice 1 adds a rewind mode (a `compensate=False` / `emit=False` flag on
`Timeline.step_back`, off by default so every existing caller and test is
byte-identical) that treats a `KIND_COMPENSATION` step exactly like a
`KIND_EMISSION` step: it is not run, it is ENUMERATED as an already-crossed
emission with an available-but-unfired compensation. The rewind then runs only
the non-emitting inverses (`KIND_EFFECT`, `KIND_PROVISION`), which are the
witnessed-extern inverses the 243 contract already guarantees do not emit. So
the fork rewind is G5-clean: no emission occurs during the rewind, and the
compensated crossings are surfaced as residue the caller may choose to act on
later, not fired behind the caller's back.

## Decision 3: the honesty model (the headline)

At step k, the tail (steps > k) is partitioned into four disjoint sets, each
derived mechanically from the recorded timeline, and each reported.

**Rewound (state truly restored).**
- `KIND_EFFECT` witnessed inverses: the fs preimage is restored via the 243/244
  `undo(witness)`. The workspace fs is actually at its step-k content.
- `KIND_PROVISION`: the provision is withdrawn (R5 runtime-derived inverse).
The claim "the workspace is actually in the step-k state" is scoped precisely to
this set: witnessed fs state and provisions. It is not a claim about anything in
the next two sets.

**Held, then dropped (never crossed, so free).**
- Class-(b) deferred sends still sitting in `SessionOwner._queue`. They never
  crossed the boundary, so there is nothing out in the world to enumerate as
  residue. Fork drops them exactly as `begin_abort` drops the queue, and reports
  them as `droppedDeferred` for lineage. This is the 245 asymmetry: the abort (or
  here, the fork-away) path for class (b) is exact by construction.

**Crossed, cannot be rewound (the residue that must be printed).**
- `crossed-bare`: uncompensated `KIND_EMISSION` steps. Each is a one-way boundary
  crossing with, by its own recorder note, "no inverse." A sent email is sent.
  These are enumerated verbatim in `emissionsCrossed`, each with its recorded
  `{key, method, service, args, site}`.
- `crossed-compensated`: `KIND_EMISSION` steps that have a `compensation`
  recorded. These are still crossed (the original send happened). They are
  reported distinctly in `emissionsCompensated`, carrying the compensation the
  caller may fire, because conflating them with the bare set would both overstate
  the first ("we have an offset for that send") and understate the second ("that
  send has an offset" when it does not). This distinction is paper section 6.1:
  compensation is not inversion, and the report says so in those words.

The fork report's headline field is the crossed set. A fork across a span that
contains any crossed emission is not refused (that would make fork useless the
moment an agent has done anything irreversible), but the crossed set is the thing
the confirm hash binds, so the primitive cannot rewind past a send without the
caller having been handed, and having acknowledged, the exact list.

### Fork report shape

```
fragment
{
  "forked": true,
  "at": k,
  "atLabel": "<step k label>",
  "parent": "<parent session_id>",
  "branch": "<new branch session_id>",

  "rewound": {
    "inversesRan": [ {step...}, ... ],       // KIND_EFFECT, witnessed fs restored
    "provisionsWithdrawn": [ {step...}, ... ]
  },

  "droppedDeferred": [ {seq, receiver, method, args}, ... ],  // held, never crossed

  // the residue that CANNOT be rewound, printed, never pretended away (6.1):
  "emissionsCrossed":     [ {index, key, method, service, args, site}, ... ],
  "emissionsCompensated": [ {index, key, method, service, args, site,
                             compensation: <step index, unfired>}, ... ],

  "residue": {
    "clean": false,                          // true iff both crossed sets empty
    "outstanding": [ <index>, ... ],
    "proof": "N emission(s) crossed the boundary between step k and head and
              cannot be undone; they are enumerated above. The fs state and
              provisions above this line were restored to step k."
  },
  "warning_emissions": "<N> emission(s) are still out in the world.",
  "guarantee": "<the timeline GUARANTEE string>"
}
```

When both crossed sets are empty (a fork over a span of only witnessed effects
and held sends), `residue.clean` is true and the fork is a provably exact rewind,
the same way a class-(a)/(b)-only session aborts to a provably clean world in 245.

## Decision 4: branch isolation and zero external residue until commit

A branch is a distinct session identity over the (single, Slice-1) workspace.

- **Fresh deferral queue.** `fork_confirm` mints a fresh `SessionOwner` for the
  branch, so the branch's `_queue` starts empty and every class-(b) send the
  branch makes is held in the branch's own queue. It flushes only on the branch's
  own `commit_confirm`. Explore the branch, and abort it, and no send ever
  crossed. This is 245 reused unchanged: the zero-external-residue guarantee is
  not re-proven here, it is inherited.
- **Fresh session id, so approvals do not carry.** The branch mints a new
  `session_id`. A standing `Approval[C]` the human granted in the parent is bound
  to the parent's session id and is refused in the branch (246 invariant 5). A
  branch exploring an alternative must re-earn consent for a class-(c) crossing;
  it cannot auto-approve a send under a token the human granted for a different
  line of exploration.
- **Distinct WAL.** The branch's records are written to its own WAL at
  `default_wal_path(branch_session_id)`. The parent WAL is not appended to by the
  branch (beyond the parent's own `fork-begin`/`fork-complete` bracket, Decision
  5). Parent and branch recover independently.
- **N branches, zero residue, explored SERIALLY (Slice-1 scope).** The
  guarantee the roadmap states ("exploring N branches costs zero external residue
  until one is chosen") is a guarantee about external sends, and it holds because
  every branch holds its sends. In Slice 1 the branches are explored one at a
  time over the single rewindable workspace: fork to k, explore branch A, abort A
  (which rewinds the witnessed fs back toward k via A's own inverses), fork to k
  again, explore branch B. At most one branch is live at a time. True parallel
  divergent filesystem states would require a per-branch copy-on-write workspace
  and are deferred (Decision 8 and self-review attack 3); a second concurrent fork
  while a branch is live is REFUSED in Slice 1.

## Decision 5: crash recovery of a branched session

Fork reuses `recover` unchanged; it adds only a WAL lineage bracket and leans on
the invariant that each session's WAL recovers independently to its own last
consistent point.

- **The fork bracket.** `fork_confirm` writes `fork-begin {parent, at: k,
  crossed: [...]}` to the PARENT WAL before the rewind touches the fs, and
  `fork-complete {branch}` after the branch snapshot is restored. The crossed set
  is durable in the parent WAL, so the irreversible residue survives a crash and
  is not silently lost.
- **Crash after fork-complete.** Parent and branch each have their own WAL.
  Recover on the branch WAL behaves exactly as 245: incomplete and no
  `commit-approved` rolls the branch back over the branch's own witnessed effects
  (the effects since the fork), reaching the branch's fork point, which is the
  step-k state. Recover does NOT try to reconstruct the branch topology or
  re-derive it from the parent; the `fork` records are informational lineage, not
  a replay script. Recover on the parent WAL is untouched by the branch.
- **Crash mid-fork (between fork-begin and fork-complete), the half-rewound
  workspace.** This is the dangerous window: the rewind runs witnessed inverses
  against the fs, and a crash can leave the fs partially rewound. On recover the
  parent WAL has `fork-begin` but no `fork-complete`; recover treats the parent
  activation as incomplete and rolls it back over its own recorded inverses. Those
  inverses re-run against a fs some of whose steps already had their inverse run
  mid-fork. This is safe ONLY if the rewound inverses are idempotent-total (item
  309): re-running an already-applied inverse is a no-op. Slice 1 therefore
  REFUSES a fork whose rewound span contains a non-idempotent-total inverse
  (reusing 309's classification), and recovers a mid-fork crash by completing the
  parent rollback. A mid-fork crash never collapses into a branch; it collapses to
  the parent rolled back to its last consistent point (step k or, if 309 lets it
  go further, the last discharge boundary). Non-idempotent rewound spans are an
  open item (self-review attack 5).

## Decision 6: fork of a session with in-flight vs committed actions at k

- **In-flight class-(b) deferred at fork time.** Dropped (Decision 3, held set).
  The branch starts with an empty queue. Enumerated as `droppedDeferred`.
- **Class-(c) already crossed above k.** Enumerated as `emissionsCrossed` /
  `emissionsCompensated` (Decision 3). Cannot be rewound.
- **A commit boundary below the fork point.** A `commit_confirm` writes
  `commit-approved` then `flushed`... then `activation-complete` and closes the
  WAL. Fork operates within a single uncommitted activation's timeline. A fork
  whose `k` lies before a step that has already durably committed (a `flushed`
  record exists for it) is REFUSED: you cannot fork to a point before a crossing
  that has already committed and closed, because that crossing is durable and the
  window below it is a different, closed session. The rewindable window is
  `[last-commit-boundary, head]`. This keeps the primitive from ever appearing to
  rewind a committed, flushed send.

## Decision 7: the G-invariants during the fork

- **G5 (no emission in teardown), during the inverse replay.** The fork rewind is
  not a teardown, and Decision 2 makes it strictly non-emitting: only
  `KIND_EFFECT`/`KIND_PROVISION` witnessed inverses run, and those do not cross
  the boundary (243 contract). Compensations, which would cross, are enumerated
  and not fired. So no emission occurs during the rewind, which is stronger than
  G5 requires.
- **G7 (LIFO teardown order).** `step_back` already unwinds newest-first; the fork
  rewind runs the same inverses in the same LIFO order the teardown uses, so G7
  holds unchanged.
- **The witnessed-inverse contract (243/244).** The fork rewind runs the exact
  `undo(witness)` the contract defines against the recorded preimage; it is the
  same replay `abort` and `recover` use. No new inverse machinery is introduced,
  so the contract's guarantees carry over directly.
- **The enumeration exhaustiveness (245 Decision 4).** The crossed set is derived
  from the timeline's recorded `KIND_EMISSION` steps, and 245 already proves the
  queue-plus-timeline is the exhaustive set of crossings (erase_report enumerates
  every reached extern per component; there is no unenumerated way out of the
  process). Fork inherits that exhaustiveness proof rather than re-deriving it.

## Adversarial self-review

Every prior item design review found a CRITICAL. Here is this design's, found
first.

### CRITICAL 1: the naive rewind fires compensations and leaks external residue

A fork that reuses `Timeline.step_back(force=True)` directly runs every
`KIND_COMPENSATION` step in the rewound span, and a compensation's inverse is a
fresh outbound boundary crossing ("a second boundary crossing chosen to offset
the first", backends/python/replay.py). So a primitive sold as "explore N
branches at zero external residue" would, on the very first fork across a
compensated emission, SEND a cancellation or a refund into the world, mid-turn,
with no commit and no prompt. The zero-residue claim would be false exactly when
the workload is doing the reversible-pattern (send + compensate) that the whole
243/247 stack was built for.
Mitigation (in the design, Decision 2): the fork rewind is non-emitting. A new
`compensate=False` rewind mode treats compensations as crossed emissions,
enumerating them (unfired) instead of running them. Only witnessed non-emitting
inverses run. This is the single new line of behavior the design requires and it
is off by default so every existing test is byte-identical. Resolved.

### CRITICAL-adjacent 2: a fork that pretends to rewind an irreversible send

Attack: fork reports the workspace "actually at step k" while a class-(c)
emission crossed above k, letting the agent reason as if the send never happened.
Mitigation: the crossed set is a mandatory, headline field of the fork report
(Decision 3), the confirm is hash-bound to it (Decision 1), and the "actually at
step k" claim is scoped in words to witnessed fs plus provisions only, with the
crossed emissions carried in a separate `residue` envelope whose proof states
they cannot be undone. The rewind never mutates or hides a `KIND_EMISSION` step.
Resolved.

### Attack 3: two branches sharing witnessed fs state corrupt each other

Attack: fork twice from k, run branch A and branch B concurrently, both writing
the same witnessed file; A's inverse restores a preimage B has since overwritten,
and both branches' fs state is corrupt while both report `noResidue`.
Mitigation (Slice-1 scoping, Decision 4): Slice 1 permits only SERIAL branch
exploration over the single rewindable workspace; at most one branch is live, and
a second concurrent fork is refused. The zero-external-residue guarantee (about
sends) still holds for serially explored branches. OPEN: true parallel divergent
fs states, which require a per-branch copy-on-write workspace snapshot, are
deferred to a later slice (Decision 8). The design does not claim parallel fs
isolation in Slice 1; it refuses the operation that would need it.

### Attack 4: crash mid-fork leaves a half-rewound workspace

Attack: crash during the inverse replay; the fs is partially rewound and the
parent WAL still describes the full forward history, so recover re-runs inverses
that already ran and double-applies them.
Mitigation (Decision 5): the `fork-begin`/`fork-complete` WAL bracket makes the
window recoverable, and Slice 1 REFUSES a fork whose rewound span contains a
non-idempotent-total inverse (item 309), so a recover-time re-run of an
already-applied inverse is a no-op and the parent rolls back to a consistent
point. OPEN: forks across non-idempotent inverse spans (the design refuses them
rather than shipping an unsound recovery).

### Attack 5: the crossed-emission enumeration is incomplete

Attack: a crossing exists that never made it into the timeline's
`KIND_EMISSION` set (an extern reached by a path the recorder did not
instrument), so the fork report under-enumerates and an agent trusts a rewind
that silently dropped a send.
Mitigation (Decision 7): the enumeration is the same set 245 Decision 4 proves
exhaustive via `erase_report` (every reached extern per component is enumerated;
there is no unenumerated way out of the process), and a compensated crossing is
reported distinctly from a bare one so neither set is padded or thinned. This
reduces the fork's completeness to 245's already-argued completeness rather than
asserting a fresh one. Residual: if 245's enumeration is ever shown incomplete
for a tier, the fork inherits that gap; the fork adds no new escape but also
closes none. Tracked as depending on the 245 exhaustiveness proof.

### Attack 6: a branch inherits the parent's standing approval and auto-sends

Attack: the human approved an `Approval[C]` for a class-(c) send in the parent;
the branch reuses it to auto-approve a send the human never saw in the branch's
context.
Mitigation (Decision 4): the branch mints a new `session_id`, and a standing
approval is bound to the session id it was granted in (246 invariant 5), so the
parent's token is refused in the branch. The branch re-earns consent. Resolved.

## Slice plan

### Slice 1 (the smallest landable fork, py runtime)

The honest workload-surface fork, reusing the landed timeline, recovery, commit,
and snapshot machinery.

1. `Timeline.step_back` gains a `compensate=False` rewind mode (Decision 2), off
   by default so all existing callers and goldens are byte-identical.
2. `Session.fork(at=k)` enumerates: partition the tail, build the fork report
   (Decision 3), return it with a hash binding the rewound span and the live
   composition.
3. `Session.fork_confirm(hash)` performs (Decision 1 sequence): re-derive and
   check the hash, run the non-emitting rewind to k, drop the parent queue,
   `snapshot()` the step-k state, mint the branch identity (new session_id, new
   WAL, no approval carry), restore the snapshot into the branch, write the
   `fork-begin`/`fork-complete` bracket.
4. MCP verbs `revl_fork` / `revl_fork_confirm` wired in src/revl/mcp/server.py,
   documented alongside `revl_timeline` / `revl_step_back` / `revl_snapshot`.
5. Refusals: a fork before a `flushed` commit boundary (Decision 6); a fork whose
   rewound span has a non-idempotent-total inverse (Decision 5); a second
   concurrent fork while a branch is live (Decision 4).
6. Recover reads the new `fork-begin`/`fork-complete` records as lineage; the
   recovery verdict logic is otherwise unchanged (Decision 5).

Slice 1 explicitly does NOT change any emitter or any tier but py; the fork lives
in the py MCP session and the py replay/runtime, exactly where the timeline,
owner, and recovery already live. No IR, no cross-tier golden, no language
grammar changes.

### Deferred to later slices (with rationale)

- **Parallel branches with copy-on-write workspaces.** Needed for true concurrent
  N-branch exploration with isolated divergent fs state (self-review attack 3).
  Requires a per-branch workspace snapshot layer that does not exist yet; Slice 1
  refuses the concurrent case rather than shipping unsound sharing.
- **The language `fork` form.** Deferred until it earns its place, per the roadmap.
  The workload surface (the sessions view calling `revl_fork`) delivers the
  capability with zero language-surface risk. A language form would have to answer
  where a `fork` expression's branch value lives and how the type system sees two
  divergent continuations, which is a large design in its own right and is not
  required to ship the primitive.
- **LLM-aware replay modes (item 121 / external proposals 1 and 4 overlap).**
  Recording each model decision in the WAL and offering exact / tool-only /
  model-substitute / counterfactual replay, plus `revl branch run --at`,
  `revl replay branch`, `revl compare`. This is the counterfactual-history layer
  on top of the branch primitive; it depends on Slice 1's honest fork existing and
  on an LLM-aware WAL that is its own item. Deferred and cross-referenced, not
  designed here.
- **Non-idempotent rewound spans.** Open (self-review attack 4); Slice 1 refuses
  them.
