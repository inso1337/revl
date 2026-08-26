# Design: deferred irreversibility, the session commit protocol (item 245)

Status: design proposed. The commit boundary, the three-class derivation, the
deferral queue, the commit-prompt enumeration, and the abort path are decided
here; implementation builds on the landed 243/247 py foundation (the
`transactional`/`compensation` entry kinds, `Frame.drain`, the WAL
discharge-descriptor and discharge records, `revl recover`'s two-phase
replay). Item 246 (auto-approve policy) consumes this design's commit manifest
and is deliberately not specified here.

## The one thing to get right

The commit already exists; it is just in the wrong place. The teardown
contract says so explicitly (docs/design/teardown-contract.md, "Commit path"):

    Until item 245 lands the explicit commit UX, a clean successful unload IS
    the commit (implicit).

Item 245 is the act of moving that commit from unload time to an explicit
SESSION commit point, and adding the one thing the accumulator cannot
represent: an irreversible action that has not happened yet. Agent actions
split three ways:

- **(a) witnessed-revertible.** Executes freely mid-session. Already landed:
  the `transactional` entry kind (243), abort-only replay, commit-time
  discharge plus witness GC.
- **(b) deferrable.** The irreversible tail of a reversible pattern: actually
  emptying the garbage directory 244's `rm` renamed into, actually sending a
  queued outbound message. The action is postponed; nothing crosses the
  boundary until the session commit fires it.
- **(c) immediately-irreversible.** An emission whose response the task needs
  mid-session (an API call that mutates and answers). It fires when called;
  246 decides the per-call prompt. `compensate` (247) lives here.

Class (b) is the new primitive and it is NOT a fourth accumulator entry kind.
Every accumulator entry describes how to react to something that already
happened (release it, roll it back, offset it). A deferred emission has not
happened. On commit it FLUSHES (the real action fires, once, after the one
prompt); on abort it is DROPPED, and dropping is free: nothing fired, so there
is nothing to invert (243) and nothing to offset (247). That asymmetry is the
whole value: for class (b) the abort path is exact by construction, not by
engineering a witness and not by best effort.

## What already exists (the landed foundation this builds on)

- **The commit/discharge split, per activation.** `Frame._committed` flips at
  `Frame.drain` entry; a `_Transactional` disposer replays `undo(witness)` iff
  not committed, otherwise discharges and GCs the witness
  (backends/python/runtime.py, `Frame`, `_Transactional`, `drain`).
- **The WAL discharge machinery.** `Frame.transactional` /
  `Frame.compensation` write a `discharge-descriptor` (a named call:
  `call.receiver/method/args`, serializable, never a closure) at registration;
  `Frame.drain` writes `{"record": "discharge", "discharged": [seq...]}` at
  commit (backends/python/replay.py, `record_discharge_descriptor`,
  `record_discharge`).
- **Crash recovery over that WAL.** `revl recover` rolls back an incomplete
  activation in two phases, SKIPS discharged seqs (a committed transaction is
  never rolled back), routes compensations through the dedicated
  `apply_compensation` record-only path, and states the merged residue proof
  (src/revl/recovery.py, `_roll_back`, `_residue_proof`).
- **The session surfaces.** `revl run` owns the driver, opens one WAL per run
  (`--wal` implies `--record`), and writes `activation-complete` on clean
  shutdown (src/revl/run.py, `_Driver`, `_commit_wal`). The MCP session is the
  same driver with the trace captured, driving load / swap / call / unload as
  verbs (src/revl/mcp/session.py); operator profiles gate the mutating verbs
  (src/revl/mcp/operator.py, `TOOL_VERB`).
- **The per-call registration point.** Item 318 (landed) opens the
  witnessed position in a provide-method: a per-tool-call witnessed effect
  registers its transactional inverse into the enclosing component's
  activation `Frame` (`Frame.transactional_method`, parked in
  `_deferred_transactional` and disposed by `drain` once the verdict bit is
  settled; `Frame.abort` sets `_aborting` so a settled activation can still
  revert), which the landed seam discharges on commit and replays on abort.
  318 flagged one fork for this doc, resolved below: whether per-call
  effects need a SESSION-scoped accumulator beyond the activation frame
  (they do not; the activation frame suffices for one-component/one-session
  H1).

## Decision 1: the commit boundary (and the 318 fork, resolved)

**A session is one driver lifetime.** Concretely: one `revl run` process, or
one MCP session from its first `revl_load` to its commit or abort. This is
already a materialized thing in the tree: it is the scope of the one WAL the
driver opens. WAL `seq` is global across it only while exactly one
`WriteAheadLog` instance lives for the whole session; today's reopen path
breaks that, and the seq-space section below makes the one-instance rule
normative. The session may span
many activations (the initial load, swaps, generation undos); it ends in
exactly one of COMMIT or ABORT (a crash is an abort whose replay runs in
`revl recover`).

**The commit point is an explicit verb, on three surfaces, one
implementation in the driver:**

- `:commit` at the `revl run` REPL; a clean interactive exit offers it (the
  exit prompt IS the commit prompt). `:abort` is the counterpart.
- `revl_commit` / `revl_commit_confirm` on the MCP session (two-step, see
  Decision 4): the harness relays the enumerated manifest to the human and
  confirms with the manifest hash. `revl_abort` is the counterpart. Both join
  `operator.TOOL_VERB` as a gated verb (`commit`), so an operator profile can
  scope who may commit.
- Unattended `revl run` (no tty): there is no one to prompt, so the driver
  refuses to flush a non-empty deferral queue and aborts with the manifest in
  the report, unless started with an explicit `--commit` acknowledging
  unattended flush. Silence never approves.

**The 318 fork: session commit IS the activation commit, moved; there is no
second accumulator.** The resolution:

1. The activation `Frame` remains the only accumulator. 318's mechanism
   (method-body witnessed effect registers into the activation frame) is
   correct and unchanged. No session-scoped accumulator is introduced for
   witnessed or compensation entries.
2. What changes is WHEN discharge happens. Under a session owner, a clean
   unload stops implying commit. `bracket` entries still run at unload
   (releasing a handle is always right, success or abort; unchanged).
   `transactional` and `compensation` entries of a mid-session withdrawal
   (a swap, an edit, an undo) are not discharged at drain; they are handed to
   the session's **discharge escrow**, a session-owned holding list of
   already-registered entries awaiting the session's verdict. Their WAL
   discharge records are NOT written at drain.
3. At session COMMIT, the driver discharges in one pass: every live frame's
   transactional and compensation entries plus the whole escrow. One durable
   `discharge` record covers all their seqs, written after the flush
   (Decision 3) and before `activation-complete`.
4. At session ABORT, live activations tear down exactly as the teardown
   contract specifies (per-activation two phases, cascade order); then the
   escrow replays, reverse-seq within the escrow, itself in two phases
   (transactional inverses, then owed compensations, both best-effort-guarded
   per the contract's Phase rules). The contract's "no global phase barrier
   across activations" already covers this shape; the escrow is one more
   torn-down activation's worth of entries, replayed last because its
   activations were withdrawn first.

So the answer to 318's question is: per-call effects do NOT need a
session-scoped accumulator. They need the activation accumulator they already
have, plus a session-scoped DECISION (commit or abort) that the activation
defers to. The two genuinely session-scoped structures 245 adds, the deferral
queue (Decision 3) and the discharge escrow (above), are not accumulators:
the queue holds actions that have not happened, the escrow holds entries that
already exist and merely wait for the verdict.

**The gate target: derived from the owner's registry, never "the current
frame".** The commit operator commits exactly three things, and it derives
them from state the session owner already holds, not from any notion of a
current activation:

1. the deferral queue (Decision 3), driver-owned;
2. the discharge escrow (above), driver-owned;
3. every LIVE activation frame in the owner's registry. A frame joins the
   registry when it registers its session owner (the same hook as the
   compatibility clause below) and leaves it at withdrawal, handing its
   undischarged entries to the escrow.

The manifest hash binds the target: `revl_commit` enumerates over a snapshot
of (queue, escrow, registry), `revl_commit_confirm` recomputes, and any
drift refuses (Decision 4). The runtime's per-call notion of a current
activation (`_FRAME_BY_CTX` and the activation stack in
backends/python/runtime.py) is an attribution convenience and plays no part
in target derivation: a session with three live components commits all
three or none.

How the verdict drives the 318 seam, both directions:

- COMMIT: the driver unloads each registry frame WITHOUT calling
  `Frame.abort()`. `drain` flips `_committed` True, the activation-body
  `_Transactional` disposers discharge, and the method-registered
  `_deferred_transactional` entries are disposed by `drain` itself with the
  bit already settled (the 318 seam: a method entry is not a cordis
  disposer, precisely so it observes the settled bit).
- ABORT: the driver calls `Frame.abort()` on EVERY registry frame FIRST,
  before any teardown starts, then unloads. `abort()` sets `_aborting`;
  `drain` then leaves `_committed` False and every entry replays. The
  ordering is normative: for a cleanly activated component `drain` runs on
  BOTH verdicts, so "did drain run" no longer discriminates (that is what
  318 added `_aborting` for). Mark-then-teardown is what makes the bit mean
  the verdict; an unload that reaches any frame's `drain` before its
  `abort()` has silently committed that frame's entries.

Disambiguation of abort from clean commit, stated at both levels. At
runtime: a committed frame ends with `_committed` True and its
transactional entries `discharged`; an aborted frame ends with `_committed`
False, `_aborting` True, and its entries `replayed` (the `_Transactional`
flags). At the WAL: a committed session carries `commit-approved` then
`discharge`; an aborted session carries NEITHER, plus the `aborted`
completion record when the replay finished in-process (Decision 3). The
absence of `commit-approved` is the abort verdict; a crashed abort and a
clean abort differ only in whether `aborted` closed the file.

**Compatibility clause.** The escrow behavior activates only when a session
owner registers itself with the frame (the driver, in run.py / mcp
session.py). A bare `Frame` with no owner keeps today's semantics: drain
discharges at unload, implicit commit. Every existing test and every
non-session embedding stays green; the teardown contract's "Commit path"
section gains one amendment sentence pointing here.

## The WAL seq space across the session (normative, Slice 2)

The seq space is the session's spine. Discharge records, `commit-approved`,
the `aborted` completion record (Decision 3), and recover's skip set all name
seqs, so what a seq MEANS must be stable for the entire session. The rule:

**One session, one WAL file, one `WriteAheadLog` instance, one counter.**
`seq` is strictly increasing for the whole session and is never reset while
the session is open. Every seq-bearing record kind draws from the same
counter (`effect`, `discharge-descriptor`, and Decision 3's
`deferred-emission` all consume `WriteAheadLog._seq`): a seq is a position in
the session, not a per-kind index. Discharge records name explicit seq lists,
never ranges, and one session WAL may carry several (each ownerless frame's
drain writes one today; the session commit writes the one covering escrow
plus live frames); the discharged set is their union, which is how
recovery.py already reads them (`discharged.update(...)` over every
`discharge` record). Explicit lists plus append-only are what keep an
in-flight abort or recover safe: a later record can only add facts about
later seqs, it can never re-describe a seq a replay is already acting on, and
no record is ever rewritten out from under a reader.

**What is wrong today, named precisely.** `WriteAheadLog` starts `_seq = 0`
per INSTANCE (backends/python/replay.py); `open()` opens the file in append
mode and writes a header only when the file is empty; and `Recorder.open_wal`
closes any prior WAL and constructs a NEW instance. The `--watch` reload path
(src/revl/run.py) calls `open_wal` again over the same path, so a mid-session
reopen silently restarts the seq space at 0 over a file that already carries
those seqs. Even pre-245 that is a live corner-case bug (watch + `--wal` +
witnessed): two descriptors share seq 0, and recover's union-then-skip can
skip an aborted transaction's rollback because a discharge record from the
other generation named the same number. Under 245 it is fatal, because
`commit-approved` and the session discharge record refer to seqs across the
whole session. This is the migration the slice must land, not a refinement it
may get to.

The migration, in four rules:

1. **Open once, hold for the driver lifetime.** The driver opens the WAL at
   session start and keeps that one instance until the verdict; nothing calls
   `open_wal` again while the session is open. A `--watch` generation reload
   continues the same instance and the same counter; the generation is
   record-level data (the header and records carry it), never a new WAL.
2. **A fresh WAL file means a new session.** A harness that wants one is
   asking for a session boundary: verdict first (commit or abort, Decision
   1), then the new file. There is no in-place reset.
3. **Reader hardening.** `revl recover` checks monotonicity while scanning:
   each seq-bearing record's seq must exceed the previous seq-bearing
   record's. A regression marks the file seq-corrupt; recover then refuses to
   interpret discharge seq references over the ambiguous region, and every
   descriptor whose seq appears more than once is reported as
   `unreconstructible` residue (the existing kind), never silently replayed
   and never silently skipped. Guessing which descriptor a discharge covers
   is exactly the silent failure this rule exists to prevent.
4. **What recover reads after a mid-session crash: the one file, whole.** The
   verdict comes from the markers (`activation-complete`, `commit-approved`,
   `aborted`; Decision 3 and its window rule). The discharged set is the
   union of discharge records. Undischarged descriptors replay reverse-seq
   across the WHOLE session, activations interleaved as they registered,
   which is the escrow's replay order by construction: escrowed entries keep
   their registration seqs, so reverse-seq over the file and reverse-seq
   within the escrow agree.

## Decision 2: the three classes are a type judgment

The class of an action is a total function of its checked extern
classification. Nothing at runtime, and nothing in the harness, can move an
action between classes.

| class | derivation | mid-session behavior | on commit | on abort |
|---|---|---|---|---|
| (a) revertible | `witnessed` extern (243) | executes freely, registers transactional inverse | discharge + witness GC (landed) | inverse replays (landed) |
| (b) deferrable | `emission` extern with the `deferred` modifier | ENQUEUES a descriptor; host body does not run | FLUSH: host body fires, once, post-prompt | DROP: never fired |
| (c) immediate | `emission` extern, no `deferred` | fires at the call | nothing left to do | already out; `compensate` may offset (247) |

**What makes an emission deferrable: a declared property, not a harness
mark.** The modifier is spelled in the extern classification slot, in the
same modifier position `async` occupies (parser.py, `ExternDecl.async_`):

    extern emission[mail] deferred fn send(to: Str, body: Str)
    extern emission[fs] deferred fn purge_garbage(dir: Str)

Declaration-owned for the same reason 243 made the inverse declaration-owned:
deferral changes call-site semantics. A deferred emission returns before the
world changes; a program calling it must not be able to observe the send. If
the harness could toggle deferral at runtime, the same program would mean two
different things under two harnesses, and the checker could not enforce the
rules below. The harness's discretion lives in 246 (whether to prompt, what
to auto-approve), never in what a call does.

Checker obligations (new, Slice 1):

- **`deferred` only on an `emission` extern.** A `pure` extern has nothing to
  defer; an `acquire` or `witnessed` extern must run mid-session (its return
  is the resource or the witness).
- **A deferred emission returns `Unit`.** The call completes before the action
  fires, so no value can flow back from it; a non-Unit return would be a lie
  the program could branch on. This rule is also the mechanical boundary
  between (b) and (c): an emission whose response the task needs mid-session
  cannot type as `deferred`, so it stays class (c). That is the roadmap's
  "immediately-irreversible" case, derived rather than annotated.
- **`deferred` and `compensate` are mutually exclusive.** A compensation
  offsets a fired emission on abort; the only abort that could ever owe it
  precedes the fire (the queue is dropped, nothing to offset), and after the
  flush the session is over (no abort remains). A compensation on a deferred
  emission is dead code by construction, so the checker refuses the pair.
- **`deferred` and `async` are mutually exclusive (v1).** There is no
  response to await.
- **Deferred emissions are refused in teardown positions** (`undo`,
  `compensate` bodies): teardown runs at or after the verdict; enqueueing
  into a queue that is already flushing or dropped is unanswerable. Same
  spirit as 247 decision 3's "a compensation emits and returns; it does not
  accumulate".

**Tier gate: `deferred` is refused at emit on the five ownerless tiers.**
Class (b)'s lowering needs a session owner at runtime: the deferral queue,
the escrow, and a commit verb to flush or drop it. Only the py tier has one
today (the `revl run` / MCP driver). Until a tier grows its own owner
(Slice 4), its emitter REFUSES any call to a `deferred` extern at emit
time. This is the wasm tier's existing discipline for a capability the tier
lacks ("violations are EmitError, never silent degradation",
backends/wasm/emit.py; a method-time compensation is already a hard
`EmitError` there), extended to all five ownerless tiers: rust, go, java,
wasm, typescript, each through its existing `EmitError` (or equivalent
refusal) channel.

Refusal, not degradation, because both available degradations lie. Firing
at call time executes an action the program was typed to withhold until
approval, the worst possible reading of the declaration. Enqueueing with no
owner drops the action on the floor: no verdict ever comes, so the send the
program promised (on commit) silently never happens. The refusal keys off
the call site, the point where the emitter would otherwise have to pick one
of those lies; a declared-but-never-called deferred extern does not poison
the build.

The diagnostic, one wording for all five tiers so six backends do not
invent six messages:

    <component>: `deferred` emission `<key>.<method>` needs a session owner
    runtime (the deferral queue and the commit verb), which the <tier> tier
    does not have yet; deferred emissions run on the python tier only.
    Refusing rather than degrading: firing at call time would break the
    declaration's promise that nothing crosses before the session commit.
    Either target the python tier, or drop `deferred` from the extern to
    make it an immediate emission (class (c): fires mid-session, prompted
    per 246).

The classification function is exported on the G8 audit surface: each
crossing record (src/revl/erase_report.py aggregates every reached host
extern) carries its class tag, so 246's policy reads `(a)/(b)/(c)` off the
checked surface with no re-derivation.

## Decision 3: the deferral queue

**Where class-(b) tails live until commit: a session-scoped FIFO of
named-call descriptors, WAL-logged at enqueue.** The queue is owned by the
driver (the same owner as the escrow), not by any frame: entries survive the
withdrawal of the activation that enqueued them (a swapped-out tool
component's queued send still flushes at commit, and still drops on abort).

The entry is the WAL's existing named-call discipline (243 rule 4, 247
decision 5: captured serializable arguments, never a closure), as a new WAL
record kind written at enqueue:

    {
      "record": "deferred-emission",
      "seq": <global session seq>,
      "call": {"receiver": ..., "method": ..., "args": [...]},
      "origin": {"key": ..., "method": ..., "args": [...], "site": ...},
      "idempotency": <author key or null>
    }

**The call site is the enqueue.** The emitted code for a deferred emission
does not invoke the host body; it appends the descriptor to the session queue
(and the WAL) and returns Unit. The host body runs exactly once, at flush, or
never. This single-lowering property is load-bearing for Decision 4's
exhaustiveness proof.

**On commit: FLUSH.** After the approval (Decision 4) is durable, the driver
fires the queue FIFO (program order, the causal order the intents were
formed in), calling each descriptor's host body. Each completed fire appends
`{"record": "flushed", "seq": N}`. Write-ahead honesty: the intent was logged
at enqueue; the outcome is logged after the fire, because logging a send
before it happens would claim a crossing that may not exist. A crash between
the fire and its `flushed` record therefore leaves that one emission
`outcome: unknown` for recover, which is the truth; an idempotency key (item
309) is the author's tool for making the re-check safe. A flush failure
(the host body raises) is continue-and-record, exactly the Phase-1 rule of
the teardown contract: the remaining queue still flushes, the failure becomes
a residue record of a new kind, `flush-residue`, and the commit still
completes with that residue enumerated in the final report. `flush-residue`
is an addition to the merged residue schema and is therefore an amendment to
docs/design/teardown-contract.md (its schema is closed by its own rule);
the record reuses the existing shape with `attempted.phase: null` (flush is
not an abort phase).

After the flush, the driver writes the one `discharge` record (escrow plus
live frames, Decision 1), then unloads cleanly, then `activation-complete`.
Durability order: `commit-approved`, then `flushed` records, then
`discharge`, then `activation-complete`. Each is fsync'd by the existing
`WriteAheadLog._write` discipline.

**The approved-to-discharged window.** Between `commit-approved` and the
`discharge` record there is a window in which the session's verdict is
decided but the per-seq bookkeeping is not durable: inverses are not yet
discharged, witnesses not yet GC'd, the flush possibly part-done. A crash
anywhere inside that window is a COMMITTED session. The record ordering is
what makes the answer unambiguous: `commit-approved` is written before the
first fire and before `discharge`, so its presence IS the commit point.
`revl recover` on a WAL that carries `commit-approved`:

- replays NO transactional inverse and re-issues NO compensation from this
  session, whether or not their seqs appear in a discharge record. The
  durable approval, not the discharge record, is the session's commit proof;
  the discharge record is bookkeeping the crash interrupted.
- rolls that bookkeeping forward: it appends the missing `discharge` record
  itself, naming every descriptor seq not already discharged. Appending a
  record fires nothing, so the roll-forward is safe, and it makes a second
  recover pass read the same verdict with no special-casing.
- reports the flush state per the crash cases below: each approved
  descriptor with no `flushed` record is owed.

A WAL with no `commit-approved` is the other verdict: abort semantics,
undischarged descriptors replay. There is no third state. For an ownerless
frame (the compatibility clause, no session owner registered) the landed
rule stands unchanged: drain's discharge record remains the commit proof,
durable before success is reported (teardown-contract.md, "Commit path").
This window rule extends the contract's recover reading, it does not
contradict it: discharged-seq skipping stays true, and `commit-approved`
adds a superset skip for the session-owned case. It joins the
teardown-contract.md amendments Slice 2 owes (see the slice plan).

**On abort: DROP.** The queue is discarded. No host body runs; the verdict
needs no record, because the ABSENCE of `commit-approved` is the abort
verdict (the window rule above: two states, no third). `revl recover`
reading a WAL with `deferred-emission` records, no `commit-approved`, and no
`activation-complete` treats them as dropped: reported in the verdict as
"n deferred emission(s) dropped, never fired", counted clean (zero
crossings), never residue. This is the exact-by-construction abort path.

One completion record is still written, for recover's benefit, not the
verdict's: an in-process abort that finishes its replay appends
`{"record": "aborted", "replayed": [seq...]}` naming the seqs whose inverses
actually ran, after the escrow replay completes and before the driver exits.
It lets recover tell a COMPLETED abort (report clean, redo nothing) from a
CRASHED one (finish the replay). A crash between the last inverse and the
`aborted` record costs only a redundant re-run on recover, never a wrong
one: inverses are idempotent-on-replay (243 rule 5). The record is
bookkeeping; the missing `commit-approved` remains the verdict.

**The 245/247 boundary, stated.** A dropped deferral and a compensation are
not two strengths of the same thing; they are on opposite sides of the fire:

- Deferral (245) acts BEFORE the crossing: on abort the emission was never
  sent, the proof surface records nothing, and the guarantee is exact.
- Compensation (247) acts AFTER the crossing: the forward emission already
  left, the offset is a second crossing, best-effort, audit surface only.

An author's rule of thumb follows: if the task does not need the response
mid-session, declare the emission `deferred` (class b) and get the exact
abort for free; reach for `compensate` only when the emission must fire
mid-session (class c). The checker's `deferred`/`compensate` exclusion keeps
the two from ever stacking.

**Crash cases, aligned with recovery.py's voice:**

- Crash before commit: roll back. Queue entries dropped (clean, reported);
  escrowed and live transactional inverses replay per the landed
  `_roll_back` (discharged-seq skipping is already correct because no
  discharge record was written yet).
- Crash after `commit-approved`, mid-flush: the approval was durable and
  named the manifest hash, so the session is COMMITTED (the window rule).
  Recover replays no inverse and re-issues no compensation, appends the
  missing `discharge` record, and reports each approved descriptor with no
  `flushed` record as OWED (`flush-residue`, `attemptedFlag: false`,
  `outcome: not-attempted`, hint: finish the flush by hand or re-run with the
  idempotency key), and each `flushed` one as fired. v1 recover never
  auto-fires an owed emission (report, do not pretend; open question 2).
- Crash after `discharge`: committed. `activation-complete` absent still
  rolls back the OTHER records, but every discharged seq is skipped (landed
  behavior, recovery.py `_roll_back`), and flushed emissions are already
  honestly logged.

## Decision 4: the one-prompt residue enumeration (246's input)

At `revl_commit` / `:commit` the driver produces the **commit manifest**, the
one schema 246 freezes against (sibling of the teardown contract's residue
envelope, same one-channel discipline):

    {
      "deferred": [                  # the queue, verbatim, grouped for display
          {"receiver": ..., "method": ..., "args": [...], "site": ...,
           "group": "fs.purge_garbage"}
      ],
      "summary": [                   # grouped counts, the prompt's one line
          {"group": "fs.purge_garbage", "count": 3},
          {"group": "mail.send", "count": 1}
      ],
      "fired": [...],                # class-(c) crossings already out this
                                     # session, with 247's three-state tag
                                     # (bare / compensated / unresolved)
      "witnessed": {"count": N},     # class-(a) entries about to discharge
      "residue": {...},              # the merged residue envelope as it stands
                                     # (restore-residue etc.), usually clean
      "prompts": {...},              # Decision 6 counters
      "hash": "sha256:..."           # over the canonical JSON of the above
    }

The prompt the human sees is `summary` rendered: "empty trash: 3 files;
send: 1 email". Everything else is the evidence behind it.

**Why the enumeration is provably exhaustive.** Three facts, all checked:

1. Every boundary crossing goes through a classified extern; the checker
   refuses an unclassified host call, and the G8 surface (emission analysis,
   erase_report.py) enumerates every reached extern per component. There is
   no unenumerated way out of the process.
2. A class-(b) call has exactly one lowering, the enqueue (Decision 3). So
   the queue IS the set of irreversible crossings the commit will cause; an
   emission that bypassed the queue would have had to fire mid-session, which
   its `deferred` typing forbids.
3. Class-(c) crossings are WAL-logged at fire (the existing emission
   records), so `fired` is complete over what already left.

The manifest is therefore not a best-effort summary of what the agent
remembers doing; it is a projection of the checked audit surface plus the
queue, and a crossing missing from it is a compiler bug, not a policy gap.

**Approval is bound to the hash.** `revl_commit` enumerates and returns the
manifest; `revl_commit_confirm(hash)` flushes. If the queue or the live
composition changed since enumeration (another enqueue, a swap), the
recomputed hash mismatches and the confirm is refused with a fresh manifest:
what the human approved is exactly what fires, never a superset. The approval
is durable (`{"record": "commit-approved", "hash": ...}`) before the first
fire. This is deliberately the same bind-to-candidate-hash shape 246's folded
typed-approval proposal requires, so 246 inherits it rather than reinventing
it; the prompt's wording, the auto-approve policy, and the per-call class-(c)
prompting are 246's item and are not specified here.

## Decision 5: abort

Abort is the landed machinery plus two subtractions. On `:abort` /
`revl_abort` / driver failure, the driver first marks every registry frame
with `Frame.abort()` (Decision 1's gate target: the `_aborting` bit must be
set before any teardown starts, or a frame's `drain` implicitly commits it),
then:

1. The deferral queue is dropped (Decision 3). Zero cost, zero crossings.
2. Live activations tear down per the teardown contract, unchanged: Phase-1
   proof replay (bracket + transactional, continue-and-record, both
   severities), Phase-2 compensations (bounded, guarded).
3. The discharge escrow replays after the live cascade, reverse-seq, in its
   own two phases (Decision 1).
4. The abort report is the merged residue envelope, extended only by the
   dropped-deferral count in the proof line. In recovery.py's voice: what
   ran, what is still out, what was never fired at all; a session that only
   ever used classes (a) and (b) aborts to a provably clean world, and the
   proof says so ("no residue: ... n deferred emission(s) dropped, never
   fired").

The crash half is `revl recover`, already two-phase and discharge-aware; its
deltas are exactly the three crash cases in Decision 3 (dropped-clean,
approved-owed, discharged-skipped) plus the `flush-residue` kind.

## Decision 6: metrics (prompts-per-session)

The driver counts every human interaction it or 246 raises, in one place:

- `commit`: 1 per session (the Decision 4 prompt), 0 for an aborted session;
- `perCall`: one per class-(c) prompt (incremented by 246's operator layer
  when it lands; 0 until then);
- `residue`: one per residue-triggered prompt (restore-residue,
  bracket-fault escalation, flush-residue).

Reported in the commit manifest (`prompts`), in the abort report, and in
`session.state()`. Prompts-per-session is their sum; the target is ~1, and
the claim behind the target is now measurable: a session whose every action
is class (a) or (b) shows exactly `{"commit": 1, "perCall": 0, "residue": 0}`.
The companion ratio for 248's headline number, fraction of calls
auto-approved-with-proof, is `(a + b calls) / all boundary calls`, computable
from the class tags on the G8 crossing records (Decision 2). 248 measures
both before/after on a real harness.

## Slice plan (py first, on the landed foundation)

- **Slice 0: this doc.**
- **Slice 1: frontend + IR.** The `deferred` modifier in the extern
  classification slot (parser.py, the `async_`-style modifier field); the
  Decision 2 checker obligations; `deferral: true` on the emission IR node;
  the class tag on G8 crossing records. Additive: no existing program spells
  `deferred`, backends untouched, suite stays green. Files: src/revl/parser.py,
  lower.py, emission_analysis.py, erase_report.py, diagnostics.py.
- **Slice 2: py runtime seam.** The session owner registration on `Frame`
  (compat clause, Decision 1) and the owner's frame registry (the gate
  target); drain defers transactional/compensation discharge to the owner;
  the driver-owned deferral queue and discharge escrow; the WAL seq-space
  migration (the one-instance rule, the `open_wal` reopen fix, recover's
  monotonicity check); enqueue lowering for deferred call sites in
  backends/python/emit.py; WAL record kinds `deferred-emission` /
  `commit-approved` / `flushed` / `aborted` in replay.py; recover deltas
  (dropped-clean, approved-owed, `flush-residue`, the window rule's
  commit-approved-dominates verdict plus its discharge roll-forward,
  `aborted`-aware abort reporting) in src/revl/recovery.py; the
  `deferred`-call emit refusal on the five ownerless tiers (Decision 2's
  tier gate) in backends/{rust,go,java,wasm,typescript}/emit.py; the
  teardown-contract.md amendments (commit-path sentence, `flush-residue`
  kind, the recover reading extension: `commit-approved` dominates the
  discharge set for a session-owned WAL).
- **Slice 3: the commit surfaces + metrics.** `:commit` / `:abort` and the
  exit prompt in src/revl/run.py; `revl_commit` / `revl_commit_confirm` /
  `revl_abort` in src/revl/mcp/session.py + server.py; the manifest schema
  and hash; the `commit` verb in operator.py `TOOL_VERB`; the prompt
  counters. Exit tests below.
- **Slice 4: 246 consumes.** The operator-layer policy over the manifest and
  the class tags. Not specified here. The other five tiers acquire the queue
  and escrow with their own harness stories; the py driver is the only
  session owner today, so no cross-tier fan-out is owed by 245 itself (the
  wasm activation-time-accumulator restriction noted in the teardown
  contract applies to escrow adoption there and is that item's business).

Exit tests (the conformance gate for Slices 2/3):

1. A deferred emission's host body does not run before commit; runs exactly
   once after confirm; FIFO order.
2. Abort and crash-before-approval drop the queue: zero host calls, verdict
   counts them dropped-clean.
3. A mid-session swap escrows: the withdrawn activation's witnessed mutation
   persists on session commit and reverts on session abort (this is the
   observable difference from today's unload-is-commit).
4. Confirm with a stale hash is refused after a post-enumeration enqueue.
5. Durability order on commit: approved, flushed, discharge,
   activation-complete; `revl recover` on a WAL cut after each prefix gives
   the Decision 3 verdicts (owed / unknown / skipped-committed). In
   particular, the cut inside the approved-to-discharged window: no inverse
   replays, the discharge record is rolled forward, a second recover pass is
   a no-op.
6. An all-(a)/(b) session reports `prompts == {"commit": 1, ...}` and an
   exact clean abort.
7. No session owner registered: byte-identical behavior to today (drain
   discharges at unload), the whole existing suite plus the per-backend
   goldens stay green.
8. Seq-space: a `--watch` reload mid-session with `--wal` continues the same
   seq counter (no restart at 0); recover on a hand-built seq-regressed WAL
   reports the ambiguous descriptors `unreconstructible` and replays none of
   them.
9. Tier gate: a program calling a `deferred` extern is refused at emit on
   rust, go, java, wasm, and typescript with the Decision 2 diagnostic; the
   same program with the extern declared but never called emits cleanly.

## Open questions (left deliberately)

1. **Generation undo vs the escrow.** `revl_undo` (item 65) withdraws a
   generation mid-session; its transactional entries escrow under Decision 1,
   so the undone generation's witnessed mutations persist until the session
   verdict. Arguably an undo WANTS the mini-abort (replay that generation's
   escrowed inverses immediately). Deciding that needs the item-65 history
   semantics on the table; until then the escrow default is the conservative
   one (nothing is irreversibly discharged early).
2. **Recover roll-forward of approved-but-unflushed emissions.** v1 reports
   them owed and never auto-fires. With a typed idempotency key (item 309) a
   `revl recover --complete-commit` could finish an approved flush safely.
   Deferred until 309 exists.
3. **Long-lived sessions across process restarts.** A harness that wants a
   session to survive its own restart would need the queue and escrow in the
   item-15 snapshot. Out of scope for v1: a session ends in commit or abort
   within one driver lifetime.
4. **Flush ordering beyond FIFO.** Cross-receiver ordering constraints (fire
   all fs purges before any mail?) are not expressible in v1. If a real
   harness needs them, they belong in the manifest as a declared partial
   order, which is also where the human would see them.
5. **Unattended commit policy.** The `--commit` acknowledgment for no-tty
   runs is a blunt instrument; whether 246's policy language should subsume
   it (an operator profile that pre-approves specific groups) is a 246
   question, flagged there.
