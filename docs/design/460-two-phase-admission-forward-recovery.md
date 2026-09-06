# 460 (provisional): two-phase admission commit with forward recovery

**Provisional roadmap id.** This note is filed against GitHub issue
[#476](https://github.com/inso1337/revl/issues/476) ("Two-phase admission
commit with forward recovery"). The number 460 is a placeholder chosen so the
file sorts after the last numbered design note; the orchestrator assigns the
real item number at merge and renames this file. Every reference to "this
item" below means issue #476.

Design only: no compiler change, no `src/` change, nothing implemented.
Companion docs:
[443-estop.md](443-estop.md),
[245-session-commit.md](245-session-commit.md),
[246-auto-approve.md](246-auto-approve.md),
[426-composition-layers.md](426-composition-layers.md),
[teardown-contract.md](teardown-contract.md),
[../crash-recovery.md](../crash-recovery.md),
[../persistence.md](../persistence.md).

Source of record for the claims about today's code (line numbers as of
`8fcc41a5`): `src/revl/mcp/session.py` (`admit` at :2426, `_wire_turn` at
:2510, `_merged_turn_ir` at :2617, `_drain_pending_admits` at :2877,
`_commit_spends` at :3260, `_approval_wal` at :2975), `backends/python/replay.py`
(`WriteAheadLog.open` at :1841, `_write` at :1877), `src/revl/wal.py`
(`read_wal` at :194), `src/revl/recovery.py` (`_replay_tier` at :93,
`_roll_forward` at :395), `src/revl/mcp/persist.py` (`resume` at :342),
`backends/python/runtime.py` (`_estop_check` at the `plug` seam, :770),
`src/revl/estop.py` (`TIERS_WITH_ESTOP`).

## 1. The gap, stated against the code

Admission of a per-turn source is atomic today, and the atomicity is
in-process. `Session.admit` compiles the turn under the untrusted-author
profile and either returns a refusal verdict with the running composition
untouched, or hands the compiled `turn_doc` to `_wire_turn` (or queues it in
`_pending_admits` when the event loop is already driving a call).
`_wire_turn` then does, in this order:

1. builds the post-admission composition with `_merged_turn_ir` (fresh dicts,
   so building it mutates nothing);
2. builds the class map over that merged composition;
3. runs the lease gate over the turn's own acquisitions, the activation gate
   over the turn's activation bodies against the merged map, and the cache
   applicability fold;
4. plugs the turn's components into the live `driver.root`, settling any
   async activation body;
5. adopts the turn into `self.ir` and `driver.ir`;
6. installs the per-generation indexes in the same order `load` and `swap`
   install them: class map, distilled auto-approve rules, cache index, the
   owner's reach-closure candidate hashes.

A refusal in step 3 raises before step 4, so nothing is plugged and nothing is
adopted. That is the atomicity that item 246 F1 bought, and it is real. Its
scope is one process lifetime. Three things about that scope matter here.

**The decision is not durable.** The only durable trace an admission leaves is
indirect: the `approval-consumed` records `_commit_spends` writes for the
class-(c) tickets the turn's activation body consumed (consume-before-fire,
item 246 Decision 3), and whatever effect records the activation body's
crossings journal. Nothing on disk says "this turn was admitted against that
generation with these grants". After a crash between step 3 and step 6 the
restart sees spent approvals and, possibly, landed effects, with no record of
the decision that caused them. `persist.py:5` says it plainly for the base
generation ("an admitted generation does not survive a restart"), and a turn
survives even less, because the persisted snapshot describes the base sources
only.

**Application is not idempotent.** Step 4 runs the turn's activation body.
Re-running `_wire_turn` on the same `turn_doc` plugs a second set of fibers and
runs the body again. `_drain_pending_admits` already documents the hazard for
the in-process case: a ticket raised at wire time must leave the queue empty
"or the retry would double-wire the turn". Across a crash the same hazard
appears with no queue to empty: a restart that wants to honour the admission
has no way to re-establish the turn's provisions without re-running the body,
and the body's crossings may already have landed.

**The generation re-check is coarser than the surface.** `_generation` moves
on `load` (to 1), `swap`, `rollback` and `undo`. It does not move on
`_wire_turn`, and [426](426-composition-layers.md) §5.2 records that as the
intended behaviour: an `add` layer is "incremental, no generation change". But
`_wire_turn` does rebuild the class map, so the SURFACE a decision was taken
against can change while the generation number stays the same. A decision
recorded with `generation: 3` is not enough to know whether the class map it
was checked against is the one now live. And across a restart the number is
worthless anyway: `restore` re-admits through `load`, which sets the
generation back to 1.

Today none of this is load-bearing because the crash window is one
synchronous block and the runtime dies with the process. The issue names when
it becomes load-bearing: once the E-Stop reaches the non-py tiers (issue #122)
and the extern tiers (item 440) make a journalled crossing re-issuable, a
"decide, crash, restart" sequence has a runtime that can genuinely have
advanced past the decision. The E-Stop is built to look like a crash to
`revl recover` ([443-estop.md](443-estop.md), open question 3), so every halt
lands in the same window. What follows is what to write down, in what order,
so the restart can decide instead of guess.

## 2. The three stages

An admission passes through three durable stages. The names are the issue's.

| stage | written when | means |
|---|---|---|
| `decided` | after every pre-plug gate cleared and the spends were committed, BEFORE the plug | the gate said yes to this turn against this surface; authority was spent |
| `runtime_applied` | after the plug settled and the turn was adopted into `self.ir` | the runtime holds the turn; its activation body ran (or was cut) |
| `finalized` | after the per-generation indexes were installed | the surface the class map describes matches what is live |

Each stage is one WAL record on the session's single seq space
([245](245-session-commit.md), "one session, one WAL file, one counter"):
`admit-decided`, `admit-applied`, `admit-finalized`. They are written through
`WriteAheadLog._write`, which is the same append-flush-fsync discipline every
other record uses, so a record that returned was on disk. A fourth record,
`admit-abandoned`, closes a decision that will never reach `applied` (a plug
that raised, a latch at the plug seam). It is a terminal record, not a stage.

The ordering in `_wire_turn` becomes:

```
gates (lease, activation, cache)        refusal raises: nothing written
_commit_spends                          approval-consumed records, as today
write admit-decided                     stage 1
plug + settle                           failure: write admit-abandoned, dispose the turn's fibers
adopt into self.ir / driver.ir
write admit-applied                     stage 2
install class map, rules, cache index, candidate hashes
write admit-finalized                   stage 3
```

The refusal path is unchanged: a gate that refuses raises before `decided` is
written, so a refused admission still leaves nothing behind, and the
`_drain_pending_admits` contract ("nothing queued, nothing wired, nothing
fired") still holds because a wire-time ticket is raised inside the gates.

Why `decided` sits after the spends and not before. The spends are the
fail-closed half of consume-before-fire: a crash after `approval-consumed` and
before the fire leaves a consumed token and no effect, and the restart re-asks.
A `decided` record written BEFORE the spends would let a restart find a
decision whose authority was never spent and be tempted to honour it. Written
after, a `decided` record is a promise that its authority is already on the
ledger, and the restart can read the spend records it names.

### 2.1 The `CommitDecisionRecord`

The `admit-decided` record carries everything a fresh process needs to
re-derive the decision, and nothing it would have to trust:

```
{ "record": "admit-decided", "seq": N,
  "decisionId": "<sha256 of (turn source digest, granted, baseManifestHash, seq)>",
  "turn": { "sources": {...}, "granted": [...], "modules": {...} },
  "expected": { "generation": g, "surfaceEpoch": e,
                "baseManifestHash": "...", "classMapDigest": "..." },
  "spends": ["<requestId>", ...],
  "components": ["<turn component name>", ...],
  "keys": ["<provided key>", ...] }
```

`turn.sources` is the per-turn source, verbatim, in the same re-admittable
shape item 15's snapshot bundle uses for the base. It is the source, not the
compiled `turn_doc`, for the reason `restore` re-admits through the gate rather
than booting a persisted IR: a restart must re-run the checker and the
untrusted-author profile over the turn, so a turn the current checker now
refuses fails loudly instead of resuming on stale authority.

`expected` is the compare-and-swap target, and it has two halves because the
two places it is checked have different lifetimes (§3). `spends` names the
`approval-consumed` records this decision committed, so the audit join
`approval-granted` to `approval-consumed` to emission gains a `decided` hop and
a restart can see that the authority a decision claims was actually spent.

`admit-applied` and `admit-finalized` carry `decisionId` plus the
`{generation, surfaceEpoch}` observed when they were written. `admit-abandoned`
carries `decisionId` and a `reason` (`plug-failed`, `estop`, `stale`).

### 2.2 What is deliberately not in the record

Not the compiled IR (re-derived). Not the class map (re-derived; only its
digest is recorded). Not the result of any crossing (the effect journal owns
that, §4). Not an operator identity (the spend records already carry it).

## 3. Compare-and-swap on the expected surface

Every stage transition checks that the surface it is about to act on is the
one the decision was taken against. Two mechanisms, chosen by lifetime.

**In-process: `(generation, surfaceEpoch)`.** A new monotonic counter,
`_surface_epoch`, increments at every class-map install: `load`, `swap`,
`rollback`, `undo` and `_wire_turn`. It is the missing finer key: the
generation says which composition is live, the epoch says which SURFACE (class
map, auto-approve rules, cache index, candidate hashes) is live. `decided`
records the pair it was checked against; `applied` and `finalized` compare
before they write and refuse with `admit-abandoned {reason: "stale"}` on a
mismatch. In `_wire_turn` as it stands the three stages run in one synchronous
`_run` block with no `await` between the gates and the index install, so a
mismatch is unreachable today. The check is there for the places the block
will be split: the queued path through `_drain_pending_admits` (a swap could
land between `admit` and the drain), and the conductor's multi-process apply
once a child process acknowledges its own `applied` (§6).

**Across a restart: content digests.** The generation number and the epoch
die with the process (`load` sets the generation to 1 on restore). The record
therefore also carries `baseManifestHash`, the same manifest hash
`record_commit_approved` already writes, and `classMapDigest`, a digest of the
class map over the merged composition (the per-(key, realm) provider and class
assignments, sorted, hashed). Forward recovery recomputes both from the
restored base plus the recorded turn source and refuses to finalize when either
differs. The class-map digest is the load-bearing one: a base whose manifest
hash matches but whose policy file changed the class of a granted provider is
a surface the decision never saw, and a decision must not carry across it.

This is the issue's "re-check the class map against the CURRENT generation,
not the one recorded at decision time", made concrete. The map is never
trusted from the record; it is rebuilt and compared.

## 4. Idempotent runtime-apply

The runtime-apply step must be safe to run more than once for one
`decisionId`, in two situations with different mechanics.

**Same process, same decision.** The driver keeps an applied set keyed by
`decisionId`. `_wire_turn` consults it before the plug and returns the
recorded keys when the decision is already applied. This closes the
double-wire hazard the drain docstring describes without relying on the queue
being emptied first.

**Fresh process, decision already applied before the crash.** The process
memory is gone, so the turn's fibers and provisions must be re-materialized,
which means the activation body must run again. What must NOT happen is that
its crossings land again. The mechanism is the effect journal the WAL already
keeps, plus one field: every record a turn's activation body writes
(`effect`, `discharge-descriptor`, `deferred-emission`, `approval-emission`)
carries the `decisionId` of the admission it ran under. A re-apply under
recovery plugs the turn in journal-served mode: at each boundary-crossing seam
the runtime looks for the record with the same `(decisionId, ordinal)` and
decides by the crossing's tier, which is item 440's register as
`recovery.py:_replay_tier` already reads it off the descriptor:

| journal state for this crossing | `read` tier | `keyed` / declared-idempotent | fenced (no register) |
|---|---|---|---|
| record present, completed | re-dispatch (free) | re-dispatch (remote dedups) | serve the recorded outcome, do not dispatch |
| record present, not completed (in flight at the cut) | re-dispatch | re-dispatch | `admit-ambiguous`: refuse to finalize |
| no record | dispatch | dispatch | dispatch, and journal it |

The fenced row is the whole point. A fenced crossing is exactly the one whose
second attempt "cannot be proven safe" (item 309's wording), and the journal
is the only evidence a fresh process has that the first attempt ran. Serving
the recorded outcome requires that fenced crossings under a decision record
their outcome, which today's `effect` records do not carry; that is Slice 2's
schema addition (§7) and it is scoped to crossings that run under a
`decisionId`, so a composition that never admits writes byte-identical
records.

An in-flight fenced crossing at the cut is the same state the E-Stop produces
deliberately (`estop-ambiguous`, [443-estop.md](443-estop.md) open question
2), and it gets the same answer: at most one such record per decision, named
in the report, left to a human. Forward recovery does not finalize over it.

## 5. `recoverForwardCommit`

The new idea in the issue. `revl recover --wal FILE` today decides between
roll-forward (terminal marker present) and roll-back (absent). Admission adds
a scan over `admit-*` records that runs in both branches, because an
admission can be owed under either verdict: the session's activation may have
completed long before the turn was admitted.

For each `admit-decided` record with no `admit-finalized` and no
`admit-abandoned` behind it, classify by the evidence on the same WAL:

| evidence after `decided` | classification | action under `--forward` |
|---|---|---|
| nothing (no `applied`, no journal record with this `decisionId`) | `owed` | the runtime did not advance. Re-admit the recorded turn source through the gate against the restored base, which re-asks any ticket (the spends this decision committed are already consumed, fail-closed, exactly the mid-walk crash rule `_reserve_spend` documents). Report it; do not silently re-run. |
| journal records with this `decisionId`, or `admit-applied` | `advanced` | the runtime advanced past the decision. CAS on content (§3); on match, journal-served re-apply (§4), then finalize and write `admit-finalized`. |
| `advanced`, CAS mismatch | `stale` | write `admit-abandoned {reason: "stale"}`, report the digest that moved, leave the landed effects to the normal roll-back/roll-forward verdict. Never finalize a decision onto a surface it did not see. |
| `advanced`, a fenced crossing in flight | `ambiguous` | report the one record; refuse to finalize; the operator reconciles it exactly as an `estop-ambiguous` record. |
| turn placed on a tier outside `TIERS_WITH_ESTOP` (§6) | `owed` | report only; forward apply is refused for that turn until the tier can account for its own residue. |

Why finalize forward instead of leaving `advanced` ambiguous. The alternative,
treating every un-finalized decision as void, is the "silently dropped
admission" the issue forbids: the turn's effects landed, its approvals were
spent, and the restart would pretend none of it happened while the world says
otherwise. The evidence needed to finalize is already the evidence recovery
reads (the journal, by decision), and the two checks that make finalizing safe
(content CAS, the fenced-row refusal) are both refusals, so the forward path
can only finalize what it can prove.

Without `--forward`, `revl recover` reports the classification per decision
and changes nothing, matching `revl estop --report`.

The `recoverForwardCommit` name is the issue's; in this codebase it is
`recovery.recover_forward_admissions(wal, session, snapshot)`, called from
`_roll_forward` after `persist.resume` restores the base (so the CAS has a
restored generation to compare against) and from the roll-back branch after
the inverse replay (so a finalize never runs over a world an inverse is about
to change).

## 6. Dependency on E-Stop and the extern tiers

**Extern tiers (item 440, issue #119, landed `a3b4ae1c`).** The journal-served
table in §4 keys every row on the descriptor's `register`, and the re-dispatch
of a keyed owed emission is the item-309 slice-3 seam 440 landed. Without the
read tier every activation-body read would reach an operator as ambiguous on
every forward recovery, which would make the feature unusable rather than
unsound. This dependency is met.

**E-Stop (item 443; issue #122 open for the non-py tiers).** Three couplings,
all on the py tier today:

1. `_estop_check` at the `plug` seam (`runtime.py:770`) refuses a plug under an
   armed latch. With the stages in place that refusal lands between `decided`
   and `applied` and writes `admit-abandoned {reason: "estop"}`, so a halt
   during admission is a settled decision, not an owed one.
2. A latch that trips during the activation body leaves the halt's
   `estop-ambiguous` record, and §4's fenced row reads it as "present, not
   completed". The E-Stop and the two-phase commit share one ambiguity
   vocabulary, which is what the roadmap's 440/443 note asked for ("the two
   features are stronger together").
3. A halt after `applied` and before `finalized` strands the index install.
   The halt is shaped like a crash, so the §5 table finalizes it forward on
   the next `revl recover --forward`, under the same CAS.

For a turn whose components are placed on a tier outside
`revl.estop.TIERS_WITH_ESTOP` (today everything but `py`), the runtime that
"advanced" is a child process the conductor can only SIGKILL, and its residue
is UNKNOWN by construction (443, "Per-tier status"). Forward recovery has no
evidence to finalize on, so it classifies the decision `owed` and refuses
`--forward` for it. This is the gate the issue names: the multi-process half
of this design is done when a child on that tier honours the latch and
acknowledges `admit-applied` on the conductor's channel, which is issue #122's
remaining exit.

## 7. Slice plan, ordered, with exit tests

Each slice lands alone and leaves a composition that never admits
byte-identical on the WAL. Tests go in `tests/test_admit_two_phase.py` beside
`tests/test_admit_approval_gate.py`, which already carries the cordis fixture
and the class-(c) turn corpus.

**Slice 0: the surface epoch and the content digests.** `_surface_epoch` on
`Session`, incremented in every class-map install path; `classMapDigest` and
`baseManifestHash` exposed on `state()`; a `_cas_surface(expected)` helper
that raises on mismatch. No WAL change.
Exit: the epoch increments once per `load`, `swap`, `undo`, `rollback` and
`_wire_turn`, and not on `call`; the digest is stable across two builds of the
same class map and differs when a policy file changes a provider's class; the
helper refuses on either half moving.

**Slice 1: the decision record and the three stage records.** `_wire_turn`
writes `admit-decided` after `_commit_spends`, `admit-applied` after the plug
and adopt, `admit-finalized` after the index install, all through the
session's open WAL (`_approval_wal`). A plug that raises disposes the turn's
already-plugged fibers and writes `admit-abandoned {reason: "plug-failed"}`.
`read_wal` needs no change (its reader keeps unknown record kinds,
`wal.py:194`); `recovery.py` ignores the new kinds until Slice 3.
Exit: on the WAL the seqs satisfy `decided < every crossing the activation
body journalled < applied < finalized`; a refused turn writes nothing; a
wire-time ticket writes nothing and the retry after approval writes one
`decided`; a plug failure leaves no turn fiber in `driver.fibers`, no turn
component in `self.ir`, and one `abandoned`; a session with no `admit` call
produces a byte-identical WAL to today's.

**Slice 2: idempotent apply and the decision-tagged journal.** The driver's
applied set keyed by `decisionId`; every record an activation body writes
under a decision carries `decisionId` and an `ordinal`; fenced crossings under
a decision record their outcome on completion.
Exit: wiring the same `turn_doc` twice plugs once and returns the same keys;
the second `_drain_pending_admits` of a queued turn is a no-op; a fenced
crossing's record gains an outcome exactly once; records outside any decision
are byte-identical to today's.

**Slice 3: `recover_forward_admissions`.** The §5 classification and the
`--forward` apply on the py tier, called from both recovery branches;
journal-served plug mode in the py runtime per the §4 table; `revl recover`
prints one line per un-finalized decision.
Exit: a crash matrix, parametrized over cut point (after `decided`, after
`applied`, after `finalized`) and crossing tier (`read`, `keyed`, fenced):
after `decided` the decision reports `owed` and the retry re-asks the ticket;
after `applied` a `read` and a `keyed` crossing re-dispatch and the keyed one
reaches the remote's dedup exactly once, a completed fenced crossing is
served from the journal and dispatches zero times, an in-flight fenced
crossing reports `ambiguous` and the WAL gains no `finalized`; after
`finalized` the scan is a no-op; a base restored from a snapshot whose policy
reclassified a granted provider reports `stale` and writes `abandoned`. The
non-vacuity check: with the journal-served seam disabled the fenced-completed
case dispatches twice and the test fails on it.

**Slice 4: E-Stop coupling on py.** The plug-seam refusal writes
`abandoned {reason: "estop"}`; an `estop-ambiguous` record under a decision is
read as the §4 in-flight row; the conductor's halt report lists un-finalized
decisions by `decisionId` beside its stranded entries.
Exit: with `tests/test_estop_443.py`'s latch fixture, a latch armed before the
plug yields `abandoned` and no owed decision on the next recover; a latch
tripped inside the activation body yields exactly one `ambiguous` and no
`finalized`; `revl estop --report` and `revl recover` name the same
`decisionId`.

**Slice 5 (gated on issue #122): the non-py tiers.** A child on a tier that
honours the latch acknowledges `admit-applied` on the conductor channel; the
conductor writes the stage record on the ack; forward recovery for that tier
becomes `advanced` instead of `owed`.
Exit: a placement with the turn's component on that tier, cut after the
child's ack, finalizes forward; the same cut on a tier still outside
`TIERS_WITH_ESTOP` reports `owed` and `--forward` refuses it.

**Self-host check (item 429).** None of this touches the compiler: the change
is in `session.py`, the py runtime seams, `recovery.py` and the WAL schema.
No `selfhost/` port is owed, and no byte-agreement oracle covers it.

## 8. Posture: what this provides, and what it does not

Provides, once Slices 0 to 3 land on the py tier:

- **No dropped admission.** Every decision the gate took is on the WAL before
  the runtime is touched, and the restart names each one it cannot finalize.
- **No double-run of a fenced extern.** A fenced crossing that completed under
  a decision is served from the journal on re-apply; one that was in flight
  is refused and reported, never re-dispatched.
- **The generation is re-checked, by content.** A decision finalizes only onto
  a base whose manifest and class map digest match what it was checked
  against. In-process, the surface epoch closes the gap the generation number
  leaves open for `add`-only layers.
- **One ambiguity vocabulary with the E-Stop.** An operator halt during
  admission produces the same records and the same reconciliation path as a
  crash.

Does not provide, and says so:

- **Knowledge of an in-flight fenced outcome.** The at-most-one ambiguous
  record per decision is still a human's to settle. This design bounds and
  names that record; it does not resolve it.
- **A distributed transaction.** For a turn placed across processes, `applied`
  is the conductor's view of a child's ack. Until issue #122 lands the latch on
  the other tiers there is no ack, and forward recovery refuses those turns.
  Nothing here makes a SIGKILLed child's residue known.
- **Atomicity of the plug itself.** A crash in the middle of plugging a
  multi-component turn leaves the WAL at `decided` with some journal records,
  which §5 classifies `advanced`; the re-apply re-plugs every component and
  the journal decides per crossing. The turn is not applied "all or nothing"
  at the fiber level; it is applied idempotently per crossing.
- **Approval re-use.** A decision classified `owed` has already spent its
  approvals. The retry re-asks. Reusing a spent token on the strength of a
  `decided` record would be the laundering consume-before-fire exists to
  prevent, and this design keeps that rule.
- **Anything for the WAL-less session.** A session without recording has no
  WAL to write stages to; it keeps today's in-process atomicity and nothing
  more. A policy load already refuses without recording (item 246 Decision 2),
  so the sessions that carry class-(c) authority are exactly the ones that
  get the stages.

## 9. Open questions, left deliberately

1. **Should `admit-decided` be written before the spends, with the spends
   naming it?** §2 chose after, so a decision on disk always has its authority
   behind it. The other order would give the audit a cleaner "decision, then
   spends" join at the cost of a restart finding decisions with no spends. The
   fail-closed choice is kept until the audit surface asks for the other.
2. **Does an `owed` decision deserve an automatic retry?** §5 says report and
   re-admit under `--forward`, re-asking tickets. An unattended restart
   (item 33's policy knob) could want the retry automatic when no ticket is
   owed. Deferred to whoever wires the knob for the item-309 slice-3 seam,
   since it is the same knob.
3. **Journal-served outcomes and `Secret[T]` args.** A fenced crossing whose
   recorded args were redacted (item 256 Slice 3) cannot be re-issued, which
   recovery already refuses; serving its recorded OUTCOME may still be fine,
   since the outcome is what the body returned, not the redacted argument.
   Slice 2 should decide whether an outcome can be recorded next to a
   redacted arg, and refuse rather than guess if the taint engine says no.
