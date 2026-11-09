---
id: P49
title: Type-Directed Cross-Tier Fuzzing With a Reference Oracle, an Honesty Classifier and a Delta-Debug Shrinker
theme: compiler
kind: short
maturity: partial
status: The fuzzer, its tests and 13 regression fixtures are on main, but no campaign log, totals, baselines or CI run exist, so the findings rest on roadmap text and the type-directed claim has no ablation.
fused_with: P48, P50
---

## Pitch
A compiler that targets six runtimes can turn "all tiers agree" into a running experiment: generate typed programs, take the Python tier as the oracle, refuse to count declared refusals and absent toolchains as bugs, and shrink what is left to a fixture a human can read.

## Abstract
Compilers with several backends claim that one source program means the same thing on every target, but the usual evidence is a hand-written conformance suite, which only reaches the cases someone thought of. We describe a differential fuzzer for revl, a language that lowers one typed IR to six runtimes (Python, TypeScript, Go, Rust, Java, WebAssembly). A type-directed generator builds pure functions over Int, Float, Bool, Str, lists, options, results, product types and algebraic types with `match`; the frontend is the judge of admissibility, so programs it rejects are discarded rather than counted. The Python tier is the oracle: its value for a probe function is embedded as an assertion that every other available tier must satisfy. An honesty classifier separates four outcomes that a naive harness would merge: agreement, an absent toolchain, a documented emitter refusal, and a real divergence, which is either a build failure of code the emitter itself produced or a different value. A two-phase delta-debugging shrinker drops declarations and then simplifies the probe expression until the divergence is minimal, and the result is written as a regression fixture with its root-cause fingerprint. The roadmap entry for item 292 states that the tool rediscovered two known backend defect classes (Rust build gaps, Go option-type gaps) and found two new ones (no campaign log is in the tree): a WebAssembly soundness hole where refusal of Float did not recurse into variant payloads, and a Go silent value divergence on `Ok`/`Err` and empty-list equality. Thirteen regression fixtures are kept in the tree. We state what the method cannot see: bugs in the shared frontend, and anything the Python tier also gets wrong.

## Problem and claim
Multi-target compilers (Kotlin Multiplatform, Haxe, Scala.js and similar) advertise semantic uniformity. The evidence for it is normally a conformance suite of authored cases. That suite is blind in the same way the author is: revl's own docs describe string-length, integer-overflow and equality divergences that survived because "every test was ASCII" (`docs/strings.md:21-24`) or because a test checked that an emitter "accepts a construct" and not that the tier "means something else" (`docs/contract-errata.md:271-276`).

Plain differential fuzzing in the Csmith line needs care in a setting like this one, because not every difference is a bug. A backend may refuse a construct on purpose (at the time of this campaign the WebAssembly tier had no Float value representation; it lowers Float natively now, issue #3295), and a machine may lack a toolchain. A harness that reports both as divergences drowns the real findings. The claim of this paper is narrow: with a typed generator, a fixed reference tier, and an explicit classifier that only counts "built but did not run" and "ran but computed a different value", a small harness (about 1000 lines of Python) is reported to have found real cross-tier defects and shrinks them to fixtures.

This is a first cut and says so itself: "It is not a property-testing framework" (`tools/fuzz_cross_tier.py:31-32`). The paper would be a tool and experience paper, not a theory paper.

## Contributions
1. A type-directed generator for the revl pure subset (`Generator`, `tools/fuzz_cross_tier.py:270`), deterministic under `--seed`, whose admissibility is judged by the real frontend, with rejected programs discarded and counted.
2. A reference-oracle protocol: the py tier's value of `probe()` is rendered as a literal and embedded as `test "cross_tier_probe" { assert probe() == <lit> }`, so the other tiers run their own test runner on the same source (`assertion_source`, `tools/fuzz_cross_tier.py:615`). No tier-to-tier value serialisation is needed.
3. An honesty classifier with explicit non-findings (`classify`, `check_tier`, `tools/fuzz_cross_tier.py:655-710`): pass, skip (toolchain absent), refusal (emitter declined, a declared capability boundary) and only then build or value divergence.
4. A two-phase delta-debug shrinker (drop declarations to a fixpoint, then simplify the probe expression outermost first) that re-derives the reference literal on every candidate so the fixture stays self-consistent (`shrink`, `tools/fuzz_cross_tier.py:790`).
5. A divergence signature that fingerprints root cause (`divergence_signature`, `tools/fuzz_cross_tier.py:671`) so distinct bugs yield distinct fixtures, plus fixture emission with the mapping to a known roadmap item or "NEW" (`emit_fixture`, `tools/fuzz_cross_tier.py:857`).
6. A kept corpus: 13 regression fixtures with notes under `examples/regressions/` (9 Go, 2 Rust, 2 WebAssembly), and two defect reports (items 301, 302) traced to the fuzzer.

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Csmith (Yang et al., PLDI 2011) (citation to be checked) | Random C programs, majority vote across compilers, avoids undefined behaviour by construction | Targets one compiler's six backends, not several compilers; oracle is a designated reference tier, not a vote; the generated language is a typed pure subset where UB does not exist |
| YARPGen (citation to be checked) | Generates programs with controlled UB for compiler optimisation bugs | Not looking for optimiser bugs; looking for cross-target semantic drift, including build failures of emitted code |
| EMI / equivalence modulo inputs (citation to be checked) | Mutates programs to equivalent variants and compares one compiler's output | Compares across backends of the same source, no variant generation |
| McKeeman, differential testing (1998) (citation to be checked) | Establishes comparing implementations on generated input | Adds a classifier that excludes declared refusals and absent toolchains |
| WebAssembly spec tests, test262 | Hand-written or conformance suites per implementation | Generated, shrunk, and the corpus is grown by findings; the authored suite is the sibling (P48) |
| Kotlin Multiplatform, Haxe | Multi-target languages with per-target test suites | No public cross-target differential fuzzer known to the authors (citation to be checked) |
| QuickCheck-style shrinking | Type-derived generators with shrinking inside one runtime | Shrinks over source declarations and a probe expression, across runtimes; the user-facing `prop test` is the in-runtime cousin (see the evidence table) |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Type-directed generator, py oracle, honesty classifier, two-phase shrinker exist | `tools/fuzz_cross_tier.py:1-61` (design), `tools/fuzz_cross_tier.py:270` Generator, `tools/fuzz_cross_tier.py:655` classify, `tools/fuzz_cross_tier.py:790` shrink | shipped | 1061 lines |
| Frontend is the admissibility judge; rejected programs are discarded | `tools/fuzz_cross_tier.py:8-11` (docstring), `tools/fuzz_cross_tier.py:955-980` (`main`, `discarded_reject`) | shipped | |
| Refusals and skips are not divergences | `tools/fuzz_cross_tier.py:623-645` (comment block), `tools/fuzz_cross_tier.py:655` (`classify`) | shipped | `test_classify_separates_refusal_build_value`, `tests/test_fuzz_cross_tier.py:104` |
| Divergence signature is stable and specific | `tests/test_fuzz_cross_tier.py:121` | shipped | |
| Deterministic under `--seed` | `tests/test_fuzz_cross_tier.py:39` | shipped | |
| Generator is "mostly admissible" | `tests/test_fuzz_cross_tier.py:52-68` asserts at least 108 of 120 admitted | shipped | The roadmap text says "~99% admissible"; the test floor is 90% |
| Smoke run completes on py alone, hermetic | `tests/test_fuzz_cross_tier.py:31` | shipped | 8 programs, seed 7 |
| Rediscovered 278 (Rust) and 280 (Go) | `docs/roadmap-archive/12-post-v2-0-horizon-strategy-review.md:588`; fixtures `examples/regressions/fuzz_rust_*`, `examples/regressions/fuzz_go_*` | shipped, asserted by roadmap text only | Items 278 and 280 are at lines 559 and 563 of the same file. No log of the campaign is in the tree |
| Found 301 (Wasm soundness, refusal not recursing into variant payloads and match-arm widths) | `docs/roadmap-archive/12-post-v2-0-horizon-strategy-review.md:613`; `examples/regressions/fuzz_wasm_af371f9d.rvl` | shipped | Roadmap states fix and that both fuzzer fixtures now refuse cleanly |
| Found 302 (Go `Ok`/`Err` and empty-list value divergence in `revlEq`) | `docs/roadmap-archive/12-post-v2-0-horizon-strategy-review.md:615`; `examples/regressions/fuzz_go_f4963252.md` | shipped | Note gives `RevlErr[any, any]` vs concrete instantiation, seed 2, program #4 |
| Regression corpus kept | `examples/regressions/` has 13 `.rvl` plus 13 `.md` | shipped | The roadmap says "10 fixtures"; three Go ones (`fuzz_go_letbind_valuetype`, `fuzz_go_matchlit_typeswitch`, `fuzz_go_vet_redundant_bool`) have hand-chosen names and were added later |
| Fuzzer's classifier is reused by the conformance certificate | `tools/conformance_cert.py:41,260,275` | shipped | Shows the classifier is a shared component |
| Sibling frontend fuzzer (invalid input, crash/refusal) | `tools/fuzz_frontend.py:1-30`, `tests/test_fuzz_frontend.py` | shipped | Out of scope here; cite as related work in the same repo |
| `prop test` has type-derived generators and shrinking, in the py runtime only | `docs/prop-test.md:1-40`, `src/revl/fault.py` (`_PROP_MAX_SHRINK` at `src/revl/fault.py:2332`), `tests/test_prop_test.py` | shipped | |
| Compiling `prop test` to all six tiers as a cross-tier fuzzer | `docs/prop-test.md:142-148` | designed | Doc: "is **not built here**" |
| Fuzzer runs in CI | none found | not shipped | No fuzz step appears in `.github/workflows/ci.yml`; only the smoke test in `tests/` runs |

## Numbers that exist
- Generator admissibility floor: at least 108 of 120 programs admitted at seed 11 (`tests/test_fuzz_cross_tier.py:52-68`). This is a test threshold, not a measured rate.
- Regression corpus: 13 fixtures (9 Go, 2 Rust, 2 WebAssembly), counted from `examples/regressions/`.
- Findings credited to the tool: 2 rediscoveries (items 278, 280) and 2 new defects (items 301, 302), from the roadmap entry (`docs/roadmap-archive/12-post-v2-0-horizon-strategy-review.md:588`).
- Default execution set: py reference, wasm, go; rust behind `--slow`; ts and java probed and skipped if their runner does not pass the trivial probe (`detect_tiers`, `tools/fuzz_cross_tier.py:892-917`).
- No campaign totals (programs generated, programs run per tier, wall time, divergence rate per seed) are kept anywhere in the tree. The fixture notes carry seed and program index for the divergences only (for example seed 2, program #4).

## Paper outline
1. Introduction: the claim "six tiers agree" and why authored suites miss it, with the emoji-length and `lambda_` examples.
2. Design: the four parts (generator, oracle, classifier, shrinker), one figure showing the outcome lattice pass / skip / refusal / build / value.
3. Case studies: the four findings (278, 280 rediscovered; 301, 302 new), each with the shrunk fixture.
4. Evaluation: campaign totals, ablations, shrink ratios (none of which exist yet).
5. Limits and related work.

## Threats to validity and limits
- Shared frontend: all tiers consume the same IR, so a frontend or type-checker bug that gives every backend the same wrong program is invisible. The fuzzer's own docstring defines a divergence only against the py reference (`tools/fuzz_cross_tier.py:17-27`).
- Oracle correctness: the py tier is trusted by fiat. P47 discusses that agreement can be wrong agreement. A value the py tier gets wrong and others copy is not a finding.
- The honesty classifier is string matching on runner output (`_BUILD_ERROR_SIGNS`, `tools/fuzz_cross_tier.py:646`): a build error whose message lacks every sign is classed as a value divergence, and an assertion message containing one of the signs is classed as a build failure. The classifier has one unit test with four example messages (`tests/test_fuzz_cross_tier.py:104-118`) and no measured precision.
- Refusals are excluded by design. A tier that refuses too much (over-refusal) is never flagged, only counted. The fix for item 301 notes "no over-refusal [Float-free ... still emits]" but that was checked by hand.
- Reach is a bounded grammar of pure functions; "It is not a property-testing framework" is the tool's own caveat. Effects, components, async and cordis realms are out of reach. Java and TypeScript execution depends on local toolchains and is skipped silently if absent (`docs/contract-errata.md:281-300`), so a campaign on a box without them says nothing about those tiers.
- The 278/280 "rediscovery" is a roadmap statement; the campaign log is not in the tree. It cannot be replayed from main today.
- The roadmap's "~99% admissible" is not what any test checks (floor 90%).
- No ablation shows that the type-directed generator beats an untyped one, no shrink ratios exist, and the oracle has not been swapped to a second tier to measure its bias. The cross-tier `prop test` is design-only.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 23 checked, 3 corrected.
