# A formalism for parallel fan-out (issue 2741)

**Status: decision note and the first theorems over the chosen relation. The
differential row, and the forward-idempotence and compensation half of the gate,
are open.**

Parallel fan-out is checked by the compiler (`src/revl/parallel.py`) and run by
the Python runtime (`backends/python/runtime.py`, `_revl_parallel`, with the join
emitted by `backends/python/emit.py`), but the Lean model does not say what a
fan-out means. The model is a sequential small-step relation
(`RevL.Lemmas.SemStep` / `SemSteps`, `formal/RevL/Lemmas/WalLemmas.lean`), chosen
for the WAL guarantees, and nothing in `formal/` or `docs/design/` named a
concurrency formalism. The one related fact,
`RevL.A8.declared_idempotent_replay_free` (`formal/RevL/Theorems/A8_WalDischarge.lean`),
is about replaying a declared-idempotent inverse over a durable log after a
crash. It says nothing about interleaving.

This note compares the candidate formalisms against the three questions in the
issue, names the one chosen, records what was tried, and says what the first
theorems do and do not cover. They are in
`formal/RevL/Theorems/ParallelFanout.lean`.

## What the choice must be able to say

The two statements the issue requires:

1. Compensation runs only when allowed.
2. A parallel fan-out is effect-equivalent to some sequential order when the
   idempotence gate admits it.

The three questions a candidate must answer:

- **Q1, the group fault case.** `docs/research/papers/P09-checked-parallel-emissions.md`
  withdraws byte-identical `accumulated` for any group that can fault or be
  diverted: a fault changes which members fired and so which compensations the
  join registers. Can the candidate state what fired and what the abort leaves
  when a member fails?
- **Q2, reuse without moving G7 and A8.** Can it be built on the existing
  `SemStep` without editing `SemStep`, the L0 teardown stack G7 is stated over
  (`formal/RevL/Semantics.lean`), or the WAL run A8 is stated over?
- **Q3, an oracle that can drive the Python fan-out.** Can a differential row
  drive `backends/python` over an enumerated schedule the way the G7 row drives
  `runtime.py` over a teardown scenario (`formal/harness/diff_corpus.py`)?

A candidate that cannot answer Q2 is a second semantics. One that cannot answer
Q3 is a paper model with no row. A wrong choice wastes a long proof effort, so
the first theorems are over the smallest group.

## Candidates

### A. A process algebra with effects and compensation

A labelled transition system in the style of CCS or CSP: parallel composition,
a fault label, bisimulation as the equivalence. It can express the fault case
(Q1).

It fails Q2. `SemStep` has no labels and no behavioural equivalence, so reusing
it means a translation from configurations to terms, and bisimulation would
replace what G7 and A8 already state about single runs. It fails Q3 without a
second translation from terms to runtime events. **Rejected:** a second
semantics with an unproved projection.

### B. An interleaving extension of the small-step relation

`ParStep` runs two member bodies against one shared world and one shared log. A
step is either member taking a `SemStep` on that shared state; a member that
cannot step (`Body.done`, `Body.fail`) does not stop the other.

Q1: yes. A failing member is `Body.fail`, which is stuck, and the other member
still fires. `par_run_is_a_sequential_run` says every interleaving that runs
both members to completion ends in the world and log of a sequential run, in
the order that puts the failing member last. The abort that follows is R4's
equation on that sequential run (`group_abort_leaves_exactly_the_emissions`).

Q2: partly. `SemStep`, the G7 stack and the A8 WAL run are untouched, and the
group is stated over `Body.witnessed` and `Body.emit` members. But the member
the gate is about is an emission that carries a compensation and a forward
idempotence declaration, and `Body` has no form for either: `Body.emit` has no
inverse, `Body.witnessed` carries only `undoIdem` (item 309), and `SemStep` has
no Phase-2 compensation drain (`WalLemmas.lean`, "Fidelity"). Stating the
idempotence conjuncts of `_group_eligible` needs a new `Body` form, which would
change `WalLemmas.lean` and every match over `Body`. The first theorems therefore
cover the inverse-bearing half of the gate only.

Q3: yes, with a caveat. The steps are the same forward effect steps the runtime
performs. The join and the compensation registration are emitted code
(`backends/python/emit.py`, the group emitter), not part of `_revl_parallel`, so
an oracle that drove thunks would re-implement the join instead of testing it.
The row must compile and run an emitted module whose body has a group, and
observe the world after the fire and after the abort. It enumerates which
members fault, not the schedule: the runtime picks the interleaving. It maps a
member with a `compensate` clause to a witnessed member and one without to an
emission, and that mapping is exactly what the row tests; the theorems do not
prove it.

**Chosen.**

### C. Graph rewriting of effect and compensation pairs

A rewriting relation over a multiset of `(effect, compensation)` pairs. Q1: yes,
the set is the state. Q2: no, it replaces the small-step relation. Q3: a
rewriting trace needs a projection to runtime events that is an unproved
mapping. **Rejected:** a second semantics with an unproved projection.

### D. A hybrid: a term language over a small-step core

Q1: only through the term surface, which repeats A's translation. Q2: no, a
second semantics stays to be synchronised. Q3: no, the surface adds a
translation with no row. It doubles the proof and review surface for no
statement the two required ones need. **Rejected.**

## What the theorems say

- **`par_run_is_a_sequential_run`.** For two single-effect members (fail, a
  witnessed mutation, or an emission), every `ParSteps` run to completion ends in
  the world and log of a `SemSteps` run of the two bodies in sequence, in plan
  order or reverse, and that run fails exactly when a member failed. It carries
  no idempotence hypothesis, and says so: single delivery of each effect makes
  the sequential witness exist whatever the gate. Where the gate matters is
  below.
- **`group_abort_leaves_exactly_the_emissions`,
  `group_abort_reverts_when_nothing_crossed`.** The abort after a group replays
  every witnessed inverse and nothing else, so the world it leaves is exactly the
  members that crossed the boundary; with no crossing it is the empty world, in
  every interleaving.
- **`over_fired_witnessed_member_is_undone`, `over_fired_emission_is_residue`.**
  The reason the gate exists. When member 1 fails, the plan-order run never
  reaches member 2, so its world is empty. The parallel run fires member 2
  anyway. If member 2 made a witnessed mutation, the abort undoes it and the two
  worlds agree; if it is a one-way emission, it stays out and the worlds differ.
  An emission with no compensation therefore leaves bounded over-delivery after a
  fault, which is the residual `P09` records (its limits section), and the gate's
  "compensation absent" half admits exactly that.
- **`both_interleavings_are_taken`.** Two witnessed members reach two different
  final worlds through two runs, so the order in the first theorem is a real
  choice.

"Compensation runs only when allowed" is not stated. An earlier draft had a
theorem of that name that was the definition of the trace it spoke about, with no
G7 verdict involved, and it was removed. Stating it against the G7 verdict needs
the compensation `Body` form above and is open.

## What was tried

- A first draft stated the theorem over an abstract effect trace (a member record
  with idempotence flags, a plan-order parallel trace and a sequential trace). It
  did not use `ParSteps`, so the interleaving was not quantified over. Its gate
  hypotheses carried weight only because the parallel side delivered every effect
  twice and the sequential side once, a duplication the runtime does not do: each
  thunk runs once, and retry is per emission, grouped or not. It also made "either
  order works" with no fault, which is false for the trace it defined. It was
  replaced by the `ParSteps` development above.
- `declared_idempotent_replay_free` was checked for direct reuse. It is a replay
  fixpoint over a durable log, not a schedule, so it cannot say which members
  fired before a fault.
- The G7 teardown model was checked for direct reuse. G7 says which registered
  entries replay under a verdict, not when a group member's forward step happens,
  and its stack is per activation, not per member.
- A process algebra was drafted as the natural first choice and rejected on Q2.
- The smallest group was chosen, so a rejection of this note discards one theorem
  file and one row.

## The oracle row this decision implies

The row, built in a follow-up slice, has the shape of G7's:

- the facts enumerate which members of a small group fault and whether each has a
  compensation, so the corpus is enumerated rather than hand-picked;
- the Lean side prints the predicted world after the run and after the abort,
  from `group_abort_leaves_exactly_the_emissions`;
- the reference side compiles and runs an emitted module with that group and
  reads the world off the runtime, so the emitted join and compensation
  registration are under test;
- the mutation that must make it fail drops a compensation from a member that
  fired and the plan-order run skipped: the model predicts residue, so a runtime
  that undoes it, or a model that does not predict it, disagrees.

## What this decision does not claim

- No forward idempotence and no compensation (item 247): `Body` has no form for
  them. The idempotence conjuncts of `_group_eligible`, and the async, shape and
  on-task-record conjuncts, are not modelled.
- No true parallelism. The runtime is one cordis event loop with cooperative
  `await` interleavings; the relation is an interleaving of steps, not a
  preemptive schedule.
- No member that lands and then faults, no divert (P09's open question 3), no
  nested groups, no group of more than two members.
- No registration order. The join registers compensations in plan order and the
  teardown replays them last-in first-out (`emit.py`, the group emitter; G7);
  with two members and an abort that replays a set, the model cannot see the
  difference.
- No byte-identical `accumulated`. P09's retraction stands.
- No claim that the Python fan-out and the model agree. That is the differential
  row.

## Follow-up slices

1. The differential row and its mutation, over an emitted module.
2. A compensation `Body` form and a Phase-2 drain in `SemStep`, which would let
   the gate's idempotence conjuncts and "compensation runs only when allowed" be
   stated. It touches `WalLemmas.lean`, so it is the owner's call.
