# Schedule testing (`revl test --schedule-seed`)

*Roadmap item 295. Design: [docs/design/295-schedule-testing.md](design/295-schedule-testing.md).*

The fault sweep ([docs/fault-tests.md](fault-tests.md)) tests failure *points*:
it splices one `fail` step into a body and interrogates the wreckage. Concurrent
compositions also break for a different reason: independent lifecycle operations
*interleave* in an order the author never pictured. Schedule testing is the
sweep's sibling for interleavings. A seeded, deterministic scheduler drives one
composition through many orderings of the same operations and, for each ordering,
checks the properties only concurrency can violate.

```
revl test app.rvl --schedule-seeds 200     # sample 200 interleavings + the baseline
revl test app.rvl --schedule-seed 48192    # replay exactly one, from its seed
```

## Why this is deterministic, not flaky

The cordis-py reference runtime is deterministic by construction: no wall-clock,
no threads, every wait an explicit cooperative turn, and time is a coeffect the
harness advances rather than a clock the runtime reads. Once the *order* in which
the harness issues lifecycle operations is pinned, the whole run is reproducible
byte for byte.

A **seed is the tape a deterministic chooser reads at every scheduling
decision.** At each point where more than one operation is ready, the chooser
asks a seeded PRNG for an index into the canonically-sorted ready set and runs
that operation. The sequence of chosen indices *is* the schedule, so the same
seed replays the identical interleaving. `--schedule-seed 48192` seeds that
chooser; `--schedule-seeds N` samples `N` seeds as a random walk over the
schedule space, plus the canonical sequential baseline.

The ready set is sorted by `(operation-kind rank, fiber load-order index)` so
that "index 0" names the same operation on every replay. Load order is the same
`ir.manifest.loadOrder` the fault driver already brings a composition up in.

## The interleaving alphabet (v1)

The schedulable atoms in this version are the two lifecycle operations the
harness drives directly and deterministically, with no dependence on event-loop
task ordering:

| atom | what it does | when it is ready |
| --- | --- | --- |
| `activate(F)` | bring fiber `F` up on a real `cordis.Context` | `F` is not up yet and every provision it `requires` is already provided |
| `teardown(F)` | unload a live fiber `F` | `F` is up and every component that depends on a provision `F` publishes is already torn down |

The teardown gate keeps every explored schedule a *valid* reverse of the
dependency order: withdrawing a provision a live consumer still needs is not an
interleaving, it strands the consumer, so it is not a schedule this sweep
explores. Independent fibers have no dependents, so their activation and teardown
order stays fully free. That freedom is exactly what the seed explores:
concurrent activation and interleaved teardown, which is where the properties
below break.

The finer intra-activation turns the design names (`resume` of an in-flight
`await` step, `complete` of a `Job`, `fire` of a due timer) and per-fiber
capability (property 3) are the documented follow-on. See the design doc's
decision 6 and open questions.

## The checked properties

Each property is a decidable predicate over a *completed* schedule (every fiber
settled and torn down), and each reuses an oracle that already exists on the
fault path. A schedule passes iff all hold. A failing schedule is minimized and
reported.

1. **Residue-free.** After the schedule completes, the four `fault.py::_snapshot`
   deltas are zero (registry, provisions, effect stack, listeners) and every
   host resource acquired during the run was released
   (`_unreleased_host_resources`). This is the sweep's `no-residue` assertion,
   evaluated at end-of-schedule instead of end-of-single-activation.
2. **No deadlock.** If the ready set is empty yet some fiber never activated
   because a provision it requires can never be published, the schedule is
   deadlocked and the blocked fibers are named. A clean schedule always drains
   its ready set to empty with every fiber settled.
3. **Stable final state.** The observable end state must not depend on the
   interleaving. The fingerprint is the `_snapshot` tuple plus the *ordered*
   emission multiset. Every schedule that commits must produce the same
   fingerprint as the canonical sequential run (the seed that always picks index
   0). A divergent fingerprint is a finding: the composition has an
   order-dependent result its declarations did not admit. Emissions are the
   order-sensitive observable, because an emission is an irreversible boundary
   crossing whose order is visible downstream; teardown order is not observable
   this way for a correct composition, so it does not enter the fingerprint.
4. **No use-after-withdrawal.** Order every provision-withdrawal and every use of
   a provision on the schedule timeline; a use placed after the withdrawal of the
   same provision is a violation. In v1 an activation only runs against live
   providers, so uses precede withdrawals by construction; the check scans the
   recorded timeline so a future finer-grained interleaving cannot silently
   regress it.

## Minimization and reproduction

A found-bad schedule is stored as its choice vector and minimized by
delta-debugging over the choices, the same idea `fault.py::_shrink_args` applies
to a failing property-test input. Minimization prefers the canonical index-0
atom at each decision, which collapses an interleaving toward the sequential
run, and keeps any shortened vector that still triggers the same property failure
with the same signature. The minimized vector is the reproduction: the dossier
prints the shortest choice sequence that exposes the issue, and
`--schedule-seed <seed>` replays it.

For example, the two-consumer composition below reports a minimized `[0, 1, 0,
0, 0, 0]`: a single non-canonical choice (activate `B` before `A` at the one
decision where both are ready) flips the emission order, and every other choice
collapses back to the baseline.

```revl
service Log { emission fn write(line: Str) }
component Sink provides log: Log { provide log { fn write(line) { } } }
component A requires log: Log { emit log.write("A up") }
component B requires log: Log { emit log.write("B up") }
```

## Where it runs, and when it skips

Schedule testing runs on the **py reference tier only**, for the same reason the
fault sweep does: py is the only tier that executes lifecycle activations for
real and the only tier with the deterministic cooperative substrate the
determinism claim rests on. `--backend` does not apply; a note is printed if one
is passed.

Because it activates components for real, a missing cordis-py runtime is a *skip
with a reason*, never a pass, exactly as `--sweep` handles it:

```
sh backends/python/setup.sh
revl test app.rvl --schedule-seeds 200        # the documented happy path (the
                                              # setup script installs `revl` on
                                              # PATH; issue #317 / #336)
```

Where the absolute-interpreter fallback is needed (a venv's `python` is
required by the call), use `backends/python/.venv/bin/python -P -m revl test …
`. `-P` is the safety bit: without it, `-m` puts the CWD at `sys.path[0]`
and a sibling `.py` in a composition's directory can shadow a real
runtime import (issue #317).

## Relation to other items

- **Distinct from item 259 (parallel emission).** 259 finds the parallelizable
  partition of an emission sequence and fans it out for *latency*, asserting
  byte-identical results. 295 takes operations that run concurrently and reorders
  them on purpose, to *test* that the ordering guarantees survive. 259 makes it
  faster; 295 proves the speed did not buy a wrong answer.
- **Checks item 243, does not own it.** The transactional-vs-bracket teardown
  contract (item 243) is read, not reimplemented: schedule testing stresses that
  contract under interleaving through the residue and teardown properties.
- **Feeds item 308 (ownership modes).** A shared-witnessed-resource ambiguity a
  schedule surfaces is tagged `ownership?` and routed to the item 308 design
  pass; schedule testing produces the minimized interleaving that proves the
  ambiguity is real, it does not resolve ownership itself.
