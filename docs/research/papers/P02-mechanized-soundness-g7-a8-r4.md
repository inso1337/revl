---
id: P02
title: "Mechanized Soundness of Derived LIFO Teardown, WAL Discharge and No-Residue"
theme: formal-pl
kind: full
maturity: partial
status: "Lean theorems for teardown, WAL discharge and no-residue and a differential oracle against the Python runtime are on main; roll-forward, failure during teardown and cascading abort are not modelled, and no mutation study of the oracle exists."
fused_with:
---

## Pitch

A Lean development, tied to the shipped Python runtime and recovery code by a differential oracle that runs them, shows, for the modelled fragment, that derived teardown replays exactly the right entries in the right order per verdict, that a failed body is reverted from a write-ahead log, and that what an abort leaves behind is exactly what the runtime reports.

## Abstract

Languages that derive cleanup order from structure, rather than letting authors write it, owe their users a precise statement of what the derived teardown does under each outcome. We give one for revl, whose per-activation stack holds three entry kinds (bracket, transactional, compensation) and is unwound in two phases under a verdict (commit, abort, or the operator halt). In Lean 4 we prove, with no `sorry` and no project axioms (the CI axioms gate checks this; it was not re-run for this write-up), that (G7) the set of entries replayed, their order, and the phase split are determined by kind and verdict, with replay, discharge and strand forming an exact partition; that (A8) a body that fails after taking witnessed steps has its world restored by replaying its write-ahead log, that the commit marker is the roll-back-or-roll-forward decision (a specification-agreement statement by the file's own account), and that an undeclared inverse is never applied twice across a crash cut; and that (R4) the world after an abort equals the starting world plus exactly the residue the runtime reports. The models are small on purpose, so the proofs are tied to the implementation by a differential oracle that drives the shipped runtime over an enumerated scenario corpus (267 teardown scenarios, 1620 WAL scenarios) and fails the build if the corpus stops distinguishing LIFO from FIFO or phase 2 from phase 1. The oracle found one real divergence, where the model claimed a clean re-issue and the code refuses and reports residue (60 of 1620 scenarios). We also report what is not proved: failure of an inverse during teardown, the roll-forward residue surface, escrow and cascading abort.

## Problem and claim

revl derives teardown: the author writes each effect beside its inverse, and the runtime unwinds the stack. That is only worth something if one can say exactly what the unwinding does. The design doc has to answer questions a naive "LIFO" does not: a transactional entry is discharged, not replayed, on a clean commit; a compensation is deferred to a second phase on abort; an operator halt replays and discharges nothing. An earlier version of the Lean model had one entry kind and replayed everything, and was found to contradict the runtime (`formal/RevL/Theorems/G7_LifoComplete.lean:5-24`). The statements had to be made relative to kind and verdict.

The claim is a mechanized account of three things: which entries run and in what order (G7), that failure reverts and the commit marker decides recovery across a crash (A8), and that abort leaves no residue except what is reported (R4). The limits are part of the claim: the models cover a small fragment and say so, and the status file lists the rest as unproved.

The paper also carries a methodological claim that is checkable: a proof about a model of a runtime is only useful if the model is run against the runtime. The oracle part is what separates this from proving a spec.

## Contributions

1. G7: a mechanized statement that, per entry kind and verdict, replay, discharge and strand are an exact partition; completeness and soundness of the replayed set; the LIFO equation; the two-phase split (compensations drain after the proof pass); LIFO within each phase; and the halted verdict, which replays nothing, discharges nothing and strands everything, with a total inventory of a halt cut into completed, ambiguous (at most one) and unattempted (`formal/RevL/Theorems/G7_LifoComplete.lean:46-392`; `formal/RevL/Semantics.lean:125-135`).
2. A8: `revert_on_failure` over a small-step semantics in which the log is the trace; `crash_cut_converges`, the commit-decision theorem and `approved_decides_the_crash_window`; `fence_before_apply_at_every_cut` and `at_most_once_across_crash` for undeclared inverses (`formal/RevL/Theorems/A8_WalDischarge.lean:103`, `:207`, `:224`, `:240`, `:256`, `:271`).
3. R4: `residue_is_exactly_what_remains` (the post-abort world is the starting world plus the reported residue), its corollary `abort_leaves_no_residue` when no emission occurred, and `residue_necessary`, which shows the side condition cannot be dropped (`formal/RevL/Theorems/R4_NoResidue.lean:75`, `:98`, `:174`).
4. Supporting rules: A2 (no acquisition after a provision), A5 (compensation accompanies an emission), and capability ceilings, each with its own theorem file (`formal/RevL/Theorems/A2_NoAcquisitionAfterProvision.lean`, `formal/RevL/Theorems/A5_CompensationAccompaniesEmission.lean`, `formal/RevL/Theorems/CapCeilings.lean`).
5. A differential oracle that runs `backends/python/runtime.py` and `src/revl/recovery.py` over enumerated scenario corpora and diffs observed dispositions against the model's predictions, with a coverage ratchet that fails the gate if the corpus stops biting (`formal/harness/diff_corpus.py:4876-4960`; `formal/STATUS.md`, rows G7, A8, R4).
6. An honesty ledger: per guarantee, whether it is full, partial or absent, the kind of gap (modelling limit or unbuilt work), and which statements are contentless or specification-agreement only (`formal/STATUS.md:44-72`; `formal/RevL/Theorems/A8_WalDischarge.lean:32-48`).

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| CompCert, CakeML, seL4 (see the [related-work table](../related-work.md)) | Verified compiler, verified language runtime, verified kernel; proof about the artifact itself | Models are small and abstract; the proof is tied to a shipped Python runtime by running it, not by extraction or refinement to code |
| FSCQ, Yggdrasil, Crash Hoare Logic and Perennial (verified crash consistency) (citation to be checked) | Proofs that a file system or storage layer recovers correctly after a crash | Recovery is a WAL replay for application effects with three entry kinds and a commit decision marker; proves a crash-cut convergence and at-most-once fences, not full crash refinement |
| Iris and separation-logic frameworks (citation to be checked) | General program logics for verified systems | Not used; revl uses a bespoke small-step semantics plus an enumeration oracle |
| Type-and-effect soundness (Wright and Felleisen progress/preservation) (citation to be checked) | Soundness of a type system against an operational semantics | The statements are not progress and preservation; they are properties of a derived teardown order and of recovery outcomes |
| Eff metatheory (see the [related-work table](../related-work.md)) | Semantics and soundness for algebraic effects and handlers | revl has no handlers; the theorem subject is reversibility bookkeeping |
| Sagas and compensation (Garcia-Molina and Salem) (citation to be checked) | Compensating transactions as a pattern, rarely mechanized | A compensation is a third entry kind with a defined phase and a discharge rule on commit, proved against the runtime |
| Cordis paper (Thm. 16, derived LIFO) | Proves derived teardown on paper under hypotheses | Mechanizes a version that is relative to entry kind and verdict, because the project's own earlier one-kind model contradicted the runtime in the transactional case (`formal/RevL/Theorems/G7_LifoComplete.lean:11-20`); whether the paper statement itself needs the same refinement is not checked here |

## Evidence on main

| Claim | Where | Status | Note |
|---|---|---|---|
| G7 replay table per kind and verdict | `formal/RevL/Theorems/G7_LifoComplete.lean:46` (`replay_table`, by `decide`) | shipped | Header explains the pre-fix model was wrong (lines 5-24) |
| Completeness and soundness of the replayed set | `formal/RevL/Theorems/G7_LifoComplete.lean:59`, `:85` | shipped | |
| LIFO equation, two-phase split, LIFO per phase | `formal/RevL/Theorems/G7_LifoComplete.lean:170`, `:184`, `:204`, `:209` | shipped | |
| Commit discharges transactional and compensation; commit replays only brackets | `formal/RevL/Theorems/G7_LifoComplete.lean:109`, `:119`, `:127` | shipped | Non-vacuity witnesses at lines 426-484 |
| Halted verdict: replays nothing, discharges nothing, strands all | `formal/RevL/Theorems/G7_LifoComplete.lean:233`, `:251`, `:262`; `formal/RevL/Semantics.lean:125-135` | shipped | `settles_iff_not_halted` (line 293) shows the `settles` hypothesis excludes exactly `halted` |
| Halt inventory is total, ambiguity at most one | `formal/RevL/Theorems/G7_LifoComplete.lean:373`, `:383`, `:392` | shipped | |
| Disposition trichotomy | `formal/RevL/Semantics.lean:134` | shipped | |
| A8 revert on failure | `formal/RevL/Theorems/A8_WalDischarge.lean:103`; witness at 412-415 | shipped | Fragment of five body forms; the `witnessed` step collapses a real window (`formal/RevL/Lemmas/WalLemmas.lean:701`; R4 header) |
| Crash cut convergence and commit decision | `formal/RevL/Theorems/A8_WalDischarge.lean:207`, `:224`, `:240` | partial: 207 carries real content; 224 and 240 are specification agreement | The header says 224 and 240 are specification-agreement statements and not deep results |
| At most once across a crash | `formal/RevL/Theorems/A8_WalDischarge.lean:256`, `:271`, `:295`, `:308` | shipped | Durability (fsync) is "a floor, not a theorem" (`formal/STATUS.md:2209`; `formal/RevL/Theorems/A8_WalDischarge.lean:86-89`) |
| `outcome_trichotomy` | `formal/RevL/Theorems/A8_WalDischarge.lean:170` | shipped but definitional | The file says so (header, "What each theorem is worth") |
| R4 exact residue equation and corollary | `formal/RevL/Theorems/R4_NoResidue.lean:75`, `:98`, `:119`, `:130` | shipped | Scope is the abort path only |
| R4 side condition is necessary | `formal/RevL/Theorems/R4_NoResidue.lean:174`, `:188`, `:199` | shipped | Concrete runs `txnLog` and `emitLog` |
| No `sorry` in the three files | `grep -c sorry` on the G7, A8, R4, A2, A5 and G4 theorem files returned 0; gate: `formal/scripts/axioms_gate.py`, `formal/CheckAxioms.lean` | shipped | The gate runs in CI job `formal` (`.github/workflows/ci.yml:2521-2540`), which skips pull_request events; `formal/CheckAxioms.lean` prints the G7, A8 and R4 theorems |
| Differential oracle runs the real runtime and recovery | `formal/harness/diff_corpus.py:4876` (`teardown_coverage`); `formal/STATUS.md` G7/A8 rows | shipped | Status file, G7 row: "RUNS backends/python/runtime.py"; A8 row: it writes each scenario as a real JSON-Lines WAL and runs `src/revl/recovery.py` |
| Oracle found a model/implementation divergence | `formal/STATUS.md:1675-1686`; test `tests/test_idempotent_inverse_309.py` (`test_undeclared_boundary_inverse_applies_once_then_fenced`) | shipped | 60 of 1620 scenarios disagreed before the fix |
| Roll-forward residue, cascading abort, escrow, failure of an inverse during teardown | `formal/STATUS.md` G7, A8, R4 rows ("Still deliberately not modelled", "Unbuilt work") | not modelled | See limits |
| A3 and T1-T3 | `formal/STATUS.md` A3 and T1/T2/T3 rows: no theorem | not proved | "Out of scope by kind" |

## Numbers that exist

- G7 file: 34 `theorem` declarations (grep count on `formal/RevL/Theorems/G7_LifoComplete.lean`); A8: 21; R4: 9. The status file counts differently ("32 + 7" for G7, 18 for A8, 9 for R4), so no single figure should be quoted without reconciling them.
- Oracle corpora per the status file: 267 D rows (G7), 1620 O rows (A8 and the R4 residue column) (`formal/STATUS.md`, rows at lines 54, 66, 69). These counts are written into the status file, not recomputed here; the status file says it stores "no count that moves with the corpus" in the generated block.
- The one divergence found: 60 of 1620 scenarios (`formal/STATUS.md:1685`).
- The three files are 492, 458 and 207 lines (`wc -l`).
- No proof-effort figures beyond line counts, no mutation-testing result and no timing exist.

## Paper outline

1. Introduction: derived teardown needs a precise contract; what contradicted the first model.
2. The teardown contract: three entry kinds, two phases, three verdicts (`docs/design/teardown-contract.md:42-100`).
3. G7 in Lean: model, five statements, the halt, and why `settles` costs exactly one verdict.
4. The WAL and A8: small-step semantics, revert on failure, the decision marker, at-most-once across crash cuts.
5. R4: the exact residue equation and why the side condition is necessary.
6. From model to implementation: the differential oracle, coverage ratchets, the divergence it found.
7. The honesty ledger: what is full, partial, contentless or unproved, and which gaps are modelling limits versus unbuilt work.
8. Evaluation: a mutation study of the oracle, proof effort.
9. Related work and conclusion.

## Threats to validity and limits

- The models are abstract. L0 has five body forms, a `witnessed` step that appends its descriptor and mutates atomically, which "collapses a real window the reference has", and no cascading abort, compensation drain or escrow (R4 header in `formal/RevL/Theorems/R4_NoResidue.lean`; `formal/RevL/Lemmas/WalLemmas.lean:701`).
- A crash between a witnessed mutation and its log entry is "Not covered, and not claimed" because the reference logs the descriptor after the forward extern returns (`formal/RevL/Theorems/A8_WalDischarge.lean:81-90`).
- G7 "says which entries run and in what order, not what happens when one of them fails" (`formal/STATUS.md:529`).
- Some theorems are specification agreement or definitional by the files' own account (the commit-decision theorem, `approved_decides_the_crash_window`, `outcome_trichotomy`); they should not be counted as deep results.
- Durability is a floor, not a theorem. R4 is stated for the abort only; its roll-forward surface is "Unbuilt work".
- The oracle is exactly as strong as its corpus; the coverage ratchet guards named clauses only. Scenario corpora are enumerated, not sampled from real programs.
- A no-residue result is about reported residue. An emission cannot be un-crossed, so the theorem is "exactly what remains is what is reported", not "an abort is clean" (R4 header).
- Reversal of the host body is the extern declaration's assertion (`docs/rejections.md:228`), so A8 gives no guarantee about foreign code.
- The oracle drives only the Python runtime. The repository has other runtime tiers and a cross-tier agreement theorem under named hypotheses (`formal/RevL/Theorems/CrossTier.lean`); the proof here is about the reference tier.
- The proof was developed alongside the runtime by the same project, so proof and implementation could share a misreading; the divergence found is evidence against that, one instance only.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 17 checked, 11 corrected.
