# A formalism for parallel fan-out (issue 2741)

**Status: decision note and the first theorem; the differential row and the
P09 update follow.**

Parallel fan-out is checked by the compiler (`src/revl/parallel.py`) and run by
the Python runtime (`backends/python/runtime.py`, `_revl_parallel`), but the Lean
model does not say what a fan-out means. The model is a sequential small-step
relation (`RevL.Lemmas.SemStep` / `SemSteps`, `formal/RevL/Lemmas/WalLemmas.lean:737`),
chosen for the WAL guarantees, and nothing in `formal/` or `docs/design/` names a
concurrency formalism. The one related fact,
`RevL.A8.declared_idempotent_replay_free` (`formal/RevL/Theorems/A8_WalDischarge.lean:295`),
is about replaying a declared-idempotent inverse over a durable log after a crash.
It supplies the idempotence instance; it says nothing about interleaving.

This note compares the candidate formalisms against the three questions in the
issue, names the one chosen, records what was tried, and fixes the shape of the
first theorem so that switching the choice costs one new theorem file and one
oracle row, not a rewrite of G7 or A8. The theorem itself is in
`formal/RevL/Theorems/G10_ParallelEmissions.lean`.

## What the choice must be able to say

The two statements the issue requires:

1. Compensation runs only when allowed.
2. A parallel fan-out is effect-equivalent to some sequential order when the
   idempotence gate admits it.

The three questions a candidate must answer:

- **Q1, the group fault case.** `docs/research/papers/P09-checked-parallel-emissions.md`
  withdraws byte-identical `accumulated` for any group that can fault or be
  diverted: a fault changes which members fired and therefore which
  compensations the join registers. Can the candidate state the fired set and
  the compensation set of a group with a failed member?
- **Q2, reuse without moving G7 and A8.** Can it be built on the existing
  `SemStep` without editing `SemStep`, the L0 teardown stack that G7 is stated
  over (`formal/RevL/Semantics.lean`), or the WAL run that A8 is stated over
  (`formal/RevL/Lemmas/WalLemmas.lean`)?
- **Q3, an oracle that can drive the Python fan-out.** Can a differential row
  drive `backends/python` over an enumerated schedule the way the G7 row drives
  `runtime.py` over a teardown scenario (`formal/harness/diff_corpus.py`,
  `teardown_observation`)?

A candidate that cannot answer Q2 is a second semantics, and one that cannot
answer Q3 is a paper model with no row. The issue's warning is that a wrong
choice wastes a long proof effort, so the first theorem must be small.

## Candidates

### A. A process algebra with effects and compensation

A labelled transition system in the style of CCS or CSP: parallel composition
with an interleaving rule, a fault label, and bisimulation as the equivalence.
It is the standard tool for exactly this shape, and it can express the fault
case (Q1): a group is a term, a fault is a label, and the fired set is read off
the trace.

It fails Q2. `SemStep` is not a labelled transition system: it has no labels, no
observability relation, and no behavioural equivalence. Reusing it means
defining a translation from configurations to terms, then relating the two step
relations, and bisimulation would replace the statements G7 and A8 already make
about single runs. It also fails Q3 without a third translation from terms to
runtime events. The strong equivalence (bisimulation) proves more than the two
required statements, so the first slice would pay for theory it does not need.

**Rejected:** a second semantics, no reuse, no direct oracle.

### B. An interleaving extension of the small-step relation

Define an additive relation over a group of member configurations: a group step
is one member taking a `SemStep`, a failed member takes no step, and a join
settles the group once no member can step. The fired set is the set of members
whose forward step was taken; the compensation set is read off the branch
results at the join, exactly as `_revl_parallel` captures them and
`_revl_raise_first` re-raises the first failure after the successful branches'
compensations are registered.

Q1: yes. A member that faults is a configuration whose body is `Body.fail`, which
is stuck. The other member can still step, so the fired set is not a prefix of
the plan order; it is exactly the set of members that stepped. The join,
modelled as a relation rather than as a function, states the compensation set
over that fired set.

Q2: yes. `SemStep` is untouched. The new relation is defined as `SemStep` on one
coordinate of a product of `Config`s, so the existing five forms, G7's teardown
stack and A8's WAL run keep their statements. The new farm is L1, imports only
L0 and the WAL farm, and the new theorem file is L2, exactly the layering the
gate enforces.

Q3: yes. The steps are the same forward effect steps the runtime performs, and
the join is the same settle the runtime's emitted join performs. A row can
enumerate the schedules and drive `_revl_parallel` with thunks that record their
own forward and compensation events, then diff the observed trace against the
model's predicted trace. No translation layer is needed.

Cost: the first theorem is over the smallest group, two members with one effect
step each. The interleaving of two one-step members is exactly the two orders,
so the proof is a two-case argument plus the idempotence collapse. It is small
enough that switching formalism costs little.

**Chosen.**

### C. Graph rewriting of effect and compensation pairs

A rewriting relation over a multiset of `(effect, compensation)` pairs: firing
adds a pair, compensating rewrites it, and a fault removes the pairs it
invalidates. Q1: yes, the set is the state. Q2: no, it replaces the small-step
relation rather than extending it; the group is two effects, so the machinery is
disproportionate. Q3: a rewrite trace needs a projection to runtime events, and
the projection is a new unproved mapping. **Rejected:** expressive but not
reusable, and not oracle-shaped.

### D. A hybrid process-algebra surface over an interleaving core

Two formalisms to keep in sync: a term language for composition and a
small-step core underneath. It doubles the proof surface and the review surface
for no statement the two required ones need. **Rejected.**

## The chosen statements

The first theorem is about the smallest group: two members, each with one
forward effect step and an optional compensation, admitted by the gate.

- A member is `fire(seq, forwardIdem, compensation?)` or `fail`. `forwardIdem`
  is the declaration the gate reads; a compensation is present or absent, and
  `compIdem` is present only when the compensation is declared idempotent.
- The gate admits a member when its forward delivery is declared idempotent and
  its compensation is idempotent or absent. This is
  `_group_eligible`'s compensation conjunct
  (`backends/python/emit.py:2193`, `_compensation_idempotent_or_absent`,
  `:2174`) stated as a predicate, not a new rule.
- Observable effects are a trace of tokens: a forward token or a compensation
  token. The model takes duplicate delivery as the worst case the gate exists
  for: a fan-out may attempt a member's forward more than once (a retry after a
  fault or a divert), and an external dispose mid-group may run a present
  compensation more than once. An idempotent forward or compensation collapses
  the duplicate token; a non-idempotent one does not.
- The parallel trace is the join-driven one: every non-failed member's forward
  token, then, if a member failed, every fired member's compensation token in
  plan order. The sequential trace in an order is the same forward tokens for
  the prefix before the first failed member, then the fired prefix's
  compensation tokens.
- **Theorem 1, compensation only when allowed.** Every compensation token in a
  run names a member that fired, and under the gate every compensated member's
  compensation is declared idempotent. A member with an absent compensation
  never produces a token.
- **Theorem 2, two-effect group effect equivalence.** For every admitted
  two-member group there is an order whose sequential trace equals the parallel
  trace. The order moves the failed member to the end, so a member that fired
  concurrently with the failure is not lost. When no member fails, either order
  works, because the idempotent forward tokens collapse to the same set.

The equivalence is a trace equality, not byte-identity of the audit surface.
P09's retraction stands: the registered set can differ from a sequential early
exit, and the theorem is about the effect trace and the compensation set, not
about `accumulated`.

The non-vacuity evidence is part of the theorem's slice, in the same shape as
`RevL.A5.a5_not_vacuous` and `RevL.G8Classified.g8_row_not_vacuous`:

- an admitted clean group, where both orders agree;
- an admitted fault group, where plan order gives a different trace and the
  order with the failed member last matches, so "some order" is load-bearing;
- a necessity witness for each gate conjunct: a non-idempotent duplicate
  forward, and a non-idempotent duplicate compensation, each make the parallel
  trace unequal to every sequential order.

## What was tried

- `declared_idempotent_replay_free` was checked for direct reuse. It is a replay
  fixpoint over a durable log, not a concurrent schedule; it supplies the
  idempotence identity the trace model uses, but it cannot say which members
  fired before a fault.
- The G7 teardown model was checked for direct reuse. G7 says which registered
  entries replay under a verdict. It says nothing about when a group member's
  forward step happens, and its stack is per activation, not per group member.
  The fan-out needs a per-member step relation, which is why the new farm is
  additive.
- A process algebra was drafted as the natural first choice and rejected on Q2:
  it is a second semantics with no bridge to `SemStep`, and bisimulation proves
  more than the required statements.
- Graph rewriting was rejected on Q2 and Q3. A rewriting trace needs a
  projection to runtime events, and that projection is an unproved mapping, not
  an oracle row.
- The smallest group was chosen for the first theorem, so a rejection of this
  note discards only the new lemma file and the new row.

## The oracle row this decision implies

The differential row, built in the follow-up slice, has the same shape as G7's:

- the fact rows enumerate `(member mode, gate flag, plan order)` over a small
  finite set, so the corpus is enumerated rather than hand-picked;
- the Lean side prints the predicted trace from the new model;
- the reference side drives `_revl_parallel` with thunks that append a forward
  event when they run and a compensation event when the join registers them.
  Unlike G7, the trace is observed off the runtime's own branch results and join
  registration, not recomputed from the source;
- the mutation that must make the row fail is built into the corpus: a
  non-idempotent forward member and a present non-idempotent compensation
  member must produce a reference trace the model admits only when its gate
  conjunct is dropped. That is the same mutation-sensitivity bar as
  `g5_row_not_vacuous` and `g8_row_not_vacuous`.

## What this decision does not claim

- No true parallelism. The runtime is one cordis event loop with cooperative
  `await` interleavings (`backends/python/runtime.py`, the pool-job-semantics
  block); the model is an interleaving of steps, not a preemptive schedule.
- No divert-aware cancellation. P09's open question 3 is untouched: a mid-group
  divert is not modelled, only a failed member at the join.
- No general n-member theorem. The first theorem is the two-effect group; the
  n-member case is an induction that is only worth stating after the two-member
  shape has been reviewed.
- No byte-identical `accumulated`. The theorem is effect-equivalence and the
  P09 retraction is unchanged.
- No claim that the Python fan-out and the model agree yet. That is the
  differential row; until it exists the decision and the Lean theorem are not an
  agreement claim about the runtime.

## Follow-up slices

1. The differential row and its mutation, driving `_revl_parallel`.
2. The P09 update that cites the theorem and narrows its "no Lean" caveat.

The theorem and its non-vacuity witnesses are in this slice, registered in
the formal gate as `RevL.Fanout.two_effect_group_effect_equiv`,
`RevL.Fanout.compensation_only_when_allowed`, and the concrete witnesses the
registry cites.
