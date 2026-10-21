---
id: P01
title: "Typed Revertible Effects: Inverse-or-Emit as a Checked Guarantee"
theme: formal-pl
kind: full
maturity: shipped-evidence
status: "The checker, the rejection corpus and the guarantee table are on main with tests; the Lean models cover the rule lattice but not the Python checker, and no evaluation on third-party code exists yet."
fused_with: P23
---

## Pitch

A compiler can make "every effect is undoable or visibly irreversible" a checked property by classifying effects as acquire, pure, witnessed or emission, and can then derive component lifecycle and LIFO undo from the dependency graph so the author never writes teardown code.

## Abstract

Systems that load and unload components at run time depend on undo code that is written once, exercised rarely and wrong quietly. Resource-safety disciplines such as RAII, bracket and defer help, but nothing forces a mutation to name its inverse, and nothing separates an effect that can be undone from one that crossed the system boundary. We present revl, a language where reversibility is part of the type structure. Every effect is classified as acquire, pure, witnessed or emission. The checker's central rule (G4): a mutation either carries a well-shaped inverse or is an explicit, approval-gated emission. The rule is refined by capability scopes, so a service declaration `emission[db]` bounds where any provider may cross, and the bound is computed as a least fixed point over the call graph. Because code outside effect forms is pure, the teardown accumulator holds every mutation at the head granularity the proof ledger certifies, and lifecycle (activation, deactivation, LIFO undo) is derived from the dependency DAG with no author-written lifecycle code. Typed holes let a draft type-check around missing parts while noting them as unmet obligations that admission and every backend refuse. We describe the rule set, the decode table that separates guarantees inherited from the Cordis paradigm, revl-original ones and ordinary type theory, and the implementation and test corpus (186 rejection reproducer files, 47 of them with `g4` in the name). We state what is not claimed: inverse semantics are checked for presence and shape, not correctness, and the foreign-code boundary is trusted but enumerable. Lean models of the rules and a differential oracle against the shipped checker are reported in a companion paper.

## Problem and claim

Cordis (the paradigm revl descends from) has revertible effects as a discipline: its theorems rest on hypotheses a library can only ask programmers to respect (`README.md:36-41`). In practice that means undo logic is a write-only code path, and the distinction between "reversible" and "crossed the boundary" lives in programmer memory. Existing resource-safety mechanisms (RAII, `bracket`, `defer`, saga libraries) pair an acquisition with a release at best by convention; none is a type-level obligation with a checked escape hatch.

The claim is narrow and checkable: in revl, (1) every mutation form is classified, (2) a mutation without a declared inverse is refused unless it is marked as an emission, (3) the set of host boundaries a provider may cross is bounded by its service declaration and that bound is checked transitively, and (4) lifecycle and undo order are derived, not written. The paper does not claim that an `undo` expression truly reverts its effect; the project docs say this is undecidable in general and opt-in (`verified`) (`docs/vision.md:95-99`).

The positioning sentence the project itself uses is "Cordis has revertible effects as a discipline; revl makes them a type system", and the same page adds "Everything beyond that is roadmap, not claim" (`docs/vision.md:113-114`). The paper adopts that restraint. The RAII-to-Rust analogy is rhetorical framing for the introduction, not a result.

## Contributions

1. An effect classification (acquire, pure, witnessed, emission) with rule G4, inverse-or-emit, and its capability-scoped refinement: a provider's reachable capability set must be a subset of the scope in its service declaration (`docs/capabilities.md:126-170`; `src/revl/lower.py:15055-15083`).
2. A transitive analysis for that rule: a least fixed point over the call graph, lifted from a boolean to a set of capabilities, with recursion terminating (`src/revl/emission_analysis.py:157-200`).
3. Implicit lifecycle: activation and deactivation derived from the dependency DAG, with G2 (one provider per key, per realm) and G3 (acyclic) checked at link time (`DESIGN.md:154-160`, `DESIGN.md:223-235`).
4. Checked undo: because code outside effect forms is pure (G6), the accumulator is complete at head granularity; host acquisitions must undo as their release on the bound handle, and `no_residue` fails when a bracket's undo did not reverse its write (`README.md:127-135`; `docs/rejections.md:173-232`; `CHANGELOG.md:472-477`).
5. A decode table that assigns refusal codes an enforcement point and, where sourced, an origin (Cordis-inherited, revl-original, standard type theory) (`docs/guarantees.md`).
6. Typed holes as a first-class unmet obligation: checked around, noted, refused at admission and by every backend (`docs/holes.md`; `src/revl/holes.py:72-93`; `tests/test_holes.py:261-327`). Sound typing with `Opt[T]` absence and a documented gradual frontier is a supporting piece (`docs/roadmap-archive/02-done-dependency-order-as-built.md:29-31`).

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Cordis (Koishi; the paper "A Programming Paradigm for Spatiotemporal Composability") | Revertible effects and disposers as a runtime discipline, with theorems that assume well-behaved programs | Moves the theorems' hypotheses into a checker; `docs/guarantees.md` marks which codes are Cordis-inherited and which are original |
| Rust ownership, C++ RAII | Lexical resource scope enforced by destructors or the borrow checker | Governs dynamic component scope; pairs each mutation with a declared inverse and distinguishes irreversible crossings by marker, not by drop order |
| Koka, Eff effect handlers and row-typed effects (Leijen; Bauer and Pretnar) (citation to be checked) | Effects tracked in types, handled by delimited continuations | Effects are tracked for reversibility class and boundary scope, not for control flow; no handlers, and teardown is derived |
| Haskell `bracket`/`ResourceT`, Scala/ZIO `Resource`, Python `ExitStack`, Go `defer` | Library or idiom for paired acquire/release | Pairing is a compile-time obligation; an unpaired mutation is refused, an irreversible one must be marked |
| Sagas and compensation (Garcia-Molina and Salem; Temporal-style libraries) (citation to be checked) | Compensating actions written by hand per step | `compensate` is an extern-declaration slot checked by the registry rule A5, and compensations run in a defined second phase |
| Linear and affine types (citation to be checked) | Resources used exactly once or at most once | Not a substructural discipline; the guarantee is "inverse present and well-shaped", plus a derived LIFO order |
| Capability systems and effect-scoped permissions (object capabilities, WASI-style) (citation to be checked) | Authority by reference | `emission[cap]` bounds where a provider may cross, and the bound is checked against transitive reach, so a hot-swapped provider cannot widen it |

## Evidence on main

| Claim | Where | Status | Note |
|---|---|---|---|
| Core move: every mutation beside its inverse, or an `emit` marker at the call site | `README.md:30-34`; `docs/rejections.md:173-190` (`g4_missing_undo`) | shipped | Rejection reproducer `examples/rejections/g4_missing_undo.rvl`; message asserted at `tests/test_frontend.py:239` |
| Four effect classes enumerate the boundary (G8) | `docs/design/456-ambient-and-prog-export.md:223` (the four-class row); `docs/rejections.md:551-560` (G8 refusal). `DESIGN.md:184-191` covers only acquisition vs emission | shipped for the G8 refusal; the four-class list is in the export format, not `DESIGN.md` | Unclassified `extern` is refused |
| `acquire` and `witnessed` externs must declare `undo` | `src/revl/lower.py:4832`, `src/revl/lower.py:5078` | shipped | The Lean file cites older line numbers (`lower.py:2573`, `:2744`), which have drifted |
| Emission is forbidden an `undo`, since it is a one-way crossing | `src/revl/lower.py:4893-4896` | shipped | The hint redirects to `compensate`. Lines 3956-3966 are the G5 witnessed-through-emission message, a different rule |
| Capability-scoped G4: provider reach must be a subset of the declared scope | `src/revl/lower.py:15055-15083`; `docs/capabilities.md:126-170`; `examples/rejections/g4_capability_not_declared.rvl`; `tests/test_frontend.py:278` | shipped | Subset, including empty, is accepted: "a provider purer than declared is sound" |
| Transitive least fixed point over the call graph, as sets | `src/revl/emission_analysis.py:157-200`; `docs/capabilities.md` ("Transitivity") | shipped | Scoped externs contribute the scope, not their name; a bug where name-keying refused an in-bounds provider is documented in the docstring |
| Scoped-extern subset behaviour | `tests/test_1234_g4_scoped_extern_subset.py:75`, `:84`; `tests/test_emission_capscope.py:51-90` | shipped | |
| Lifecycle derived from DAG, no author-written lifecycle | `DESIGN.md:154-160`; `docs/guide-humans.md:9-35` | shipped | G2/G3 refusals: `examples/rejections/g2_provision_conflict.rvl`, `examples/rejections/g3_dependency_cycle.rvl` |
| Pure code outside effect forms makes the accumulator complete | `README.md:127-135`; `DESIGN.md:161` and the guarantee table (G6) | shipped at head granularity | `formal/STATUS.md` (G6 row) and `docs/vision.md:109` agree: G6 is `full` at head granularity, the shape-level statement being the row's content. The paper relies on that head-granularity claim; the README's "provably" is softened in the abstract |
| Host acquisition undo must be its release; `no_residue` red otherwise | `docs/rejections.md:193-232`; `CHANGELOG.md:472-477`; `tests/test_extern_acquire_inverse_1859.py`, `tests/test_host_undo_release_1859.py` | shipped | `docs/rejections.md:228`: "What this proves is the choice of inverse; that the host body reverts is still the declaration's assertion" |
| Typed holes noted, never run; admission and backends refuse | `docs/holes.md:1-33`; `src/revl/holes.py:72-93` (code T3); `tests/test_holes.py:261`, `:304` | shipped | |
| Decode table with origins | `docs/guarantees.md` (G1-G9, A, T families) | shipped | Several rows say "origin: unconfirmed", so the origin split is not complete |
| `Opt[T]` absence, gradual frontier | `docs/roadmap-archive/02-done-dependency-order-as-built.md:29-31` | shipped (archive entry) | Typing rules T1-T3 have no Lean theorem (`formal/STATUS.md`, T1/T2/T3 row: "none") |
| G4 over the classification lattice mechanised | `formal/RevL/Theorems/G4_ClassifiedInverseOrEmit.lean:75`, `:88`, `:112`, `:169`, `:178` | full over the lattice; shape-level statement weak and marked | Models the per-declaration rule and the reach fold; the G4 row of `formal/STATUS.md` lists open items (fuel bound, `FnDecl.calls` stand-in, first-class dispatch read as `*`) |
| G4 rule checked against the shipped checker | `formal/STATUS.md` G4 row ("182 G rows, 25 P rows") | partial | Differential oracle in `formal/harness/diff_corpus.py`; this is evidence of agreement on a corpus, not a proof about Python |

## Numbers that exist

- 186 files in `examples/rejections`. 47 have `g4` in the filename; the rest are other rules.
- G4 differential-oracle rows reported by the proof status file: 182 G rows, 25 P rows, 6 agree-G4, 36 approval-floor refusals filed under agree-G4 (`formal/STATUS.md`, G4 row). These are corpus sizes, not accuracy figures.
- G4 Lean layer: 2 + 7 + 10 theorems per the status file; `formal/RevL/Theorems/G4_ClassifiedInverseOrEmit.lean` contains 7 `theorem` declarations.
- No measurement exists of how many real bugs G4 prevents, of author burden, or of false-positive rate on third-party code.

## Paper outline

1. Introduction: undo as the write-only path; the discipline-to-type-system pitch, stated with the docs' own restraint.
2. Model: components, services, `effect ... undo`, `emit`, the four classes.
3. The G4 rule and its capability refinement, with the transitive analysis (set-valued least fixed point).
4. Derived lifecycle and checked undo: G2, G3, G6, G7 as consequences; what "the accumulator is complete" depends on.
5. Typed holes and admission: drafts that check but cannot run.
6. Decode table: which guarantees are inherited, original, or ordinary typing.
7. Implementation and corpus: where each rule is enforced; the rejection corpus; mechanized models and the differential oracle in brief (details in the companion paper).
8. Evaluation: a study on code not written for the rule, and a bug-detection comparison against existing languages, once run.
9. Related work and limits.

## Threats to validity and limits

- Semantic inverse correctness is not decided: "The checker proves an inverse is present and well-shaped (G4)" and no more (`docs/vision.md:95-99`).
- The extern boundary is trusted, not verified, though enumerable (`docs/vision.md:100-102`). Host body reversal "is still the declaration's assertion" (`docs/rejections.md:228`).
- G7 (derived LIFO teardown) is "by lowering" and exercised by runtime scenarios; the only checker-side lever is totality of `verified fn` (`docs/rejections.md:530-538`). The "derived, hence correct" claim therefore rests on the lowering and runtime, not on a checker rule.
- The metatheorems "transfer by construction, they are not re-proved here" (`docs/vision.md:103-110`). The Lean layer covers part of the admission judgment; G1 is `partial`, G2 to G9 are `full` at the granularity `formal/STATUS.md` states per row, and T1-T3 have no theorem.
- Several origin labels in `docs/guarantees.md` are "unconfirmed", so the Cordis-inherited split is not fully sourced.
- The corpus is the authors' own; no third-party study exists yet, so the paper cannot say the rules are low-burden.
- The first-class dispatch case is conservatively read as `*` (unnameable boundary), which can over-refuse.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 15 checked, 9 corrected.
