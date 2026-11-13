---
id: P09
title: Deriving Safe Concurrency From Capability Declarations: Checked Parallel Emissions
theme: formal-pl
kind: short
maturity: partial
status: Checker and Python runtime fan-out ship on main with tests; the TypeScript runtime fan-out and divert-aware cancellation do not exist, and there is no workload evaluation.
fused_with:
---

## Pitch

A checker can read a safe parallel schedule for an agent's tool calls off the capability declarations it already has, and when it cannot prove independence the checker's own outcome is no speedup. Two trust assumptions (declared disjointness and honest `commutative` declarations) can still give a wrong answer if false, and fault or divert paths change teardown effects, so the claim is bounded by those residuals.

## Abstract

Agent harnesses can speed up multi-tool turns by running tool calls concurrently, and the decision of which calls are safe is typically a convention or absent (no survey of harnesses was done). We show that a language that already tracks the capability set of every external crossing can derive that decision. In revl, the checker partitions each straight-line run of emissions into groups whose members have declared-disjoint capabilities, or share a key that is declared commutative. Anything it cannot prove independent stays sequential, so the checker's own failure mode is lost speed. The residual trust assumptions named below can still produce a wrong result if they are false. A Python runtime fans a group out under one event loop, buffers each branch's audit entries, and replays them in plan order at the join, so a clean run produces the same result and audit trace as the sequential form in the tested shapes; in the test suite three emissions with a 0.15 s stubbed latency finish in under two latencies and under 0.7 of the sequential time. The design was reviewed adversarially twice. The reviews retracted the original claim that teardown state is byte-identical to a sequential run: a fault or a deadline divert changes which members have fired, so the claim was weakened to teardown-effect equivalence, enforced by admitting a member to a group only when its compensation is idempotent or absent. We report the derivation, the retraction, what the tests establish, and the residuals the design leaves open (hidden shared host resources, dishonest commutativity declarations, no divert-aware cancellation). We also describe a second, older use of the same argument: boot concurrency bounded by dependency-graph depth.

## Problem and claim

Concurrency in agent loops is decided by convention. A batch list that says "these tools are safe together" is not checked against what the tools do, and the failure it hides (a reordered side effect, an interleaved audit log, a compensation that runs for a call that never completed) is silent. Effect systems and deterministic parallelism give static guarantees, but they ask the programmer to annotate for parallelism.

The claim is narrower. If every external crossing already carries a declared capability set (needed for authority checks, not for speed), disjointness of those sets is a sufficient condition for firing the crossings concurrently, provided the runtime also keeps the audit trace and teardown deterministic. No new syntax is involved; the only declaration used for parallelism is `commutative`, which exists for recovery ordering (`docs/design/259-checked-parallel-emissions.md:3-12`).

The second claim is about honesty of the guarantee. The first version of the design promised a byte-identical teardown accumulator. Two adversarial reviews showed that cannot hold for any group that can fault or be diverted, and the design says so and replaces it (`docs/design/259-checked-parallel-emissions.md:23-52`).

## Contributions

1. A partition algorithm over each straight-line emission run: groups grow only while every new member is compatible with every member already in the group, same-key pairs group only if `commutative`, and any step whose callee reach includes the unnameable `*` marker is a hard barrier (`src/revl/parallel.py:288` `parallel_plan`; `tests/test_parallel_259.py:105`, `:133`).
2. A runtime fan-out whose audit is deterministic: per-branch entry sinks held in a context variable stack, replayed in plan order at the join (`backends/python/runtime.py:2091` `_revl_parallel`, `:2106` `_revl_flush`).
3. A fail-safe admission gate for the fan-out: forward delivery idempotent, compensation idempotent-or-absent, audit entries produced on the awaiting task, not an approval or deferred or validated emission (`backends/python/emit.py:1659` `_group_eligible`; negative end-to-end test at `backends/python/tests/test_parallel_259_runtime.py:358`).
4. A documented retraction: byte-identical teardown state is withdrawn and replaced by teardown-effect equivalence, with fault, divert and external-dispose tests (`backends/python/tests/test_parallel_259_runtime.py:438`, `:489`, `:511`).
5. The same independence argument applied to boot: components activate as soon as their real prerequisites are done, so boot latency follows graph depth, and teardown reverses the completion order (`docs/parallel-activation.md`; `tests/test_parallel_activation.py:82`, `:117`, `:145`).

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Cilk, Rayon (work-stealing data parallelism) | Programmer marks parallel regions; runtime schedules. Safety is by discipline or by borrow rules (Rayon). | No marking. The partition is derived from capability declarations on external crossings, and the audit trace and teardown order are part of the contract. |
| Deterministic Parallel Java (Bocchino et al., OOPSLA 2009) (citation to be checked) | Region and effect annotations prove two tasks do not interfere. | Same shape of proof (disjoint effects), but the effects are the capabilities the program already declares for authorisation, and the target is external side effects, not heap regions. |
| Koka and other effect-typed languages | Effect rows separate pure from effectful and permit reordering of pure parts. | Treats effectful host crossings as parallelizable when their declared capabilities are disjoint, and handles compensation and divert, which pure reordering does not face. |
| Software transactional memory, Haskell STM | Optimistic concurrency with rollback on conflict. | No speculation or rollback of the concurrent calls themselves. Independence is proved before firing; where it is not proved, the group is a singleton. |
| Saga-style compensation (Garcia-Molina and Salem 1987) (citation to be checked) | Compensation runs for completed steps after a failure. | Shows the specific hazard of parallel sagas: a fault changes which steps completed, so the compensation set differs from sequential. The gate (idempotent-or-absent compensation) is the answer. |
| Agent frameworks with parallel tool calls | Run all tool calls in a model turn concurrently, or follow a safe-list. (Unverified: no framework was examined.) | Parallelism is decided by a checker, not by the framework or the model, and a dependent pair keeps its order. |

No empirical comparison with these systems exists; the table is positioning only.

## Evidence on main

| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Checker derives a partition from disjoint caps and `commutative` | `src/revl/parallel.py:288`; `tests/test_parallel_259.py:99-142` | shipped | module docstring states "the feature's worst case is 'no speedup', never a wrong result" (`src/revl/parallel.py:17-18`); this is the claim the residuals C3/C4 qualify, so it is quoted as the design's intent, not as a proof |
| First-class arrow is a barrier (hidden crossing not reordered around) | `src/revl/parallel.py` (barrier B in docstring); `tests/test_parallel_259.py:105` | shipped | the design calls it HIGH-2 of the second review (design doc:92) |
| Group grows pairwise against the whole group | `tests/test_parallel_259.py:133` | shipped | closes the a-b-c chain hole |
| Plan visible on the audit surface; byte-identical when no group | `tests/test_parallel_259.py:153`, `:163`, `:169`; `src/revl/audit_diff.py:98` | shipped | |
| py runtime fan-out, plan-order audit replay | `backends/python/runtime.py:2091`; test at `backends/python/tests/test_parallel_259_runtime.py:207` | shipped | timing assertion uses stubbed 0.15 s latency |
| Admission gate incl. compensation idempotent-or-absent | `backends/python/emit.py:1640`, `:1659`; tests at `backends/python/tests/test_parallel_259_runtime.py:274-358` | shipped | |
| Fault and divert are teardown-effect equivalent | `backends/python/tests/test_parallel_259_runtime.py:438`, `:489` | shipped | `accumulated` byte-identity explicitly not claimed |
| External dispose mid-group leaves bounded idempotent over-delivery | `backends/python/tests/test_parallel_259_runtime.py:511`; module docstring | shipped, with stated residual | divert-aware `_revl_parallel` deferred (design doc:989, open question 3) |
| Two-member group over the shipped `SemStep` (Lean) | `formal/RevL/Theorems/ParallelFanout.lean` (`RevL.Fanout.par_run_is_a_sequential_run`, `group_abort_leaves_exactly_the_emissions`, `over_fired_emission_is_residue`); `docs/design/2741-parallel-fanout-formalism.md` | proved for two single-effect members; no differential row yet | every interleaving is a sequential run with the failing member last; the abort leaves exactly the one-way emissions, so a member the plan-order run skips is undone only if it has an inverse; forward idempotence and compensation are not modelled |
| ts runtime fan-out | design doc slice 3, "Slice 3 - typescript runtime fan-out" | designed | a search of `backends/typescript` for the fan-out finds nothing; the selfhost emitter emits an UNSUPPORTED marker for a fan-out body (`selfhost/emit_py.rvl:3655`) |
| Disjointness of same-token sibling cones (D2) | design doc:249-258 | designed | gated off |
| Parallel activation by DAG depth | `src/revl/activation.py:108` (`activate_concurrent`), `:171` (`teardown_lifo`); `tests/test_parallel_activation.py:82-198` | shipped (py tier only) | non-Python tiers keep sequential runners (`docs/parallel-activation.md`) |

Line references written as "design doc:N" are to `docs/design/259-checked-parallel-emissions.md`.

## Numbers that exist

- Test timing: three emissions, stubbed delay 0.15 s each; assertion is parallel < 2 x delay and parallel < 0.7 x sequential (`backends/python/tests/test_parallel_259_runtime.py:207-233`). n = 1 run per CI invocation, no distribution reported. These are pass/fail thresholds, not measurements.
- The roadmap entry for the feature reports the slice 2 merge commit and that three disjoint emissions run in "~max not sum" (`docs/roadmap-archive/12-post-v2-0-horizon-strategy-review.md`, item 259 entry). No measured latencies are given.
- Review count: self-review found 2 critical findings, the independent review a third (same roadmap entry; design doc:23-52 and section 7).
- None yet: no real-host latency, no speedup on any agent workload, no fraction of real tool-call sequences that the checker partitions into groups of size greater than one.

## Paper outline

1. Introduction: latency in agent turns, why convention-based batching is unchecked.
2. Background: capability sets as a partial order, `commutative`, straight-line runs of emissions.
3. Derivation: independence, same-key commutativity, pairwise grow, barriers.
4. Runtime: fire, buffer, join in plan order; audit entry sinks; the admission gate.
5. The retraction: what review found, why byte-identical teardown cannot hold, what replaced it.
6. Evaluation: tests as proof obligations, timing, workload study (to be done).
7. Related work, limits, residuals.
8. Appendix or short section: depth-bounded boot as the same argument.

## Threats to validity and limits

- The design doc says the disjointness proof is only as good as the declarations: "declared-tokens-only disjointness" is an accepted open residual (design doc:115, :781). A host call with a hidden shared resource behind two disjoint capability names breaks the argument.
- The commutative argument holds "under the single-loop no-true-parallelism model" (design doc:309, :833). A threaded runtime could not use it.
- Byte-identical `accumulated` is withdrawn for any group that can fault or be diverted (design doc:30-41). The clean-commit path still has it.
- Divert-aware cancellation is not implemented; an external dispose can leave idempotent over-delivery (`backends/python/tests/test_parallel_259_runtime.py` module docstring).
- Straight-line runs only; no parallelism across `if` arms or loops (design doc:979).
- The timing test is a threshold on a stubbed latency, not a measurement on a real host.
- Activation concurrency is py-only, and the formal permission for concurrent resolution of disjoint keys is "flagged, not decided" (`docs/parallel-activation.md:88`).
- The Lean model covers a two-member group over witnessed and one-way members only (the row above). It does not model forward idempotence or compensation, so the gate's idempotence conjuncts are untouched by it; it also leaves out a member that lands and then faults, a divert, nested groups and more than two members, and there is no differential row over fault schedules. Its over-firing result matches the residual above: a skipped-then-fired emission with no inverse stays out after an abort.
- The design names the seeded interleaving tester (item 295) as the natural falsifier for the interleave-safety theorem and as the route to turning the open residuals C3 and C4 into tests (design doc:1000); that integration has not been done.
- The design doc header still reads "design, not implemented" (`docs/design/259-checked-parallel-emissions.md:3`) although the checker and the Python runtime fan-out are on main; this draft follows the code.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 45 checked, 6 corrected.
