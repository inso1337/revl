# Design: deterministic concurrency / schedule testing (item 295)

`revl test --schedule-seed 48192`

## What this item is, in one line

The fault sweep (item 30, `src/revl/fault.py`) tests failure *points*: it splices
one `fail` step into a body and interrogates the wreckage. Concurrent
compositions also break because independent operations *interleave* in an order
the author never pictured. Item 295 is the sweep's sibling for interleavings: a
deterministic scheduler drives one composition through many orderings of the
same operations and, for each ordering, checks the same paradigm properties the
sweep checks plus the ones only concurrency can violate (deadlock,
use-after-withdrawal, unstable final state).

It is **distinct from item 259**. Item 259 finds the parallelizable partition of
an emission sequence and fans it out for *latency*, asserting byte-identical
results (its exit test). Item 295 takes the operations 259 (or a hand-written
concurrent composition) runs concurrently and reorders them on purpose, to
*test* that "provably independent" really is independent and that teardown,
capability, and residue guarantees survive every ordering. 259 makes it faster;
295 proves the speed did not buy a wrong answer.

## Why the reference tier already makes this tractable

The py reference runtime is deterministic by construction. Its own docstring is
explicit: the Pool/Job substrate is "dependency-free and deterministic, no
driver, no wall-clock, no threads, no timers. Every wait is an explicit, bounded
number of cooperative scheduler turns" (`backends/python/runtime.py`, the
pool-job-semantics block). Three facts fall out of that and are the whole reason
295 is buildable rather than a research project:

1. **Suspension points are few and named.** A fiber or job only ever yields at a
   handful of places: `JobHandle._drive` suspends once per remaining tick at
   `await asyncio.sleep(0)`; an `await` step in a component body is the A1
   boundary (item 106 `Async[T]`); a `Clock.advance(ms)` firing runs a timer
   body as a discrete timeline step. Between two suspension points the runtime
   never preempts, so a run-until-next-suspension segment is atomic and needs no
   sub-splitting.
2. **Time is a coeffect, not a clock.** `Clock.now()` only moves when the harness
   calls `Clock.advance` (`backends/python/runtime.py`, the Clock class). A
   firing is already "a deterministic step in the timeline, never a race". The
   scheduler owns `advance`, so it owns when timers fire relative to fibers.
3. **The runtime is already introspectable at exactly the granularity we check.**
   `fault.py::_snapshot` reads the four teardown counters (registry, provisions,
   effects, listeners); `_unreleased_host_resources` pairs acquire/release from
   the host trace; `FaultProbe` records per-fiber inverse accumulation vs run
   order. 295 reuses these verbatim as its property oracles.

## The interleaving alphabet (decision 1)

The schedulable atom is a **fiber turn**: one fiber (or timer firing) runs from
its current suspension point until its next suspension point or terminal state.
The scheduler chooses *which* ready atom takes the next turn; it never chooses a
point *inside* an atom, because the cooperative runtime cannot be observed there.

Concretely, the alphabet symbols, and where each already exists in the runtime:

| symbol | what it is | runtime seam |
| --- | --- | --- |
| `activate(F)` | run fiber `F` from LOADING to its first `await` or to ACTIVE | `runtime.plug` / `spawn` |
| `resume(F)` | resume a fiber blocked on an `await` step, once its awaited value is ready | the `await` A1 boundary (item 106) |
| `complete(X)` | deliver an async-extern / `Job` completion to whoever awaits it | `JobHandle._drive`, one tick per turn |
| `fire(T)` | run one due timer body | `Clock.advance` firing loop |
| `teardown-turn(F)` | run the *next* inverse in `F`'s LIFO drain | `Frame.drain`, `reversed(adopted)` |

Two granularity decisions, each grounded so the space neither explodes nor goes
blind:

- **Not finer than a turn.** We do *not* schedule individual pure expression
  steps. Pure steps cannot observe each other's effects (this is exactly item
  259's disjoint-capability independence, read backwards), so reordering them
  only inflates the state space with orderings that are observably identical.
  A `Job`'s five ticks are one `complete(X)` decision, not five, unless a body
  actually observes an intermediate tick.
- **Not coarser than a turn.** We do *not* treat a whole emission-plus-teardown
  as atomic. The bugs 295 exists to find (a teardown that interleaves with a
  sibling's still-running body, an inverse that fires after a provision it
  depends on was already withdrawn) live *between* a fiber's turns. Emissions,
  async completions, and individual teardown inverses are therefore each their
  own symbol.

The alphabet is closed: every point at which the reference runtime can suspend
or resume is one of the five symbols above, and Slice 2a of item 243 adds no new
suspension point (it changes *what a teardown turn does*, per the entry kind,
not *when a turn can happen*).

## Which tier the scheduler instruments (decision 2)

**Py, and only py, in v1.** The rationale is not "py is easiest"; it is that py
is the only tier where this is currently *correct and cheap*:

- Py is the reference runtime and the only tier that executes fault tests at all.
  `fault.py::_load_py_tier` loads the py runtime; `test.py::RUNNERS` routes every
  execution-based check there. 295 is another execution-based check, so it lands
  where its oracles already live.
- Py already has the deterministic cooperative substrate (Job ticks, Clock
  advance) described above. TS has an analogous model but a different event loop;
  rust/java/wasm bodies are further from it, and wasm has no async at all (it
  refuses timers and streams). Reproducing the scheduler on five more runtimes is
  the item's follow-on, not its v1.
- Py is the tier item 243 Slice 2a is editing this week. Instrumenting the same
  tier means the two efforts touch one runtime loop, so the coordination below is
  a single conversation rather than a six-way one.

### Coordination with item 243 Slice 2a (the non-negotiable note)

Slice 2a rewrites the py teardown seam: `Frame.drain` (and the emitted teardown
loop that feeds it) gains a second accumulator **entry kind**, `transactional`,
whose replay is abort-only and whose commit path discharges the inverse and GCs
the witness, sitting beside the existing `bracket` entry (item 243 design doc,
"Slice 2"). Item 295 instruments the *same* `Frame`/drain region, one layer out:
it decides *when* a fiber's drain runs relative to other fibers, while Slice 2a
decides *what each drained entry does*. The design rule that keeps them from
fighting:

1. **295's py prototype lands after Slice 2a, never before.** The design (this
   doc) is runnable now; the code waits. This is already the roadmap sequencing
   ("Design-doc runnable now; the py prototype lands after Slice 2a").
2. **295 reads `entry_kind`, it never reimplements teardown.** The `teardown-turn`
   symbol advances `Frame.drain`; the property oracle (decision 4, LIFO + correct
   teardown) reads each entry's `entry_kind` descriptor (`bracket` vs
   `transactional`) that Slice 2a lands on the IR node, and applies the matching
   expectation: a bracket entry must replay on both clean unload and abort; a
   transactional entry must replay only on abort and must be discharged (not
   replayed) on commit. 295 checks Slice 2a's contract under interleaving; it does
   not own the contract.
3. **The instrumentation seams are orthogonal and both single-slot.** Slice 2a
   changes the body of the drain loop. 295 adds a scheduler-decision hook modeled
   on `arm_fault_probe`/`disarm_fault_probe` (process-global, single-slot, visible
   when left armed) that gates *which* ready atom runs next. Neither edits the
   other's lines. Before the 295 prototype starts, confirm with the Slice 2a
   author that the drain loop still exposes one advance-one-inverse step the
   `teardown-turn` symbol can drive; if Slice 2a collapses the loop into an atomic
   all-at-once drain, 295 needs a one-line re-entrant hook and that is the only
   code request 295 makes of 243.

## Seed to schedule, minimization, replay (decision 3)

**A seed is the tape a deterministic chooser reads at every scheduling decision.**
At each point where more than one alphabet atom is ready, the scheduler asks a
seeded PRNG for an index into the sorted ready set and runs that atom. The
sequence of chosen indices *is* the schedule. Because the runtime under it is
already deterministic (no wall-clock, no threads), the same seed replays the same
choice sequence and therefore the identical interleaving, byte for byte. This is
the whole meaning of `--schedule-seed 48192`: it seeds the chooser.

```
schedule := [ choice ]                 # one entry per decision point
choice   := index into the sorted ready set at that point
seed     -> PRNG -> choice*            # deterministic, replayable
```

Determinism requires a **canonical ready-set order** so that "index 0" means the
same atom on replay. The order is `(atom-kind rank, fiber load-order index,
serial)`: load order comes from `ir.manifest.loadOrder` (the order `fault.py::
_drive` already brings a composition up), and `serial` is the monotonic id the
runtime already stamps on Job/Timer handles. No new identity scheme.

**A found-bad schedule is stored as `{seed, choices, first_divergence}` and
minimized by delta-debugging over `choices`.** The project already owns the
shrinking machinery: `fault.py::_shrink_value` / `_shrink_args` shrink failing
property-test inputs to a minimal witness. Minimization here is the same idea over
the choice vector: repeatedly drop or coalesce choices (prefer running the same
fiber twice in a row, which collapses an interleaving toward a sequential run)
and keep any shortened vector that still triggers the same property failure at the
same fiber/step. The minimized vector is the reproduction: `revl test
--schedule-seed <seed>` replays it, and the dossier prints the shortest choice
sequence that exposes the bug, exactly as the sweep prints the offending step.

**Relation to the WAL / replay machinery.** The accumulator is already a
write-ahead log: `backends/python/replay.py::WriteAheadLog` persists each effect
as it commits, and `src/revl/recovery.py` reads it back to roll forward or back
around the `activation-complete` marker. 295 does not need a new persistence
format for the *effects*; a schedule's replay reuses the same record vocabulary
(`run.py --record`, the Recorder in `mcp/session.py`). What 295 adds on top is the
thin `{seed, choices}` cargo, stored beside the WAL, that pins the *ordering* the
WAL alone does not capture. A bad schedule is thus reproducible with the same
tools a crash is: the WAL says what committed, the choice vector says in what
interleaved order.

## The checked properties (decision 4)

Each property is a decidable predicate over a *completed* schedule (all fibers
settled and the composition torn down), and each reuses an oracle that already
exists for the fault path. A schedule passes iff all hold; a failing schedule is
minimized and reported.

1. **Residue-free.** After the schedule completes and every fiber is disposed,
   `fault.py::_snapshot` deltas are all zero: registry size, provisions, root
   effect-stack length, and listener counts are back to the pre-activation
   baseline, and `_unreleased_host_resources(trace)` is empty (every `Map.new` /
   `Pool.open` matched a `drop` / `close`). This is the sweep's `no-residue`
   assertion, evaluated at end-of-schedule instead of end-of-single-activation.
2. **No deadlock.** Decidable at every decision point, not only at the end: if the
   ready set is empty, no timer is due (`Clock.pending` counts live timers and
   none has `next_at` reachable), yet some fiber is non-terminal (still LOADING or
   blocked on an `await`, and `Job.pending() > 0` for its awaited work with no
   atom able to complete it), the schedule is deadlocked. The scheduler halts and
   reports the blocked cycle. A clean schedule always drains its ready set to
   empty with every fiber terminal.
3. **No capability violation.** Every emission in the trace was performed by a
   fiber that holds the capability for it. The runtime records emissions with the
   acting component (`run.py::_record_emit`); the oracle checks each against that
   component's admitted capability set (the same policy surface the item-33 gate
   uses). The property exists to catch an interleaving that lets one fiber's
   in-flight state be observed or driven through another's authority. See open
   question 3 on whether this needs a runtime-time hook or is already enforced at
   emit.
4. **Stable final state.** The observable end state must not depend on the
   interleaving. For a batch of seeds, compute the final fingerprint (the
   `_snapshot` tuple plus the ordered emission multiset from the trace) of each
   completed schedule; every schedule that *commits* must produce the same
   fingerprint as the canonical sequential run (seed that always picks index 0).
   A divergent fingerprint is a finding: the composition has an order-dependent
   result its declarations did not admit. Where item 259 declares two operations
   independent or `commutative`, that declaration is the equivalence relation the
   comparison uses (see open question 2).
5. **Correct teardown order (LIFO within a fiber, valid order across fibers).**
   Within one fiber, `FaultProbe.lifo_violation()` already decides that inverses
   ran in exact reverse of accumulation. Across fibers, teardown must be a valid
   reverse of the dependency/load order (`_drive` tears down `reversed(order)`
   today). Under interleaving the oracle checks both: each fiber's own drain is
   LIFO, and no fiber's inverse ran before an inverse that a later-loaded fiber
   depended on. Entry kind (item 243 Slice 2a) refines the expectation per entry:
   `bracket` replays on clean unload and abort; `transactional` replays only on
   abort and is discharged on commit. A transactional entry that replayed on a
   clean commit, or a bracket entry that was skipped, is a violation.
6. **No use-after-withdrawal.** Order every provision-withdrawal event and every
   use of that provision (an emission or method call through it) on the schedule's
   timeline. A use that the schedule placed *after* the withdrawal of the same
   provision is a violation: the consumer reached a provider that had already torn
   down. The events are in the trace and the reactive coeffect (the R2/R3
   provider-withdrawal path the runtime already runs, `run.py`); the oracle is a
   per-provision happens-before check over the recorded order.

## Interaction with items 243 and 308 (decision 5)

- **243 (transactional vs bracket teardown), checked, not owned.** Property 5
  above is exactly the check that Slice 2a's two entry kinds behave correctly when
  their drains interleave with other fibers. 295 is the concurrency stress test
  for 243's teardown contract. It reads `entry_kind` and asserts the matching
  replay/discharge behavior; it never decides what that behavior is.
- **308 (effect ownership modes) findings are evidence, not scope.** When two
  fibers hold a witness (item 243) to the *same* shared host resource and their
  teardowns interleave, "who runs the final inverse" is ambiguous: run it twice
  and the second is a use-after-withdrawal or a double-free; run it never and it
  is residue. 295 will surface these as failures of property 1, 5, or 6. The
  design position is firm: **295 reports such a shared-witnessed-resource finding
  as evidence for the item 308 design pass (owned / borrowed / shared), and does
  not attempt to resolve ownership itself.** Resolving it needs the ownership type
  system 308 proposes; 295's job is to produce the minimized interleaving that
  proves the ambiguity is real. The dossier tags these findings `ownership?` and
  routes them to 308.

## Scope of v1, and the follow-on (decision 6)

**v1 is a bounded first cut on one tier.**

- Single tier: py, per decision 2.
- Bounded interleaving space: **sampled, not exhaustive.** v1 runs `K` seeds
  (default a few hundred, `--schedule-seeds N` to raise it) as a random walk over
  the schedule space, plus the canonical sequential seed as the property-4
  baseline. It does not enumerate the full state space or apply partial-order
  reduction. Bounds: a cap on concurrent fibers and on total turns, so a
  pathological body cannot run unbounded; over-bound bodies report `unbounded`
  loudly, the way item 260 reports unbounded crossings.
- Core properties: residue-free, no deadlock, stable final state, correct
  teardown order, no use-after-withdrawal. Capability violation ships in v1 if the
  runtime already enforces per-fiber capability at emit; otherwise it is the first
  follow-on (open question 3).
- Delivery surface: `--schedule-seed <seed>` (replay one) and `--schedule-seeds
  <N>` (sample N) on the existing `test` subparser (`src/revl/cli/parser.py`,
  the `test` parser at the `--sweep` sibling), routed through a new
  `sweep_command`-shaped entry in `src/revl/test.py`, with a new
  `src/revl/schedule.py` holding the scheduler and reusing `fault.py`'s snapshot,
  probe, and shrink helpers. A missing cordis-py runtime is a skip-with-reason,
  never a pass, exactly as `sweep_command` handles it.

**Follow-on, explicitly out of v1.**

- Exhaustive / partial-order-reduced state-space exploration (DPOR): replace the
  random walk with systematic enumeration that prunes equivalent interleavings.
  This is the story that turns "we sampled and found nothing" into "we proved no
  interleaving violates the properties" for bounded compositions.
- The other five tiers, in the item-80/115 async-rollout order, wasm last (or
  refusing, since it has no async).
- Budget-aware and lease-aware scheduling once items 260/294 land, so the
  scheduler can also stress ordering against resource ceilings and expiring
  capabilities.

## Slice plan

- **Slice A (this doc, now).** Model, alphabet, tier, seed mapping, properties,
  243 coordination, open questions.
- **Slice B (py prototype, after item 243 Slice 2a lands).** `src/revl/
  schedule.py`: the seeded scheduler over the five-symbol alphabet, the
  `--schedule-seed` / `--schedule-seeds` wiring, the K-seed sampler, the canonical
  sequential baseline, and properties 1, 2, 4, 5, 6 reusing `fault.py` oracles and
  reading item 243's `entry_kind`. Skip-with-reason without cordis-py.
- **Slice C.** Minimization (delta-debug over the choice vector, reusing the
  shrink helpers) and a sweep-shaped dossier that prints the minimized bad
  schedule, tags `ownership?` findings for item 308, and states an honest verdict
  (passed / found / bounded-out).
- **Slice D (follow-on).** Property 3 as a runtime hook if needed, partial-order
  reduction, then the other tiers.

## Open questions for the user

1. **Sampled vs exhaustive for the headline claim.** v1 samples K seeds and
   minimizes any failure. Is random sampling plus minimization enough for the
   paper's concurrency claim, or do we want bounded exhaustive enumeration with
   partial-order reduction promoted into v1 (larger, but yields "no interleaving
   violates" rather than "we did not hit one")? The recommendation is: ship
   sampling first, because it finds real bugs cheaply and its minimized
   reproductions are the compelling artifact; add DPOR as the follow-on that
   upgrades the verdict.
2. **The oracle for stable-final-state under legitimate nondeterminism.** Property
   4 compares each schedule's fingerprint to the sequential baseline. When a
   composition is *legitimately* order-independent-but-not-identical (two
   `commutative` same-key emissions, item 259 Def. 39), equality is the wrong
   relation. Do we gate property 4 on item 259 having landed its
   commutativity/independence declarations, and use those as the equivalence
   relation, or ship property 4 v1 as "identical to sequential" only and refuse to
   judge bodies that declare commutativity? Recommendation: ship the strict
   equality check first, and have it *skip with a reason* on any body that carries
   a commutativity declaration, so it never reports a false finding while 259 is
   in flight.
3. **Is per-fiber capability enforced at emit time, or only at admission?**
   Property 3 (no capability violation) is decidable from the trace only if the
   runtime attributes each emission to a fiber and its capability set at emit time
   (`run.py::_record_emit` records the component; the question is whether the
   capability check runs then or only at the item-33 admission gate). If it is
   admission-only, property 3 needs a small runtime hook and moves to Slice D. Can
   you confirm which, or should Slice B probe it?
4. **Does cordis-py expose a "blocked-on-await" fiber state distinct from a
   settled ACTIVE?** Deadlock detection (property 2) needs to tell a fiber that is
   *done and idle* from one that is *parked awaiting a completion that no atom can
   deliver*. `Job.pending()` and `Clock.pending()` count in-flight work, but the
   fiber-level await state may need a probe modeled on `FaultProbe`. If there is no
   such state today, Slice B adds a `schedule_probe` beside `arm_fault_probe`; flag
   if you would rather that land in the runtime proper.
5. **The one code request 295 makes of item 243 Slice 2a.** The `teardown-turn`
   symbol needs `Frame.drain` to expose an advance-one-inverse step (it currently
   drains `reversed(adopted)` in a single async loop). If Slice 2a keeps a
   per-inverse step, 295 drives it as-is; if Slice 2a collapses the drain, 295
   needs a re-entrant hook. This is worth a two-line confirmation with the Slice
   2a author before Slice B starts, and is the only place the two efforts touch
   the same lines.
