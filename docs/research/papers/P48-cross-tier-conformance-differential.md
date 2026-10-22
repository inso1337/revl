---
id: P48
title: Six Backends, One Semantics: A Conformance Matrix, Authored-Probe Differential Execution and a Cross-Tier Agreement Theorem
theme: compiler
kind: empirical
maturity: shipped-evidence
status: The generated matrix, executor, drift gate and Lean theorem are on main; execution covers 19 of 61 constructs on three default tiers (rust and java behind a switch), the theorem is not yet bridged to the matrix, stream exit discipline is partial, and no divergence statistics exist.
fused_with: P49, P50
---

## Pitch
For a language that emits to six host runtimes, "the emitter did not raise" and "the output typechecks" both passed while tiers disagreed on values, so we state conformance as three questions, answer the third by running each executing tier (python, TypeScript and Go by default; rust and java behind a switch) against one authored literal, and prove under two named hypotheses when agreement must hold.

## Abstract
A compiler that targets several host languages usually reports portability as a table of constructs that compile on each target. We show, from a six-target compiler (Python, TypeScript, Rust, Java, WebAssembly, Go), that this table certifies the wrong thing. We separate three questions: did the emitter produce code, does the code pass the target's real toolchain, and do the targets agree on what it evaluates to. For the third we give each executable case one authored answer written as a source literal and run it on each executing tier whose runtime is present (python, TypeScript and Go by default; rust and java behind a switch), so that three tiers agreeing on a wrong value also fails; a negative control shows a wrong literal is rejected. Today the generated matrix covers 61 constructs across six emitters plus the self-host column, 19 executed with a declared answer and 42 compile-only, each row labelled with its claim strength; a drift gate fails CI if the committed matrix is stale. A larger executed suite covers rust and java behind a slow-test switch, now set in one CI job. We log the semantic divergences found by execution (structural equality on three tiers, integer division on Java, among others), close them, and pin the remainder so a pinned case that starts to agree also fails. Finally we give a Lean proof that any two runtimes meeting two conditions lower a well-annotated IR to the same value, show the annotation hypothesis is necessary, and state that whether the real emitters meet the conditions is the matrix's empirical obligation.

## Problem and claim
Compile-depth checks cannot see value divergence. In this project `tsc --noEmit` covered a TypeScript demo the whole time its runtime assertions failed, and Java typechecked `Int / Int` cleanly while answering `3` where every other tier answered `3.5` (`docs/conformance.md`, "The third question"). The spec said "no backend can diverge" on equality while two tiers did (`docs/contract-errata.md:203-215`).

Claim: cross-backend agreement is a testable contract with three levels of evidence, and each level must be reported separately: emitted, validates in toolchain, agrees on execution. Agreement must be anchored to an authored literal, because agreement among tiers is not correctness. A skipped test must never render as a pass, because the executing tiers in CI were, for a long period, empty.

## Contributions
1. A generated construct-by-tier matrix (61 constructs, six emitters plus the self-host column) with a CI drift gate and a per-row claim column (`exec` / `compile-only` with reason) (`docs/conformance.md:96-120`; `tests/test_conformance_matrix.py`).
2. The three-question conformance model with explicit `unavailable` for a missing toolchain, never `ok` (`docs/conformance.md`, "Three questions, not one").
3. Differential execution against an authored probe literal: 19 of 61 cases executed on Python, TypeScript and Go through `tools/conformance.py --execute`, plus the rust/java suite behind a switch; a negative control, which runs on the Python executor only (`tests/test_conformance_execution.py:131`).
4. A Lean theorem: any two `Conformant` profiles lower a `WellAnnotated` IR to the same value, with `annotation_necessary` showing the hypothesis is load-bearing (`formal/RevL/Theorems/CrossTier.lean:197-203`, theorem at 203).
5. A divergence discipline: semantic divergences found by execution are closed and logged; remaining ones are pinned and a pinned case that meets the ideal fails (`docs/contract-errata.md:203`; `tests/test_cross_tier_execution.py:757,932`; `tck/README.md`, "Pinned divergences").
6. Stream surfaces lower or refuse by name on each tier (partial; see "Evidence on main").

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Csmith, YARPGen, EMI (citation to be checked) | Random program generation, compare compilers or optimisation levels | Fixed authored corpus with a declared answer per case; agreement alone is insufficient. Generation is a separate paper (P49) |
| WebAssembly spec tests, test262 (see the [related-work table](../related-work.md)) | One suite, many engines, a spec as oracle | Suite is language-level and runs through six code generators to host languages, not engines of one language |
| Kotlin Multiplatform, Haxe (citation to be checked) | One source, several targets | Neither is known to publish a per-construct, per-tier matrix with an execution claim column and a drift gate; this needs checking (citation to be checked) |
| SPEC CPU (citation to be checked) | Benchmark portability across platforms | Not a correctness-of-semantics claim |
| CakeML, CompCert (see the [related-work table](../related-work.md)) | Proved compilers to one target | Proves only an abstract agreement condition; whether emitters meet it is left to an empirical matrix, stated openly |
| McKeeman differential testing (citation to be checked) | Comparing implementations on one input | Adds the authored literal so unanimous wrongness fails |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Generated matrix, six emitters + revl column, drift gate | `docs/conformance.md:1-40`, `docs/conformance.md:96-120`; `tests/test_conformance_matrix.py` | shipped | 61 constructs emitted, 1 rejected by frontend |
| Per tier results: py/ts/rust 61 ok, java 60, go 60, wasm 51 (10 deliberate limits), revl 59; 0 real gaps | `docs/conformance.md:96-108` | shipped | Generated block |
| Three-question model; `unavailable` not `ok` | `docs/conformance.md:288-310` (section "Three questions, not one") | shipped | |
| 19 executed on py, ts, go with one authored literal | `docs/conformance.md:110-115`, `docs/conformance.md:175-200`; `tools/conformance.py:347` (`PROBES`); `tests/test_conformance_execution.py:65` | shipped | The executor table lists three tiers, not six |
| Negative control | `tests/test_conformance_execution.py:131-145` | shipped | The test runs the control on the Python executor only |
| rust and java execution behind `REVL_CROSS_TIER_SLOW` | `tests/test_cross_tier_execution.py:1-60`; `.github/workflows/ci.yml:2262-2292` | shipped | Set in the `conformance` job |
| Silent skips guarded | `tests/test_env_gated_skips_run_somewhere.py:10-30,792` | shipped | |
| Divergences closed and pinned | `docs/contract-errata.md:203-260`; `tests/test_cross_tier_execution.py:757-800`, `tests/test_cross_tier_execution.py:932-960` | shipped (closure); pinning is empty here | The DIVERGENCES table is empty after #549 per its own comment (`tests/test_cross_tier_execution.py:778-782`), so the pinned-case drift test has no cases; pinned divergences are described in `tck/README.md` |
| TCK with R1-R5, A1/A5/A8, G7 cases and pinned divergences | `tck/README.md:1-60` | shipped (doc read; runner not opened) | A2-A4, A6-A7 listed as pending, never green |
| Cross-tier agreement theorem | `formal/RevL/Theorems/CrossTier.lean:203` (`cross_tier_agreement`), `formal/RevL/Theorems/CrossTier.lean:252` (`six_tier_agreement`), `formal/RevL/Theorems/CrossTier.lean:262` (`annotation_necessary`); `formal/STATUS.md:597-615`, `formal/STATUS.md:299-302` | shipped | No `sorry` (`formal/STATUS.md:597-615`). Per `formal/STATUS.md:299-302`, `cross_tier_agreement`, `six_tier_agreement` and `conformance_hypotheses_are_inhabited` use `propext`, and `annotation_necessary` uses no axioms; map values one level deep |
| Stream surfaces lower or refuse by name | `docs/v2.0-roadmap.md:1374` (item 130); `backends/rust/emit.py:4259` (`_refuse_unlowered_stream_surface`); `tests/test_stream_reactive.py` | partial | Item 130 says a pull request lowers the last-n replay backlog on ts, go and java with the durable cursor still refused by name there, and that rust refuses both replay forms. No test lists every stream surface per tier |
| Backend IR errata: ship order, normative lowering strategy | `docs/contract-errata.md:1-28` | shipped | |

## Numbers that exist
- Matrix: 61 constructs emitted; 19 executed, 42 compile-only (33 activation, 9 signature); per tier ok/limit/gap table above (`docs/conformance.md:96-120`).
- Guarantee-by-tier matrix: six host columns identical `proved` on every G/A/T row that has a reproducer; the self-host `revl` column has `proved`, `div` (G1, G2, G3, G6) and `unimpl` (G5, G7, G9, A2, A8, A9, G-RETAIN, G-SECRET-FLOW) cells (`docs/conformance.md:228-256`; section at `docs/conformance.md:170`). A caveat from the document: the six host columns share a verdict because the frontend runs once before emission; this says nothing about emitter-level enforcement.
- `tests/test_cross_tier_execution.py` header: 30s under the old gates (93 pass, 94 skip), 3m53 with node_modules (128 pass), 4m53 with rust (156 pass), 6m31 with a JDK; 87 tests newly execute. Measured on one arm64 laptop; the header does not say how many runs were taken.
- Two concrete value bugs that sat under existing assertions: Java `Int / Int` integer division, `==` on Float inverting NaN and -0.0 (`tests/test_cross_tier_execution.py:160-167` for integer division and `IEEE_FLOAT`; `tests/test_cross_tier_execution.py:540-548` for the `doubleToLongBits` equality).
- No data yet on how many divergences execution found per tier over time; the errata give individual instances, not a count.

## Paper outline
1. Introduction: portability claims that pass while values differ.
2. The architecture: one IR, six emitters, host runtimes.
3. Three questions and the generated matrix, claim column, `unavailable`.
4. Authored-probe differential execution; the unanimous-wrong case; the negative control.
5. What execution found: equality, integer division, float edges; closure and pinning discipline.
6. Making skips visible: the gates that executed nowhere.
7. A cross-tier agreement theorem and its two hypotheses; what stays empirical.
8. Evaluation: divergence count by mechanism, counterfactual mutants.
9. Limits, related work.

## Threats to validity and limits
- Execution covers 19 of 61 cases; 42 are compile-only. The doc says a green row on those proves only "the emitter produced code its toolchain accepts" (`docs/conformance.md:114-118`).
- The matrix's executor table names three tiers; rust and java execution lives in a separate, switch-gated suite whose cost is minutes (`tests/test_cross_tier_execution.py`). No wasm executor was found.
- The negative control runs on the Python executor only.
- The theorem models each backend by its observable profile on three axes; "that the real python/rust/... emitters realise a conformant profile is the differential conformance matrix's job (an empirical obligation)" (`formal/RevL/Theorems/CrossTier.lean`, scope section). Map values are one level deep. The theorem needs `Conformant` and `WellAnnotated`; which matrix rows evidence `Conformant` for each tier has not been stated, so the theorem is not yet connected to the matrix.
- The guarantee matrix's six host columns agree by construction, since refusal happens in a shared frontend. It does not measure emitter-level enforcement; the doc states `proved` "is a REFUSAL claim".
- wasm has 10 deliberate limits; it is not a full peer.
- A single codebase, authored fast; the divergences found are partly an artefact of how early the cross-tier checks were added.
- No count of divergences by discovery method, and no counterfactual that re-introduces closed divergences to see which of the three levels would flag them.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: about 45 checked, 9 corrected.
