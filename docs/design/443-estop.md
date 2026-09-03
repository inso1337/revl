# Design: the operator E-Stop

Roadmap item 443. Source: `formal/RevL/Semantics.lean` (`Verdict.halted`,
`EntryKind.strandedUnder`), `formal/RevL/Theorems/G7_LifoComplete.lean` (the
E-Stop column and the halt cut), `backends/python/runtime.py` (the latch, the
crossing seams, the stranding), `src/revl/mcp/session.py` (`Session.estop`),
`src/revl/cli/change.py` (`revl estop`), `src/revl/mcp/operator.py` (the
`estop` authority), `src/revl/estop.py` (the shared latch vocabulary),
`src/revl/placement.py` (the conductor halt and its report),
`src/revl/_process_runner.py` (the child watcher).

## The gap

Every stop revl had was COOPERATIVE. Teardown replays inverses LIFO under the
activation's verdict, faults route through residue records, withdrawal
propagates to dependents. That is exactly right for a composition fault and
exactly wrong for an operator emergency.

When a human hits the button during a class-(c) storm or a runaway loop with
irreversible effects in flight, unwinding two hundred brackets first is not a
safe answer. It is a long answer. The two hundred brackets are two hundred more
opportunities for the runaway loop to cross the boundary again, and the
compensation drain the abort ends with is itself a queue of fresh emissions.

So the E-Stop is not `abort` with a shorter name, and it must not be built by
making `abort` faster. It is a different verdict, and the honest part of it is
the accounting, not the halt.

## The verdict

`Verdict` gains a third constructor, `halted`. `commit` and `abort` are the
SETTLING verdicts — under each of them every entry on the activation's stack
ends up either replayed or discharged. `halted` settles nothing.

| entry kind | `commit` | `abort` | **`halted`** |
|---|---|---|---|
| `bracket` (acquire) | replayed | replayed, Phase 1 | **stranded** |
| `transactional` (243) | discharged | replayed, Phase 1 | **stranded** |
| `compensation` (247) | discharged | replayed, Phase 2 | **stranded** |

**Stranded** is the third disposition: registered, not run, and NOT dropped.
It is not a synonym for discharged, and the difference is load-bearing.
Discharge RELEASES the inverse closure and the witness (`runtime.py`'s
`discharged = True`, "so no rollback state survives a committed transaction").
Stranding keeps both, because the reconciliation path is what reads them back.

`RevL.Semantics.disposition_trichotomy` proves every (kind, verdict) pair has
exactly one of the three dispositions, and `RevL.Semantics.book_lengths_add`
proves the three lists partition the stack under every verdict. That is the
total accounting: an entry cannot fall off the books by being halted.

## What the E-Stop guarantees, precisely

Four things, and no more:

1. **Bounded latency, not bounded state.** Once the latch is set, no NEW
   boundary crossing is dispatched by this process. The halt costs one latch
   flip plus a walk of the live frames — never the cost of a teardown, a hung
   compensation, or a Phase-2 budget.
2. **Nothing is invented.** The halt runs zero inverses, zero compensations
   and zero deferred flushes, and writes no discharge record. It cannot make
   the world worse than it was at the instant of the halt, modulo the one
   in-flight crossing it cannot recall. (`RevL.G7.estop_replays_nothing`,
   `estop_discharges_nothing`.)
3. **Total accounting.** Every registered entry is on exactly one of the halt's
   books, and their union is the whole stack. (`RevL.G7.halt_books_are_total`.)
4. **Ambiguity is named, never resolved.** At most one crossing per activation
   is dispatched-and-unconfirmed when the latch is read. It is recorded
   `outcome: "unknown"` — item 440's ambiguous tier — not guessed.
   (`RevL.G7.halt_ambiguity_is_at_most_one`.)

### What it explicitly does NOT guarantee

Stated here rather than discovered in production:

* **No G7 LIFO completeness.** Nothing replays, so the LIFO story is vacuous
  under this verdict. That is by design, and the formal layer says so instead
  of quietly making G7 conditional (see "Open question 1").
* **No R4 no-residue.** R4 is a property of the ABORT path. The E-Stop
  violates it deliberately; its counterpart claim is the inverse one, "all
  residue, all of it reported" (`RevL.G7.estop_strands_everything`).
* **No handle release.** Brackets are stranded, so file descriptors, pool
  connections and leases stay held until the process exits or `revl recover`
  runs. **An E-Stop trades resource cleanliness for stop latency.** That is
  the trade the button exists to make, and a program that cannot afford it
  should use `revl_abort`.

## The residue record

Two new `kind` values join the merged residue schema
([teardown-contract.md](teardown-contract.md), "The merged residue schema"):

* **`estop-stranded`** — a registered entry whose inverse or compensation was
  never attempted. `attemptedFlag: false`, `attempted: null`,
  `outcome: "not-attempted"`, `error.type: "estop"`.
* **`estop-ambiguous`** — the at-most-one crossing that was already dispatched
  when the latch was read. `attemptedFlag: true`, `outcome: "unknown"`. This
  is item 440's ambiguous tier, and the E-Stop is precisely the event that
  CREATES that state deliberately: it lands between the journal entry and the
  completion record, so the ambiguous tier stops being an edge case and
  becomes the designed outcome of an operator halt.

Both carry `state: "unresolved"`, the third audit state 247 already defines,
and both name the crossing they belong to (`component`, `method`, WAL `seq`).

The halt record itself carries the in-flight inventory:

```
{ "halted": true, "verdict": "halted",
  "reason": str, "operator": str, "at": float,
  "activations": [{"component": str, "stranded": int}],
  "inFlight":   [Record...],   # estop-ambiguous, 0 or 1 per activation
  "stranded":   [Record...],   # estop-stranded, named at halt time
  "resumable":  false,
  "reconcile":  "revl recover --wal <file>" }
```

`clean` is `false` whenever a halt record exists. An E-Stop is never clean.

### Why the inventory is built in two halves

The halt names what it CAN name from each live frame: the witnessed
(`transactional`) entries, the `compensation` entries, and the stateful host
resources the activation acquired. An emitted BRACKET inverse is a bare
`lambda: <undo>` living in the cordis disposable list with no entry object
behind it, so it is not reachable from the frame at halt time. It is stranded
instead at `Frame._guard` — the one chokepoint every emitted disposer shape
already passes through — as the process unwinds and hands it over.

`runtime.estop_residue()` is the merged view, and the two halves are
deduplicated by a per-entry flag so an entry named at the halt is not counted
twice when cordis later hands its disposer over.

## Open question 1: does G7 weaken?

**No, and the distinction matters.**

G7's completeness (`teardown_replays_all`) and soundness
(`teardown_only_witnessed`) were already stated relative to `replaysUnder v`
after item 418's correction. They therefore extend to `.halted` UNCHANGED and
hold with an empty replay set. Nothing about them is conditional, and no
hypothesis was added to either.

What did change is two COROLLARIES that quantified over all verdicts while
assuming every verdict settles:

* `RevL.G7.bracket_replays_under_every_settling_verdict` (renamed from
  `bracket_replays_under_every_verdict`, so the name says what the theorem
  says) now carries `hv : v.settles = true`. The E-Stop counter-instance is
  proved alongside it (`estop_strands_the_bracket`), so the hypothesis is
  exhibited as load-bearing rather than left as an unexplained weakening.
* `RevL.Semantics.replays_or_discharges` — the replay/discharge dichotomy —
  likewise. It was true because every verdict settled; it is now stated over
  the verdicts that do, with `disposition_trichotomy` as the total statement
  that holds everywhere.

Four further theorems pin what that hypothesis costs, so it cannot become a
place to park a broken proof: `settles_iff_not_halted` (it excludes exactly
one verdict), `settles_iff_strands_nothing` (it excludes it because that is
the verdict that strands, so `settles` is the property the dichotomy needs
rather than a chosen side condition), `settling_strands_nothing` (a settling
verdict owes nothing, on every stack), and
`bracket_replays_exactly_when_settling` — the bracket row as an EQUATION
with no hypothesis at all, so the restricted corollary is weaker than
something proved rather than weaker than something claimed.
`bracket_is_replayed_or_stranded` is then the total form over every verdict.

And G7 gains its E-Stop counterpart rather than losing anything:
`estop_replays_nothing`, `estop_discharges_nothing`,
`estop_strands_everything`, `halt_inventory_is_total`,
`halt_ambiguity_is_at_most_one`, `halt_books_are_total`. So G7 is not
conditional; it is complete over a table with a third column, and the column's
honesty is carried by new theorems rather than by weakening old ones.

## Open question 2: a bracket whose inverse is itself in flight

It becomes that activation's single `estop-ambiguous` record.

The inverse was dispatched and its completion was never recorded, so it may
have released the handle or not. Formally the halt is a CUT at index `k` into
the interrupted verdict's replay order:

* `haltCompleted v k log = (replayed v log).take k` — completions recorded;
* `haltAmbiguous v k log = ((replayed v log).drop k).take 1` — dispatched,
  unconfirmed, and `length ≤ 1` is a theorem;
* `haltUnattempted v k log = (replayed v log).drop (k + 1)` — never attempted.

A halt arriving during forward execution is the same shape with `k = 0`, and
the ambiguous entry is then a forward crossing rather than an inverse. The
runtime marks both through the same `_InFlight` seam, so one record shape
covers both.

Reconciliation treats it exactly as item 309 already treats a spent
at-most-once attempt: with an idempotency key, recovery may re-issue safely;
without one, it refuses with `outcome: "unknown"` and the operator finishes by
hand. That is the existing behaviour, reached deliberately instead of by
accident.

## Open question 3: does an E-Stopped composition resume?

**No. The instance is dead and recovery is the only path back.**

The body was cut mid-step and the runtime cannot know whether the in-flight
crossing landed, so resuming would re-enter a body whose preconditions are
unknown — exactly the "pretend it did not happen" this design exists to
forbid. `Session.load`, `call`, `swap`, `commit_confirm` and `abort` all refuse
after a halt. `unload` still works, and strands rather than unwinds: the
process still has to drop its fibers.

The paths back, in order:

1. **`revl recover --wal FILE`.** Every stranded `transactional` inverse and
   every stranded `compensation` was WAL-logged as a named-call discharge
   descriptor at REGISTRATION, and the halt writes no `discharge` record. So
   the descriptors with no discharge behind them are exactly the entries still
   owed — which is exactly what a CRASH leaves. **An E-Stop is deliberately
   shaped to look like a crash to the recovery path**, because revl already
   has a proven answer for a crash. This is why stranding keeps the descriptor
   instead of discharging it.
2. **`revl estop --report --wal FILE`.** Read the inventory off the durable
   log without touching the world, before deciding how to reconcile.
3. **By hand, for the ambiguous record.** The one thing recovery cannot decide.

## The surfaces

| surface | verb | note |
|---|---|---|
| CLI | `revl estop --latch FILE` | arms the latch a running process watches |
| CLI | `revl estop --report` | reads the inventory back, touches nothing |
| CLI | `revl estop --clear` | removes the latch; NOT a resume |
| CLI | `revl run --estop-latch FILE` | arms the run to watch a latch |
| CLI | `revl run --placement P --estop-latch FILE` | arms the CONDUCTOR and every py child |
| MCP | `revl_estop` | gated on the `estop` operator verb |
| MCP | `revl_estop_report` | read-only |
| runtime | `runtime.estop(reason, operator=…)` | requires an operator token |

### Why `estop` is its own operator authority

It is held as an operator authority (item 55,
[operator-capabilities.md](../operator-capabilities.md)) and never folded into
`unload` or `commit`. An operator trusted to unload a composition cleanly is
not automatically trusted to strand two hundred brackets and leave every
handle held.

More importantly, this is the one verb a composition or an agent must never be
able to invoke on itself — which is the whole point of an emergency stop. So:

* there is deliberately **no in-language surface**: no extern, no stdlib
  binding, nothing an `.rvl` body can reach;
* `runtime.estop()` refuses without an operator token (`EstopRefused`), as the
  defensive twin for an embedding that reaches the module function directly;
* the MCP verb's target set is the WHOLE running composition, so a
  subject-scoped `may estop on tenant_a*` authorizes only while every live
  component is in `tenant_a*`. An operator who must always be able to hit the
  button needs `may estop on *`.

### The cross-process latch

There is no control socket, and adding one would be a second thing to secure.
The rendezvous is a **latch file**: `revl estop` writes it, and every crossing
seam in an armed process reads it. The cost is one `open()` per crossing while
armed, and NOTHING when no latch is armed — which is the default, so a
composition that never arms one is byte-identical to the pre-443 runtime.

A latch that exists but does not parse still HALTS. Failing open on a
malformed emergency stop is the one failure mode this feature exists to
prevent.

Deriving the latch from the WAL path (`--wal FILE` implies `FILE.estop`) is
not a convenience: the WAL is the durable rendezvous the reconciliation path
already uses, so a halt and its reconciliation name the same session with one
argument.

## Where the seam sits in the py tier

`_estop_check()` is called at every point that dispatches or accepts a new
boundary crossing:

* `plug` — an activation is a fresh batch of crossings, and refusing before
  `ctx.plugin` means no frame, no accumulator and nothing to strand;
* `Frame.acquire` — a `let-effect` acquisition;
* `Frame.transactional` / `transactional_method` — registration IS the
  crossing here, since the emitted call site yields on the `Ok` branch;
* `Frame.compensation` / `compensation_method`;
* `Frame.enqueue_deferred` and `_Deferred.fire` — the class-(b) queue;
* `Frame.request_approval` and `Frame.approval_crossing` — the class-(c) seam,
  refused before the token is spent, so a halt never leaves an approval
  consumed-but-unfired.

`_InFlight` brackets the calls that are genuinely dispatched — the approval
crossing, a deferred flush, and a teardown inverse — so the halt can name the
one that was out.

## The conductor (`revl run --placement`)

A composition split across processes had no operator halt at all: every stop
`run_placement` had was `_stop_all`, which asks each child to unwind and waits
on its own `DOWN` line — the child's statement that its LIFO walk covered every
registered entry (G7) and its no-residue proof printed (R4). That is the
graceful path, and it is the one an operator emergency cannot use.

`--estop-latch FILE` arms the conductor. A watcher thread reads the latch, and
when it appears the conductor does four things, in this order:

1. **Says it**, on the trace, before anything that can take time. The operator
   sees the halt at the instant it lands rather than after the inventory.
2. **Stops every child**, in two populations — see below. It asks no child to
   unwind, waits for no `DOWN`, and does not call `_stop_all`.
3. **Reports**, on stderr, naming every component left un-torn-down and every
   outstanding obligation, including the ones it cannot enumerate.
4. **Unblocks the conductor** (`_thread.interrupt_main`), because a halt that
   needed the main loop's cooperation to be noticed would not be one. The run
   exits non-zero: an E-Stop is never clean.

### The two populations, and why the report has to distinguish them

* A child on a tier that **honors the latch** (today: `py`) is already
  refusing new crossings at its own seams by the time the conductor acts —
  the conductor hands it the latch path in its environment AND in its process
  spec, because a sandboxed child (item 411) need not inherit the conductor's
  environment and an emergency stop a confined process cannot see is not one.
  Its runner watches the latch on a timer (`runtime.estop_from_latch`), since
  `_estop_check` engages lazily at the next crossing and an IDLE process
  crosses nothing — it would notice the emergency only when work next arrived.
  On the button it prints `[<name>] HALTED <inventory>` and calls `os._exit`:
  no teardown, no inverse, no residue proof. The conductor merges that
  inventory into its report by name.
* A child on **any other tier** has no E-Stop seam, so the only halt that
  exists for it is a SIGKILL, and the conductor sends one immediately. It may
  have dispatched a crossing microseconds before it died and nothing recorded
  that, so its residue is **UNKNOWN**. The report says exactly that, per
  component, rather than folding it into a group line — a halt that hid which
  processes were merely killed would be reporting a stop it did not perform.

`REVL_ESTOP_HALT_WINDOW` (default 2s) bounds how long a latch-honoring child
gets to name its inventory before it is killed anyway. It is not a teardown
grace and must never become one: when it starts, the child's seams are already
refusing, so it buys the INVENTORY and nothing else. A child that misses it is
killed and reported UNKNOWN, which is strictly better than a halt that waits.

### The report

```
E-STOP ENGAGED — the placement is HALTED, not torn down
  latch     /run/session.wal.estop
  reason    runaway loop
  operator  ops@example

  Nothing was unwound. ... G7's LIFO completeness is VACUOUS under the
  `halted` verdict (nothing replays) and R4's no-residue proof does NOT hold.

  components left UN-TORN-DOWN (2):
    HotWorker  process provider  tier py
               HALTED at its own crossing seams (no new crossing dispatched)
               and died where it stood; 1 entry STRANDED, 1 crossing AMBIGUOUS
    Edge       process edge  tier node
               SIGKILLed at once: the node tier has NO E-Stop seam, so it kept
               dispatching crossings until it died — residue UNKNOWN

  outstanding residue (3 lines, 1 of them UNKNOWN):
    provider/HotWorker  estop-ambiguous  write   outcome unknown  [seq 7]
    provider/HotWorker  estop-stranded   remove  outcome not-attempted  [seq 4]
    edge  UNKNOWN  the node tier named no inventory; whatever it held is still
                   held and still owed
```

The UNKNOWN lines are the point. They are counted in the residue total, so an
operator reads "3 lines, 1 of them UNKNOWN" and knows the inventory is
incomplete BY EXACTLY ONE PROCESS rather than being told a comfortable number.

## Per-tier status

The halt itself is landed on the **py reference tier only**, and the conductor
above is what makes that honest across a mixed placement rather than silently
partial. The five other tiers keep their existing cooperative teardown and have
no E-Stop seam: under a placement halt they are killed and reported UNKNOWN by
name. The formal layer is tier-independent and already carries the verdict, so
a later tier's loop is built against the same table, and wiring one is a matter
of adding it to `revl.estop.TIERS_WITH_ESTOP` once its runtime reads the latch
and prints the `HALTED` inventory line.
