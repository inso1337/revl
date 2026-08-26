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
- **The per-call registration point.** Item 318 (in flight) opens the
  witnessed position in a provide-method: a per-tool-call witnessed effect
  registers its transactional inverse into the enclosing component's
  activation `Frame`, which the landed seam already discharges on commit and
  replays on abort. 318 flagged one fork for this doc: whether per-call
  effects need a SESSION-scoped accumulator beyond the activation frame.

## Decision 1: the commit boundary (and the 318 fork, resolved)

**A session is one driver lifetime.** Concretely: one `revl run` process, or
one MCP session from its first `revl_load` to its commit or abort. This is
already a materialized thing in the tree: it is the scope of the one WAL the
driver opens, and WAL `seq` is already global across it. The session may span
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

**Compatibility clause.** The escrow behavior activates only when a session
owner registers itself with the frame (the driver, in run.py / mcp
session.py). A bare `Frame` with no owner keeps today's semantics: drain
discharges at unload, implicit commit. Every existing test and every
non-session embedding stays green; the teardown contract's "Commit path"
section gains one amendment sentence pointing here.

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

**On abort: DROP.** The queue is discarded. No host body runs; no WAL record
is needed beyond the absence of `commit-approved`/`flushed`. `revl recover`
reading a WAL with `deferred-emission` records, no `commit-approved`, and no
`activation-complete` treats them as dropped: reported in the verdict as
"n deferred emission(s) dropped, never fired", counted clean (zero
crossings), never residue. This is the exact-by-construction abort path.

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
  named the manifest hash. Recover reports each approved descriptor with no
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
`revl_abort` / driver failure:

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
  (compat clause, Decision 1); drain defers transactional/compensation
  discharge to the owner; the driver-owned deferral queue and discharge
  escrow; enqueue lowering for deferred call sites in backends/python/emit.py;
  WAL record kinds `deferred-emission` / `commit-approved` / `flushed` in
  replay.py; recover deltas (dropped-clean, approved-owed, `flush-residue`)
  in src/revl/recovery.py; the teardown-contract.md amendments (commit-path
  sentence, `flush-residue` kind).
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
   the Decision 3 verdicts (owed / unknown / skipped-committed).
6. An all-(a)/(b) session reports `prompts == {"commit": 1, ...}` and an
   exact clean abort.
7. No session owner registered: byte-identical behavior to today (drain
   discharges at unload), the whole existing suite plus the per-backend
   goldens stay green.

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
