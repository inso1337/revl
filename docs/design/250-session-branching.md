# Design: session branching, fork a conversation with its side effects rewound (item 250)

Status: design proposed. No implementation. This composes machinery that has
already landed; the value here is composing it correctly and stating, exactly,
what the composition can and cannot honestly claim. Slice 1 is the smallest
landable workload-surface fork on the py runtime. The language `fork` form,
parallel copy-on-write branches, and the LLM-aware replay modes of the item-121
overlap are deferred with rationale.

## Revision (adversarial review 2026-09-01)

An independent review of the first draft found one CRITICAL the author's own
self-review missed, two HIGHs, and a MEDIUM. All four are folded in below. The
draft's core mistake was keying the non-emitting rewind, and the honesty
partition, on the timeline's step KIND. KIND is the wrong axis: it does not tell
you whether an inverse crosses the boundary, and it does not cover all seven
recorded kinds. The corrected model keys the rewind on the inverse's recorded
CAPABILITY SCOPE and makes the partition total over every kind.

The four changes:

1. **Scope-gated rewind, not KIND-gated (was CRITICAL 2, new).** The draft ran
   every `KIND_EFFECT` inverse on the claim that 243 guarantees witnessed
   inverses do not emit. It does not. 243 Rule 3 is a DECLARATION-level check
   over an opaque `@py`/`@ts` host body; revl cannot see what that body does at
   runtime. A `witnessed` inverse whose declared cap scope is outbound (item 254
   witnessed NETWORK effects declare `undo` = "PUT the preimage back", an
   outbound crossing; or any author `witnessed` whose `undo` is a `pure`-declared
   extern that does outbound I/O in its host body) would PUT to a remote endpoint
   mid-fork with no commit and no prompt, the same catastrophe as CRITICAL 1, and
   the draft would have counted it in `inversesRan` so `residue.clean` reported
   true. Decision 2 now runs an inverse only when its declared capability scope
   is provably host-confined; any inverse whose scope includes a network/IPC/
   outbound capability is enumerated as would-cross-on-rewind residue and NOT
   run, forcing `residue.clean=false`.

2. **Parent is frozen at fork_confirm (was HIGH 1).** The rewind operates on the
   SHARED `REVL_FS_WORKSPACE` in place, and `step_back` leaves the component
   LIVE. So after `fork_confirm` the on-disk workspace is at step k while the
   parent's timeline still reads head: the parent's next witnessed op would
   capture preimages against rewound content (silent corruption), and parent
   abort/recover would replay against a mismatched workspace. Decision 4/5 now
   RETIRE/FREEZE the parent at `fork_confirm`: the parent becomes non-callable
   and the branch is the only live continuation from k. Slice 1 refuses any fork
   that would need the parent to stay live over the shared root (the copy-on-write
   clone that would allow a live parent is deferred).

3. **The partition is now provably TOTAL (was HIGH 2).** The timeline has seven
   kinds; the draft's four-way partition silently dropped `KIND_OPAQUE` (a
   non-callable disposer with no undo: `step_back` skips it AND the report omitted
   it, so accumulated state was neither restored nor reported while the fork
   claimed step-k state). Decision 3 now walks every tail step into exactly one
   bucket, adds an `unrestored` bucket that catches `KIND_OPAQUE` (with its
   recorded repr) and any future kind, sets `residue.clean=false` when it is
   non-empty, and REFUSES a Slice-1 fork whose tail contains a `KIND_OPAQUE` step
   (mirroring the non-idempotent refusal). `KIND_BOUNDARY`/`KIND_HINGE` are stated
   as provably empty (scaffolding, no undo). A `KIND_COMPENSATION` in the tail
   whose referent emission is below k has a defined home too (Decision 3).

4. **Pin the new report shape against goldens (MEDIUM).** `compensate=False` is
   off by default so byte-identity holds for existing callers, but it changes the
   loop terminal routing and adds new report fields (`wouldCrossOnRewind`,
   `unrestored`). Slice 1 pins the `compensate=False` report shape with a
   dedicated golden and runs the PER-BACKEND golden suite, not just
   `pytest tests/`, which does not exercise the backend goldens a new report
   field can drift.

### The corrected honesty model

At step k the tail (steps > k) is partitioned into buckets that are mutually
disjoint and provably total over all seven kinds. An inverse is RUN, and its
state counted as restored, ONLY when its recorded capability scope is provably
host-confined; every inverse or step that could cross the boundary, or that the
recorder cannot restore, is ENUMERATED as residue and never counted as restored.
The parent session is frozen at the moment of the rewind so the shared workspace
has exactly one live owner. `residue.clean` is true only when nothing crossed,
nothing would cross on rewind, and nothing was left unrestored. The one-sentence
claim the fork is allowed to make is: the host-confined witnessed state and the
provisions above step k are back at their step-k content; everything else is
printed, not pretended away.

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
The one genuinely new behavior the design asks for is a rewind mode that runs
only inverses whose recorded capability scope is provably host-confined and
enumerates the rest (see Decision 2 and the Revision section); everything else
is orchestration and reporting over parts that exist.

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
  scope-gated rewind to k (Decision 2), freezes the parent (Decision 4/5),
  snapshots the step-k state, mints the branch identity, and writes the WAL fork
  bracket (Decision 5).

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
  2. scope-gated rewind to k    (run host-confined inverses LIFO; enumerate
                                 outbound-scoped inverses + compensations as
                                 wouldCrossOnRewind; enumerate crossed set)
  3. drop the parent deferral queue    (held sends never crossed)
  4. FREEZE the parent session         (parent becomes non-callable; the branch
                                        is the only live continuation from k over
                                        the shared workspace, Decision 4/5)
  5. snapshot() the step-k state       (item 15 re-admission recipe)
  6. mint branch identity: new session_id + new WAL; standing approvals do
     NOT carry across (246 invariant 5); restore the snapshot into the branch
  7. write fork-begin / fork-complete WAL bracket naming parent, k, crossed set,
     and the parent freeze
```

## Decision 2: the rewind runs only host-confined inverses (scope-gated, not KIND-gated)

A fork's rewind must never itself cross the boundary. This is where a naive reuse
of `step_back(force=True)` is wrong, and it is the headline correctness point.
The first draft got the direction right and the axis wrong: it keyed the
non-emitting rewind on step KIND. KIND does not tell you whether an inverse
crosses the boundary.

Two things are true about the recorded timeline. First, a `KIND_COMPENSATION`
step's inverse is, by its own recorded note, "a second boundary crossing chosen
to offset the first" (backends/python/replay.py, `record_yield`): running it
SENDS something (a cancellation, a refund, an offsetting call). Second, and this
is the correction, a `KIND_EFFECT` witnessed inverse is NOT provably
non-emitting. The 243 contract (docs/design/243-witnessed-externs.md, Rule 3)
requires the DECLARED inverse extern to be classified non-emission and
non-witnessed. That is a check over the declaration; the inverse's body is an
opaque `@py`/`@ts` host body, and revl cannot see what it does at runtime. A
witnessed inverse can therefore be declared `pure` and still do outbound I/O in
its host body, and item 254 (witnessed NETWORK effects, a deferred overlap of
this item) declares its `undo` as "PUT the preimage back", an outbound crossing
that is nonetheless classified witnessed. Running such an inverse during the
rewind PUTs to a remote endpoint mid-fork, with no commit and no prompt: the
identical catastrophe as CRITICAL 1, only reached through a `KIND_EFFECT` step
the draft ran unconditionally.

So the rewind is gated on DATA the parser already records, not on KIND. The
capability scope of a witnessed extern is recorded at parse time as
`witnessed[caps]` (src/revl/parser.py: the declared token, not the extern name,
is the crossing's capability, and it joins the authority namespace). Slice 1
adds a rewind mode (a `compensate=False` flag on `Timeline.step_back`, off by
default so every existing caller and golden is byte-identical) whose rule is:

- **Run an inverse only if its declared capability scope is provably
  host-confined.** Concretely: a `witnessed[fs]` inverse whose reach is inside
  `REVL_FS_WORKSPACE`, an inverse gated by the item-373 `confined` reach clause,
  or an inverse inside the item-411 sandbox envelope. A `KIND_PROVISION`
  withdrawal is host-confined by construction (an in-process R5 inverse that
  crosses no boundary). These are the only inverses the rewind executes, and only
  these are counted as restored (`inversesRan` / `provisionsWithdrawn`).
- **Never run an inverse whose scope includes a network / IPC / outbound
  capability.** Enumerate it as a `wouldCrossOnRewind` residue item, surfaced
  exactly like a bare emission, and NOT counted in `inversesRan`. This covers the
  item-254 outbound witnessed `undo`, any author witnessed extern whose `undo`
  does outbound I/O, and every `KIND_COMPENSATION` step (a compensation is an
  outbound crossing by definition, so it is always enumerated, never fired).

Because `wouldCrossOnRewind` being non-empty forces `residue.clean=false`, the
fork can never report a clean rewind while an outbound-scoped inverse was skipped
or (worse) fired. The guarantee this decision makes is scoped precisely to
HOST-CONFINED inverses: the rewind restores host-confined witnessed state and
provisions, and enumerates every inverse that could cross. It makes NO claim that
243 guarantees runtime non-emission; 243 guarantees DECLARED non-emission over an
opaque body, which is a weaker and different thing, and the fork treats the
declaration's capability scope, not its classification word, as the source of
truth about crossing.

## Decision 3: the honesty model (the headline), a total partition over all seven kinds

The timeline has seven recorded kinds (backends/python/replay.py `KINDS`):
`KIND_EFFECT`, `KIND_PROVISION`, `KIND_EMISSION`, `KIND_COMPENSATION`,
`KIND_BOUNDARY`, `KIND_HINGE`, `KIND_OPAQUE`. The partition must account for
EVERY one, and for anything a future kind might add, or a tail step can fall
through the report unrestored and unenumerated while the fork claims step-k
state. The first draft partitioned into four sets and silently dropped
`KIND_OPAQUE`. The corrected partition walks every tail step (steps > k) into
exactly one bucket, and the fork report is a function of that total walk.

**Rewound (state truly restored, host-confined only).**
- `KIND_EFFECT` witnessed inverses WHOSE CAPABILITY SCOPE IS HOST-CONFINED
  (Decision 2): the fs preimage is restored via the 243/244 `undo(witness)`.
  Reported in `inversesRan`.
- `KIND_PROVISION`: the provision is withdrawn (R5 runtime-derived inverse,
  host-confined by construction). Reported in `provisionsWithdrawn`.
The claim "the workspace is actually in the step-k state" is scoped precisely to
this set: host-confined witnessed fs state and provisions. It is not a claim
about anything in the buckets below.

**Held, then dropped (never crossed, so free).**
- Class-(b) deferred sends still sitting in `SessionOwner._queue`. They never
  crossed the boundary, so there is nothing out in the world to enumerate as
  residue. Fork drops them exactly as `begin_abort` drops the queue, and reports
  them as `droppedDeferred` for lineage. This is the 245 asymmetry: the abort (or
  here, the fork-away) path for class (b) is exact by construction.

**Crossed, cannot be rewound (the residue that must be printed).**
- `crossed-bare`: uncompensated `KIND_EMISSION` steps in the tail. Each is a
  one-way boundary crossing with, by its own recorder note, "no inverse." A sent
  email is sent. Enumerated verbatim in `emissionsCrossed`, each with its recorded
  `{key, method, service, args, site}`.
- `crossed-compensated`: `KIND_EMISSION` steps in the tail that have a
  `compensation` recorded. Still crossed (the original send happened). Reported
  distinctly in `emissionsCompensated`, carrying the (unfired) compensation,
  because conflating them with the bare set would overstate the first ("we have an
  offset for that send") and understate the second. This is paper section 6.1:
  compensation is not inversion, and the report says so in those words.

**Would cross on rewind (an inverse we refuse to fire).**
- Any inverse whose recorded capability scope includes a network / IPC / outbound
  capability (Decision 2): an outbound-scoped `KIND_EFFECT` witnessed inverse
  (e.g. an item-254 witnessed NETWORK `undo`), and every `KIND_COMPENSATION` step
  in the tail. Enumerated in `wouldCrossOnRewind` with its recorded scope and
  site, NOT run, never counted in `inversesRan`.
- A `KIND_COMPENSATION` step in the tail whose referent emission lies BELOW k has
  no `emissionsCompensated` entry to attach to (its emission is not in the tail),
  so it is reported here in `wouldCrossOnRewind` as an orphaned compensation
  (carrying `for: <emission index <= k>`). It is neither an `emissionsCrossed`
  nor an `emissionsCompensated` item, and this bucket is where it lands, so it can
  never fall through the report.

**Unrestored (the recorder cannot restore it, and says so).**
- `KIND_OPAQUE`: a non-callable disposer with no undo (record_yield sets no
  `step.undo`, so `step_back` skips it). Its accumulated state is neither restored
  nor, in the draft, reported. It is enumerated here with its recorded `repr`.
- Any future kind not matched by a bucket above lands here by construction (the
  walk assigns the fall-through step to `unrestored`), so the partition stays
  total as the timeline grows.
Slice 1 REFUSES a fork whose tail contains a `KIND_OPAQUE` step: the recorder
cannot restore it and must not claim step-k state over it, so the fork is refused
up front (mirroring the non-idempotent-inverse refusal, Decision 5) rather than
shipping an `unrestored` item and a false "actually at step k". The `unrestored`
bucket therefore exists in the report shape for totality and for future kinds,
and Slice 1's only populated case (`KIND_OPAQUE`) is turned into a refusal.

**Provably empty (scaffolding, stated so it is not a silent gap).**
- `KIND_BOUNDARY` (A1 iteration boundary, `yield None`) and `KIND_HINGE`
  (`yield frame.drain` runtime scaffolding) carry no undo and cross no boundary;
  `step_back` skips them because `step.undo is None`. They contribute to no
  bucket and restore no state, and that is correct, not a gap. Stated explicitly
  so the partition's totality is auditable: every one of the seven kinds has a
  named disposition.

`residue.clean` is true only when `emissionsCrossed`, `emissionsCompensated`,
`wouldCrossOnRewind`, and `unrestored` are ALL empty. A fork across a span that
contains any crossed emission is not refused (that would make fork useless the
moment an agent has done anything irreversible); the crossed set and the
`wouldCrossOnRewind` set are what the confirm hash binds, so the primitive cannot
rewind past a send, or skip an outbound inverse, without the caller having been
handed, and having acknowledged, the exact list.

### Fork report shape

```
fragment
{
  "forked": true,
  "at": k,
  "atLabel": "<step k label>",
  "parent": "<parent session_id, now FROZEN>",
  "branch": "<new branch session_id>",

  "rewound": {                               // host-confined only (Decision 2)
    "inversesRan": [ {step, scope...}, ... ],       // KIND_EFFECT, scope host-confined
    "provisionsWithdrawn": [ {step...}, ... ]        // KIND_PROVISION
  },

  "droppedDeferred": [ {seq, receiver, method, args}, ... ],  // held, never crossed

  // residue that CANNOT be rewound, printed, never pretended away (6.1):
  "emissionsCrossed":     [ {index, key, method, service, args, site}, ... ],
  "emissionsCompensated": [ {index, key, method, service, args, site,
                             compensation: <step index, unfired>}, ... ],

  // inverses we refuse to fire because their scope crosses the boundary (Dec 2):
  "wouldCrossOnRewind":   [ {index, kind, scope, site,
                             for: <emission index if orphan compensation>}, ... ],

  // steps the recorder cannot restore (KIND_OPAQUE + any future kind);
  // Slice 1 refuses a fork whose tail has a KIND_OPAQUE step, so this is
  // present for totality and future kinds:
  "unrestored":           [ {index, kind, repr}, ... ],

  "residue": {
    "clean": false,  // true iff emissionsCrossed, emissionsCompensated,
                     // wouldCrossOnRewind, and unrestored are ALL empty
    "outstanding": [ <index>, ... ],
    "proof": "N emission(s) crossed the boundary between step k and head and
              cannot be undone; M inverse(s) whose scope crosses the boundary
              were enumerated but not fired; P step(s) could not be restored.
              All are listed above. The host-confined fs state and provisions
              above this line were restored to step k; nothing else is claimed."
  },
  "warning_emissions": "<N> emission(s) are still out in the world.",
  "guarantee": "<the timeline GUARANTEE string>"
}
```

When all four residue sets are empty (a fork over a span of only host-confined
witnessed effects, provisions, and held sends), `residue.clean` is true and the
fork is a provably exact rewind, the same way a class-(a)/(b)-only session aborts
to a provably clean world in 245.

## Decision 4: branch isolation, a frozen parent, and zero external residue until commit

A branch is a distinct session identity over the (single, Slice-1) workspace,
and in Slice 1 it is the ONLY live session over that workspace.

- **The parent is frozen at `fork_confirm` (the shared-workspace safety).** The
  rewind mutates the SHARED `REVL_FS_WORKSPACE` in place, and `step_back` leaves
  the component LIVE (backends/python/replay.py `Timeline.step_back`;
  src/revl/mcp/session.py `step_back` returns with the component still callable).
  If the parent stayed live, its timeline would still read head while the on-disk
  workspace sits at step k: the parent's next witnessed op would capture
  preimages against rewound content (silent corruption), and a parent
  abort/recover would replay against a workspace that no longer matches its
  recorded history. So `fork_confirm` RETIRES the parent: the parent session is
  marked frozen, becomes non-callable (any further op on it is refused with a
  "session forked at k, frozen" error), and the branch is the only continuation
  from k. This matches the serial model exactly and needs no new storage. The
  alternative, a copy-on-write workspace clone that would let the parent stay
  live over its own root, is the deferred CoW layer (Decision 8); until it lands,
  Slice 1 REFUSES any fork that would require the parent to remain live over the
  shared root. Slice 1 picks freeze-the-parent.
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
  branch (beyond the parent's own `fork-begin`/`fork-complete`/`fork-frozen`
  bracket, Decision 5). Each WAL recovers to its own last consistent point; the
  frozen parent recovers to step k (its history above k was rewound and it takes
  no further steps), and the branch recovers to its own fork point.
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
  crossed: [...], wouldCross: [...]}` to the PARENT WAL before the rewind touches
  the fs, and `fork-complete {branch}` plus `fork-frozen {parent}` after the
  branch snapshot is restored. The crossed set and the would-cross-on-rewind set
  are durable in the parent WAL, so the irreversible residue and the enumerated
  unfired inverses survive a crash and are not silently lost. `fork-frozen` marks
  the parent retired at step k so recover does not resurrect it as a live
  continuation.
- **Crash after fork-complete.** Parent and branch each have their own WAL.
  Recover on the branch WAL behaves exactly as 245: incomplete and no
  `commit-approved` rolls the branch back over the branch's own witnessed effects
  (the effects since the fork), reaching the branch's fork point, which is the
  step-k state. Recover does NOT try to reconstruct the branch topology or
  re-derive it from the parent; the `fork` records are informational lineage, not
  a replay script. Recover on the parent WAL sees `fork-frozen` and treats the
  parent as retired at step k, a terminal, non-live state; it does not re-admit
  the parent as a callable session, matching the freeze `fork_confirm` performed.
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
  not a teardown, and Decision 2 makes it strictly non-emitting by gating on
  recorded CAPABILITY SCOPE, not KIND: an inverse runs only when its declared
  scope is provably host-confined (a `witnessed[fs]` reach inside
  `REVL_FS_WORKSPACE`, an item-373 `confined` reach, an item-411 sandbox
  envelope, or an in-process provision withdrawal). Every inverse whose scope
  includes a network/IPC/outbound capability, which includes both compensations
  and any outbound-scoped witnessed `undo` such as item 254's, is enumerated in
  `wouldCrossOnRewind` and NOT fired. This does NOT rest on 243 guaranteeing
  runtime non-emission (it guarantees only DECLARED non-emission over an opaque
  host body); it rests on the recorded scope of the crossing. So no emission
  occurs during the rewind, which is stronger than G5 requires.
- **G7 (LIFO teardown order).** `step_back` already unwinds newest-first; the fork
  rewind runs the same inverses in the same LIFO order the teardown uses, so G7
  holds unchanged.
- **The witnessed-inverse contract (243/244).** For the host-confined inverses it
  runs, the fork rewind runs the exact `undo(witness)` the contract defines
  against the recorded preimage; it is the same replay `abort` and `recover` use.
  No new inverse machinery is introduced. What is new is the SELECTION over which
  inverses run: the fork consults the recorded capability scope and skips any
  crossing inverse, where `abort`/`recover` (a real teardown) may fire them.
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
`compensate=False` rewind mode enumerates compensations (unfired) instead of
running them. NOTE (2026-09-01 revision): this self-review framed the fix as
"treat compensations like emissions, run the rest," which the second adversarial
review showed is not sufficient, because a `KIND_EFFECT` witnessed inverse can
also cross (CRITICAL 2). The shipped fix keys the rewind on recorded capability
scope, not KIND, so compensations AND outbound-scoped witnessed inverses are both
enumerated; see the Revision section and the rewritten Decision 2. Resolved as
revised.

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
isolation in Slice 1; it refuses the operation that would need it. RELATED
(2026-09-01 revision, HIGH 1): the sibling hazard is the PARENT staying live over
the shared workspace after its history was rewound; Slice 1 now freezes the
parent at `fork_confirm` (Decision 4), so the shared workspace has exactly one
live owner, the branch.

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
and snapshot machinery. Slice 1 still lands alone and closes all four findings.

1. `Timeline.step_back` gains a `compensate=False` rewind mode (Decision 2), off
   by default so all existing callers and goldens are byte-identical. In this
   mode it consults each step's recorded CAPABILITY SCOPE: it runs an inverse only
   when the scope is provably host-confined (`witnessed[fs]` inside
   `REVL_FS_WORKSPACE`, an item-373 `confined` reach, an item-411 sandbox
   envelope, or a provision withdrawal), and enumerates every outbound-scoped
   inverse and every compensation into `wouldCrossOnRewind` without firing it.
2. `Session.fork(at=k)` enumerates: walk the whole tail into the total partition
   (Decision 3), build the fork report with `inversesRan` / `provisionsWithdrawn`
   / `emissionsCrossed` / `emissionsCompensated` / `wouldCrossOnRewind` /
   `unrestored`, and return it with a hash binding the rewound span, the
   would-cross set, and the live composition.
3. `Session.fork_confirm(hash)` performs (Decision 1 sequence): re-derive and
   check the hash, run the scope-gated rewind to k, drop the parent queue, FREEZE
   the parent session (non-callable, Decision 4), `snapshot()` the step-k state,
   mint the branch identity (new session_id, new WAL, no approval carry), restore
   the snapshot into the branch, write the `fork-begin` / `fork-complete` /
   `fork-frozen` bracket.
4. MCP verbs `revl_fork` / `revl_fork_confirm` wired in src/revl/mcp/server.py,
   documented alongside `revl_timeline` / `revl_step_back` / `revl_snapshot`.
5. Refusals: a fork before a `flushed` commit boundary (Decision 6); a fork whose
   rewound span has a non-idempotent-total inverse (Decision 5); a fork whose tail
   contains a `KIND_OPAQUE` step, which the recorder cannot restore (Decision 3);
   a second concurrent fork while a branch is live, and any fork that would need
   the parent to stay live over the shared root (Decision 4).
6. Recover reads the new `fork-begin`/`fork-complete`/`fork-frozen` records as
   lineage, treating a `fork-frozen` parent as retired at step k, not re-admitted
   live; the recovery verdict logic is otherwise unchanged (Decision 5).
7. Goldens (MEDIUM): pin the `compensate=False` fork report shape with a dedicated
   golden that exercises a host-confined rewind, a crossed emission, an
   outbound-scoped `wouldCrossOnRewind` inverse, and (as a refusal) a
   `KIND_OPAQUE` tail. Because `compensate=False` changes the loop terminal
   routing and adds report fields, run the PER-BACKEND golden suite (the
   replay/recovery goldens), not just `pytest tests/`, which does not exercise
   them, and confirm the default-mode goldens are byte-identical.

Slice 1 explicitly does NOT change any emitter or any tier but py; the fork lives
in the py MCP session and the py replay/runtime, exactly where the timeline,
owner, and recovery already live. No IR, no cross-tier golden, no language
grammar changes. It does add the new py-side report golden in item 7.

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
