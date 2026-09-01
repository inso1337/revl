# Design: witness retention, the undo horizon (item 273)

Status: design proposed. The default horizon, the declared-shorter-horizon
surface, the drop accounting, and the recovery bound are decided here;
implementation builds on the landed 243/244/245 foundation (the
`transactional` entry kind and its commit-time discharge + witness GC, the
session owner with its escrow and hash-bound manifest, the WAL discharge
machinery, the fs witness store). Item 246 (auto-approve policy) consumes the
retention grade this design exports and is not specified here.

## The one thing to get right

Dropping a witness is a class change. A witnessed call was admitted, and
possibly auto-approved, on the strength of a registered inverse (class (a),
docs/design/245-session-commit.md Decision 2); the moment its witness is
gone, that inverse can no longer run, and the effect is exactly as
irreversible as a bare emission. 245 made the class a type judgment: nothing
at runtime may move a call between classes. Retention does not get an
exemption from that rule; it gets the one honest reading of it. The CLASS
never changes: the call was class (a) when it was checked, and the approval
it earned was honest on the evidence that held then. What a drop changes is
whether the standing PROOF still holds, and the system already has a shape
for that event: a failed `restore` surfaces a prompt and becomes
restore-residue rather than silently degrading the auto-approval
(docs/design/243-witnessed-externs.md rule 6, docs/design/246-auto-approve.md
Decision 1, "a failed inverse escalates after the fact"). A retention drop is
the same event made deliberate instead of accidental, so it clears a higher
bar: it is declared twice (by the extern and by the session), it is durable
in the WAL before the bytes are reclaimed, and it is visible on every proof
surface that would otherwise still claim revertibility.

The rule in one line, from the roadmap: a witness may be dropped, but the
proof surface must say so before anyone relies on it. The same honesty
contract as docs/replay.md section 4: state what cannot be promised, in the
artifact itself, ahead of the reliance.

## What already exists (the landed foundation)

- **Commit-time discharge + witness GC.** `_Transactional.__call__`
  (backends/python/runtime.py): on a committed frame the inverse is skipped
  and both the inverse and witness references are dropped; on an abort the
  inverse replays against the captured witness and the references are
  dropped after. Either way a settled entry holds no rollback state. This is
  the in-memory half of retention, landed with 243/245; item 273 owns the
  policy surface over the durable half.
- **The session as the WAL lifetime.** One driver lifetime, one WAL file,
  one seq counter (245, "The WAL seq space across the session"). The session
  ends in exactly one of COMMIT or ABORT; a crash is an abort whose replay
  runs in `revl recover`.
- **The discharge escrow.** A mid-session withdrawal (a swap, an item-65
  generation undo) does not settle its transactional entries; they are held
  by the `SessionOwner` escrow until the session verdict
  (backends/python/runtime.py, `Frame.drain`'s holding branch,
  `SessionOwner.escrow`). The escrowed witnesses are live witnesses.
- **Witnesses are WAL-serializable data** (243 rule 4, item 322). Every
  registration writes a `discharge-descriptor` record carrying the named
  inverse call and the witness record; `revl recover` reconstructs and
  replays inverses from the WAL alone, on all six tiers. The descriptor is
  metadata; for the fs ops the witness also names PAYLOAD on disk (below).
- **The fs witness store** (item 244, stdlib/fs.rvl,
  backends/python/revl_fs_workspace.py). `write` snapshots the preimage into
  `<root>/.revl-fs-preimage/pre-<uuid>` (APFS clonefile CoW, copy fallback);
  `rm` parks the removed file at `<root>/.revl-fs-garbage/rm-<uuid>`;
  `move`/`mkdir` witnesses are pure metadata. Both sidecar dirs live inside
  the confined workspace root.
- **The commit manifest and its hash-bound two-step.**
  `SessionOwner.manifest()` / `approve(hash)`: what the human approved is
  exactly what fires; any drift in the gate target recomputes the hash and
  refuses the stale confirm (245 Decision 4).
- **What nothing does yet.** Discharge drops REFERENCES. No landed code
  deletes the payload bytes: a committed session leaves every preimage
  snapshot and every parked rm target in the store, and no surface says when
  they may go. That is the storage bill this item bounds, and the reason the
  bound must be a policy with accounting rather than a cleanup cron.

## Decision 1: the default horizon is the session

**A witness lives from its Ok-conditional registration to the session
verdict, and no shorter, unless a shorter horizon is declared (Decision 2).**
Concretely, per verdict:

- **COMMIT.** The mutation persists. `commit-approved` is written, the
  deferral queue flushes, the frames unload committed, and
  `SessionOwner.finalize_commit` writes the one consolidated discharge
  record over every transactional seq, escrow included. At that point every
  witness of this session is discharged: the references are GC'd (landed)
  and the PAYLOAD sidecars are dead storage. Reclaiming them is the
  commit's own business, not a retention policy's: 245 already frames
  "actually emptying the garbage directory" as the class-(b) deferrable
  tail of the reversible rm pattern, enumerated on the commit prompt and
  fired at flush. The purge is crash-safe anywhere after `commit-approved`
  is durable, because from that record on, recovery replays no inverse from
  this session (the approved-to-discharged window rule, 245 Decision 3);
  ordering it with the flush inherits that safety and puts the destruction
  under the approval that covers it.
- **ABORT.** Every inverse replays (live frames per the teardown contract,
  then the escrow reverse-seq). The fs inverses CONSUME their payloads by
  construction: `restore` and `unrm` are `os.replace` of the sidecar back
  over the target (stdlib/fs.rvl), so a completed abort empties the store
  of everything it reverted. What remains is residue, already enumerated by
  the abort report.
- **CRASH.** The witnesses of an unfinished session are owed to
  `revl recover`, which replays their inverses from the WAL descriptors.
  The horizon therefore extends past the process lifetime to the completion
  of recovery: a crashed session's store is recovery's input, not garbage
  (Decision 4).

This default needs no declaration, no budget, and no new mechanism; it is
the landed 243/245 semantics stated as a retention contract. Its cost is
bounded by the session: the store grows with the session's witnessed
mutations and returns to empty (modulo residue) at the verdict. A session
whose witnessed writes are too large for that bound is what Decision 2
exists for.

## The witness store, physically (what a horizon bounds)

A witness occupies up to three places, and a retention policy may touch
exactly one of them:

1. **The in-memory entry** (`_Transactional.witness`). Bounded by the
   session already; GC'd at settle. Not retention's concern.
2. **The WAL descriptor** (`discharge-descriptor` records). Append-only for
   the session lifetime under the one-file-one-counter rule (245); grows
   with call count, not with file sizes. NEVER prunable by a retention
   policy: the WAL is the recovery input and the proof spine, and a policy
   that could rewrite it could lie. Retention does not touch the WAL except
   to append (Decision 3).
3. **The payload sidecars**: `.revl-fs-preimage/pre-*` and
   `.revl-fs-garbage/rm-*` inside the workspace root. This is where the
   bytes are, and this is the only thing a horizon bounds. `move` and
   `mkdir` witnesses carry no payload; they cost nothing to retain and a
   sweep never selects them.

The budget meter is the logical size (`st_size`) of the payload sidecars. On
APFS the clonefile snapshot shares blocks with the original until the
overwrite diverges them, so the meter is an upper bound on physical usage;
the budget is a bound on revertibility payload, not a disk quota, and the
doc for the flag says so.

## Decision 2: a shorter horizon takes two declarations

A drop needs two keys, held by the two parties that each own half the
consequence. Neither key alone drops anything.

**Key 1: the extern declares eligibility.** A witnessed extern whose
witnesses may be dropped early carries the `droppable` modifier in the
classification slot, the same modifier position `deferred` occupies on an
emission (245 Decision 2):

    extern witnessed[fs] droppable fn write_scratch(path: Str, contents: Str)
        -> Result[WriteWitness, FsError]
        undo restore(result)

Declaration-owned for the reason 243 made the inverse declaration-owned and
245 made deferral declaration-owned: droppability changes abort semantics.
A program whose abort proof can lose entries under storage pressure means
something different from one whose abort proof cannot, and if the harness
could toggle that at runtime, the same program would mean two things under
two harnesses and the checker could not export the grade. `droppable` is a
contextual keyword, recognized only in that slot, for the same self-hosted
lexer parity reason `witnessed` was (243, Slice 1 refinement 2).

What the checker verifies and exports:

- `droppable` is refused anywhere but a `witnessed` extern's classification
  slot. A `pure`/`acquire`/`emission` extern retains no witness, so there is
  nothing to drop; the refusal names that.
- The witnessed IR descriptor gains `droppable: true`, and the emitted
  registration passes it to the runtime entry, so the sweep can tell an
  eligible entry from a protected one without re-deriving anything.
- The crossing surface carries the retention grade: the externs scope facts
  (src/revl/query.py) and the G8 crossing aggregation
  (src/revl/erase_report.py) tag witnessed crossings
  `retention: "session" | "droppable"`, next to the `actionClass` tag 246
  reads. 246's policy thereby knows the grade at admission time, before any
  drop happens: what posture a bounded-revertible call deserves is 246's
  decision, made on a checked tag, not a runtime surprise.
- Nothing else changes. The declared undo, the Ok-conditional registration,
  and the inverse classification checks (243 rules 1-6) apply unchanged; a
  droppable witness that is never dropped behaves identically to a session
  one.

stdlib/fs.rvl declares nothing `droppable` in v1. The safe default is the
default everywhere; a program that wants bounded scratch semantics declares
its own witnessed extern for the scratch path (the surface above). Whether a
`use`-site rebind should exist so a consumer can weaken a stdlib extern's
retention is deliberately an open question, not v1 surface.

**Key 2: the session declares the budget.** The driver opens the session
with a witness-store budget: `revl run --witness-budget <bytes>`, and the
MCP session config equivalently (`retention: {"budgetBytes": N}` at
`revl_load`). The budget is held by the `SessionOwner` (the owner of every
other session-scoped commit structure: queue, escrow, registry), is
immutable for the session lifetime, and its absence means the Decision 1
default with zero drops, byte-identical to today. A budget on a session
whose composition reaches no `droppable` extern is legal and inert until
pressure (see the refusal rule below); the operator declared a bound, and
the bound is enforced by refusing new payload, never by dropping old proof.

**The pressure protocol.** When admitting a new payload sidecar would take
the store's meter over the budget:

1. **Sweep**: the owner drops eligible witnesses, oldest seq first, until
   the new payload fits or no eligible witness remains. Eligible means: the
   entry's extern is `droppable`, the entry is unsettled (not discharged,
   not replayed), the session verdict is unclaimed, and the entry is not
   pinned (Decision 5). Escrowed entries are eligible on the same terms:
   they are typically the oldest storage, and dropping one is precisely how
   the undo horizon recedes (the marker in Decision 3 is how that stays
   visible). Every drop runs the full Decision 3 accounting.
2. **Refuse**: if the store is still over budget, the incoming op fails
   with `Err({code: "EBUDGET", ...})` on the fallible surface every
   witnessed fs op already has (243, "Fallible"). The mutation does not
   happen, so nothing is owed: an Err registers nothing. Refusing the new
   effect rather than dropping a protected old witness is the invariant
   that makes the default trustworthy: under pressure, a session-retention
   witness is never the thing that gives.

Oldest-first is not an optimization order; it is the semantics of a horizon.
Drops consume the far end of the timeline, so revertibility is always a
contiguous suffix of the session: "everything after seq N reverts" stays a
sentence the audit can say. A policy free to drop from the middle would
leave a lace of holes no marker could summarize.

## Decision 3: the drop accounting (the class change, never quiet)

A drop is one event with four obligations, in this order:

1. **Durable first.** The owner appends
   `{"record": "witness-dropped", "seq": N, "reason": "budget",
   "policy": {"budgetBytes": ...}, "call": {...}}` to the session WAL,
   fsync'd by the existing `WriteAheadLog._write` discipline, BEFORE any
   payload byte is reclaimed. Write-ahead honesty, the same direction 245
   argues for the flush: the dangerous misordering is reclaim-then-crash,
   which would leave a WAL whose descriptor promises an inverse whose
   payload is gone, a silent downgrade manufactured by a crash. Logging the
   drop first means the worst crash outcome is a logged drop whose payload
   survived, which recovery treats as dropped anyway (the record is the
   truth; a surviving sidecar is unreferenced bytes).
2. **Reclaim.** The payload sidecars named by the witness are deleted; the
   meter decreases. The WAL descriptor stays (it is the record that the
   mutation happened and what its inverse would have been); only payload
   goes.
3. **Mark the entry.** The `_Transactional` entry enters a third settled
   state, `dropped` (alongside `discharged` and `replayed`). On a later
   commit it discharges like any entry (the mutation was going to persist
   anyway; a drop costs a committed session nothing). On a later abort it
   does NOT attempt the inverse: it contributes a residue record,
   kind `dropped-by-policy`, naming the origin call, the witness-dropped
   seq, and the policy, into the merged residue envelope. The abort report
   for a session with drops is RESIDUE, never CLEAN: "n mutation(s)
   permanent: witness dropped by declared retention policy, listed". The
   `dropped-by-policy` kind is an addition to the closed residue schema and
   therefore an amendment to docs/design/teardown-contract.md, the same
   path `flush-residue` took (245 Decision 3).
4. **Move the marker.** The session state and the commit manifest gain the
   undo horizon:

       "undoHorizon": {
           "oldestRevertibleSeq": 17,      # null when nothing is live
           "dropped": [
               {"seq": 12, "group": "fs.write_scratch", "reason": "budget"}
           ]
       }

   This is the audit's answer to "how far back can this composition
   actually revert, right now?": everything at or after
   `oldestRevertibleSeq` reverts; the `dropped` list is what lies beyond,
   enumerated. It appears in `session.state()`, in the manifest, and in the
   abort report, so the answer exists BEFORE anyone relies on it, which is
   the whole contract.

**The manifest hash covers drops.** The dropped set joins the gate target
(`SessionOwner._target()`), so a drop that lands between `revl_commit`'s
enumeration and `revl_commit_confirm` drifts the hash and the confirm
refuses with a fresh manifest (245 Decision 4 machinery, unchanged). The
human who approved a commit with a clean horizon never silently commits
over a receded one.

**Feeding 246.** The drop event increments a `dropped` counter beside the
`prompts` counters and is visible in the manifest as above. Whether a drop
raises an interactive prompt at drop time, and what posture a
`retention: "droppable"` call deserves at approval time, are 246's policy
decisions over 246's decision table; this design's obligation ends at
making both signals checked, durable, and impossible to miss. The
re-classification is exactly the "proof stopped holding" escalation row 246
already has for a failed restore, with the difference that a drop is
declared in advance on surfaces 246 can read at admission.

## Decision 4: crash recovery bounds the minimum horizon (item 322)

A retention policy operates strictly inside the space recovery does not
need. Three rules:

1. **The WAL is out of bounds.** No retention policy prunes, rewrites, or
   truncates WAL records, ever. Retention appends `witness-dropped` and
   touches nothing else. The seq-space rules of 245 already make the WAL
   append-only for the session; this restates it against the one new actor
   that might be tempted.
2. **No drop after the verdict is claimed.** Once `begin_abort` has run
   (verdict = abort), every unsettled witness is owed to the replay; once
   the session has crashed, every unsettled witness is owed to
   `revl recover`. A sweep attempt in either state is refused outright,
   not accounted: there is no budget pressure that justifies racing the
   replay that is the witness's whole purpose. Concretely: the owner's
   sweep runs only while `_verdict is None`, and the store of a session
   whose WAL lacks its completion marker (`activation-complete`, or
   `aborted` after a completed in-process abort) is recovery-owned; any
   janitor over workspace stores must treat an unfinished WAL as a lock
   and run `revl recover` first. This is the "recovery-reachability bounds
   the minimum horizon" rule: the set of sidecars named by undischarged,
   undropped descriptors in an unfinished WAL is exactly what recovery
   will ask for, and none of it is droppable by anyone but recovery
   itself.
3. **Recover reads drops honestly.** `revl recover` gains a fourth lane
   beside ran / moot / unreconstructible (src/revl/recovery.py,
   docs/crash-recovery.md section 5): **dropped**. A descriptor whose seq
   is named by a `witness-dropped` record is not replayed (the payload is
   gone by declaration) and is not `unreconstructible` (the descriptor
   reconstructs fine; the data was retired on purpose): it is reported as
   residue with the policy named, "dropped by declared retention policy
   before the crash; mutation permanent". The verdict for a rolled-back
   session with drops is RESIDUE with exit 1, same as any other honest
   residue. A committed session's drops (`commit-approved` present) cost
   recovery nothing: discharged and dropped alike are skipped, per the
   window rule.

The two directions compose into the full lifecycle bound: the horizon of a
witness is AT LEAST max(declared horizon, verdict + completed replay), and
the declared horizon may shorten only the left edge of that, only
mid-session, only with the accounting.

## Decision 5: extensions and pins (items 65 and 250)

The horizon can be extended as well as shortened, and both existing
extenders reduce to one primitive: a **pin**. A pinned witness is excluded
from sweep eligibility regardless of `droppable`; pins are owner-held,
named by seq, and released by the thing that took them.

- **Generation undo (65).** `revl undo --to <gen>` needs the escrowed
  inverses of every generation being unwound. The undo path takes a pin on
  the escrowed entries it will replay before it starts, so a sweep cannot
  race it. Conversely, an undo whose target lies BEYOND the horizon
  (a required witness is already dropped) is refused up front, quoting the
  `undoHorizon` marker: "cannot revert to generation N-3: 2 witnesses
  dropped beyond the horizon, listed". A partial undo that silently skips
  a dropped witness would be the quiet downgrade again, one level up; the
  refusal keeps the marker the single source of truth for reachability.
- **Session branching (250, future).** A fork at step k replays inverses
  back to k, which requires every witness from k forward. When 250 lands,
  its branch-point declaration is a pin from seq k to the session head. A
  budget too small to hold a requested branch point surfaces at
  pin-taking time ("pinning gen k needs 1.2GB retained, budget is 512MB"),
  not at fork time when it is too late. Nothing in this design blocks on
  250; the pin primitive is 250-shaped so the composition is ready.

## Tier status

Retention policy is a session-owner feature, so it is py-tier v1, like the
deferral queue: the owner holds the budget, the sweep, the pins, and the
marker. Unlike `deferred`, `droppable` needs NO tier gate on the five
ownerless tiers (245 Decision 2's gate exists because both available
degradations of an unowned deferred emission lie). An unowned `droppable`
witness degrades SAFE: with no owner there is no budget, no sweep, and no
drop, so the extern behaves exactly as session-retained and only the
storage bill stands. The modifier emits everywhere, is inert off-py, and
the crossing tags still carry the grade for audit parity.

## Exit tests

1. **The default is untouched.** No budget declared: byte-identical
   behavior, the whole existing suite plus the per-backend goldens stay
   green; commit discharges and purges under the approval, abort reverts
   residue-free, sidecar dirs return to empty.
2. **An accounted drop inside a declared horizon is fine.** Session with a
   budget; a `droppable` witnessed write large enough to force a sweep of
   an older `droppable` witness. Assert the order: the `witness-dropped`
   record is durable in the WAL strictly before the sidecar is unlinked;
   the meter decreases; the new op succeeds; `session.state()` and the
   manifest show the receded `undoHorizon` with the dropped seq listed.
3. **The expired call is re-classified visibly, never silently
   reversible.** After the drop of test 2, abort the session. The dropped
   entry's inverse is never invoked (spy on the undo); the abort report is
   RESIDUE carrying a `dropped-by-policy` record naming the origin call
   and policy; it never claims clean. Companion: drop between enumeration
   and confirm, assert the stale-hash refusal.
4. **A protected witness never gives.** Budget pressure with only
   session-retention (non-`droppable`) witnesses live: the incoming op
   returns `Err EBUDGET`, zero drops happen, every existing sidecar is
   intact, and a subsequent abort reverts fully clean.
5. **A retention policy that would drop a recovery-needed witness is
   refused.** Three shapes: (i) sweep attempted after `begin_abort` is
   refused and the abort replays everything; (ii) `kill -9` mid-session,
   then a janitor sweep over the dead store is refused while the WAL lacks
   its completion marker, then `revl recover` replays clean; (iii)
   `revl recover` over a WAL that carries a `witness-dropped` record
   reports that descriptor in the `dropped` lane as residue with exit 1,
   and replays every other descriptor normally.
6. **The declaration is checked.** `droppable` on a `pure`/`acquire`/
   `emission` extern is refused with the nothing-to-drop diagnostic;
   `droppable` on a witnessed extern admits, and the scope facts plus the
   G8 crossing surface carry `retention: "droppable"` (with
   `retention: "session"` on an unmarked witnessed extern).
7. **Pins hold.** A `revl undo` in flight pins its escrowed witnesses:
   a concurrent budget sweep selects past them; an undo targeting a
   generation beyond the horizon is refused quoting the marker, and the
   workspace is untouched by the refused undo.

## Open questions (left deliberately)

1. **A `use`-site retention rebind.** Whether a consumer may weaken a
   stdlib witnessed extern to `droppable` at import, instead of declaring
   a sibling extern. Wants real demand first; the sibling-extern path
   costs one declaration and keeps the grade next to the effect.
2. **Sweep triggers beyond bytes.** The machinery generalizes to a
   count-based or generation-count horizon (drop witnesses older than k
   generations) with the same accounting; v1 ships bytes because storage
   is the bill the item names. A time-based horizon is deliberately
   excluded: wall-clock expiry inside a session would make program
   meaning depend on scheduler latency.
3. **A cross-session store janitor.** Completed sessions can strand
   residue payloads (an unreconstructible inverse's sidecar survives
   recovery by design). A `revl gc` over workspace stores that respects
   rule 2 of Decision 4 (unfinished WAL = lock) is future work; nothing
   in-session owes it.
4. **Whether a drop prompts at drop time.** The event is durable, counted,
   and manifest-visible here; 246 decides if any retention grade or drop
   event warrants an interactive prompt, on its own decision table.
