---
id: P16
title: "Testing Recovery Claims: Exhaustive Fault Sweeps and Inverse Round-Trips Across Runtimes"
theme: effects-replay
kind: empirical
maturity: partial
status: "Fault sweep, cross-tier sweep and round-trip tests are shipped with tests. A seeded-defect saga corpus (bench/saga_defects, issue #2736) reports the sweep's corpus-wide result: 4 of 16 seeded rows are caught only by the sweep, 4 are missed, and 8 are refused at compile (7 by a checker rule, 1 by a parse error; two rows are one defect kind at two call sites); no mutation analysis or baseline comparison yet."
fused_with: P17
---

## Pitch
A language that promises every effect has an inverse can test that promise instead of trusting it: inject one failure at every step of every component on several runtimes, and round-trip each declared undo against a state fingerprint, with the report stating its blind spots on every run.

## Abstract
Languages and frameworks that let authors declare an undo for each effect usually stop at checking that an undo exists. We describe two generated tests built into the revl toolchain that check that the undo works. The fault sweep derives, from the compiler's IR, one injected failure per top-level step of every component and asserts that the activation lands failed, leaves no residue, runs its inverses newest-first, and leaves sibling components unaffected; an unreachable step is listed rather than skipped, so an empty check cannot read as clean. The cross-tier sweep reruns the same faults on each mapped runtime whose toolchain is present (executing tests exist for py, go, java and rust only) and requires residue-freedom, agreement across tiers, and that exactly the owed compensations ran; a tier that cannot run is a loud skip. The verified-effect round trip snapshots an observable-state fingerprint, runs the effect and its inverse over 16 randomized configurations, and compares; a value-aware extension applies a logged inverse twice to catch delta-shaped inverses falsely declared idempotent. Each round-trip report names what it cannot see (aliased references, external emissions, clock and random values), and each sweep lists the steps it could not reach. The first fault test written against an async component body exposed a defect in a Cordis runtime (the fiber stayed ACTIVE after a failure that had correctly run its inverses), reproduced with a hand-built IR that is emitted and plugged directly, so it still goes through revl's emitter. The upstream fix is cited by the docs, not checked here. We report the design, the guarantees each assertion supports, and the failures it found. We do not yet report a bug-finding comparison against baselines or a mutation analysis.

## Problem and claim
Static guarantees such as "every effect has an inverse" (revl's G4) say the inverse is present and well-shaped, not that it is correct. `docs/verified-effect.md:13-30` states the concession directly: the runtime will not catch an undo that is wrong, partial or a no-op. Hand-written scenario scripts, one per backend, prove the property only for components their authors wrote (`docs/fault-tests.md:15-20`).

The claim: for a fixed failure model (a failure striking at a step boundary of a top-level activation body) the property "no mid-life failure point leaves residue, on any runtime that is present, and the runtimes agree" is checkable exhaustively and mechanically from the IR, and an inverse's correctness can be tested by a generated round trip whose report is explicit about its blind spots. The claim is scoped: the sweep is sequential and the round trip is in-process only (`docs/fault-tests.md:405-414`; `docs/verified-effect.md:163-180`).

The honesty discipline is part of the contribution: skips are loud, unreachable steps are counted, emissions are reported as "note: irreversible" and never as reverted, and a passing round trip says "not a proof".

## Contributions
1. `revl test --sweep`: one injected failure per top-level IR step, same assertion set as hand-written fault tests, an exhaustive verdict over top-level steps, unreachable steps reported (`docs/fault-tests.md:325-415`; `src/revl/fault.py:660` `sweep_units`, `:802` `run_sweep`).
2. The injection placement argument: inject after step N completes, not before it, because the earlier placement made `fail at step 1` vacuous (a false green) (`docs/fault-tests.md:53-96`).
3. The cross-tier sweep: same fault set on py, ts, rust, java, wasm, go via an authenticated residue proof, plus a compensation-owed-versus-ran check; agreement is a portability criterion and a skip is never a pass (`docs/fault-tests.md:416-550`; `src/revl/fault.py:913,1363`).
4. Verified-effect round trip: snapshot, effect, inverse, fingerprint over randomized rounds, reported as a test not a proof, with blind spots named in every report (`docs/verified-effect.md`; `src/revl/fault.py:1586,1900,2208`).
5. Value-aware double-undo round for idempotency claims, with the value-blind fold kept as a negative control (`src/revl/fault.py:1858`; `tests/test_idempotent_inverse_309.py:144-180`).
6. A finding: a Cordis (py) runtime left the fiber ACTIVE when an async body failed, found by the first fault test over an `await` component (`docs/fault-tests.md:297-322`).

## Related work
| prior system or line of work | what it does | what revl does differently |
|---|---|---|
| Jepsen (Kingsbury) and Chaos Monkey | inject faults into running distributed systems and check invariants from outside | The fault points are enumerated from the program's IR, so the "every step" claim is exhaustive for the failure model, not sampled in production. |
| Lineage-driven fault injection (Alvaro et al., SIGMOD 2015) (citation to be checked) | uses data lineage to pick fault combinations that could break a redundancy claim | revl uses single faults at every step of a sequential body; LDFI's multi-fault search is not attempted. |
| ALICE (Pillai et al., OSDI 2014), CrashMonkey (Mohan et al., OSDI 2018) | systematically explore crash states of file-system use | Those inject at storage-layer operations; this injects at language-level effect steps and checks LIFO inverse order and provision residue. |
| QuickCheck, Hypothesis | property-based testing with user-written properties | The round-trip property is generated from a declaration (`verified effect`), the author writes no test. Shrinking is not done (`docs/verified-effect.md:150-153`). |
| Idempotence checking in Stripe-style APIs (idempotency keys) | convention plus server dedup | The `idempotent` modifier is checked for shape only (the docs do not claim a shape check for `keyed`); a double-undo sweep tests behaviour, and the docs say a shape-proven tier cannot be derived while extern bodies are opaque (`docs/crash-recovery.md:303-316`). |
| Lenses and round-trip laws (Boomerang, Foster et al., 2007) (citation to be checked) | put/get laws proved for bidirectional transformations | Here the "lens law" for effects is tested on fingerprints, not proved. |
| Design by contract (Meyer) | pre/postconditions checked at runtime | The postcondition (state returns to baseline) is generated from the effect and its inverse. |

## Evidence on main
Tree: revl main at commit 45bec431f.

| claim | path:line or test | status | note |
|---|---|---|---|
| Fault test syntax and assertion set | `docs/fault-tests.md:1-165`; `tests/test_fault_tests.py` (58 tests) | shipped | py tier only; other tiers refuse the construct (`docs/fault-tests.md:240-274`) |
| Sweep over every top-level step, exhaustive verdict | `src/revl/fault.py:660,802`; `docs/fault-tests.md:325-366` | shipped | |
| Unreachable steps listed, counted | `docs/fault-tests.md:379-395` | shipped | nested-in-`if` steps arise only from hand-written or imported IR (doc) |
| Provider dependents held out of the bring-up | `docs/fault-tests.md:367-378`; `tests/test_fault_sweep_cross_tier.py:90` | shipped | |
| Emissions reported as irreversible notes | `docs/fault-tests.md:167-198` | shipped | not individually traced into tests |
| Cross-tier sweep, agreement, loud skip | `src/revl/fault.py:1363-1500`; `tests/test_fault_sweep_cross_tier.py` (31 tests) | shipped | |
| Cross-tier executed on py, go, java, rust | `tests/test_fault_sweep_cross_tier.py:370,444,471,484,499,514` (def lines) | shipped, toolchain-gated | each corpus here is one composition (`_two_phase()`), 4 fault points (test lines 441-456) |
| ts and wasm in the sweep | `src/revl/fault.py:913,945` (runner mapping) | designed/partial | No executed test for ts or wasm; the docs sample output lists wasm as skipped |
| Docs sample shows rust and java skipped | `docs/fault-tests.md:495-505` | stale | the tests above show rust and java executing |
| Authenticated residue proof (runner token) | `docs/fault-tests.md:449-460` | shipped | issue 1621 |
| Compensations owed versus ran | `docs/fault-tests.md:462-485` | shipped | service-call and host-builtin compensations "not observed" |
| Verified-effect round trip, 16 rounds | `src/revl/fault.py:1586` (`_ROUNDTRIP_ROUNDS = 16`), `:1900`, `:2208`, `:2276`; `tests/test_verified_effect.py` (23 tests) | shipped | py tier only (`src/revl/test.py` note "py reference tier only") |
| Partial inverse leaving a key is caught | `tests/test_verified_effect.py:278` | shipped | the static check and the fault sweep both miss it (test comment) |
| No-op inverse now refused at compile time | `tests/test_verified_effect.py:262` | shipped | issue 1859 moved this case from runtime to the checker |
| Honesty bound printed | `tests/test_verified_effect.py:291` | shipped | |
| Double-undo value-aware round, negative control | `src/revl/fault.py:1858`; `tests/test_idempotent_inverse_309.py:144-180` | shipped | |
| Shape-proven tier | `docs/crash-recovery.md:303-316` | removed | never built; parse error now |
| Cordis-py async gap found and fixed | `docs/fault-tests.md:297-322`; `tests/test_fault_tests.py:692` `test_an_await_body_lands_failed_like_a_sync_body` | shipped (fixed) | upstream commit named in the doc; the upstream repository was not checked |
| Fault units are py-only in the default `revl test` | `src/revl/test.py:106-114,164,286` | shipped | |

## Numbers that exist
- 16 randomized rounds per verified effect (`src/revl/fault.py:1586`); 64 random rounds in the separate `prop` runner (`src/revl/fault.py:2331`).
- Example sweep output: "swept 5 step(s) across 2 component(s): 5 passed, 0 failed, 0 unreachable" is a documentation illustration (`docs/fault-tests.md:346-365`), not a measurement.
- Cross-tier tests assert 4 fault points agreeing on py and go (`tests/test_fault_sweep_cross_tier.py:441-456`).
- Test function counts (not re-run): fault_tests 58, cross-tier 31, verified_effect 23, idempotent_309 25.
- No measured bug-detection rate, no mutation score, no runtime cost. The seeded-defect saga corpus (`bench/saga_defects`, issue #2736) reports the sweep's corpus-wide result: 16 rows (`missing-undo-hold` and `missing-undo-auth` are one defect kind at two call sites), 8 refused at compile (7 by a checker rule, 1 by a parse error), 4 caught only by the sweep, 4 missed by both; the rows are the authors' own and the Temporal comparison is not run.

## Paper outline
1. Introduction: from "an inverse exists" to "the inverse works".
2. Background: G4, A8 (mid-body failure reverts and contains), R4 (no residue).
3. Fault tests and the injection placement lesson (the vacuous `fail at step 1`).
4. The sweep and its refusal to read nothing as clean.
5. The cross-tier sweep: authenticated proof, compensations owed versus ran, agreement, loud skips.
6. The round trip: fingerprint from the trace ledger, rounds, blind spots; value-aware double-undo.
7. Findings: the async fiber state defect, the false green in the first injection placement, the no-op inverse moved to compile time.
8. Evaluation: a seeded-defect saga corpus with a measured catch point per row (revl half only, `bench/saga_defects`); mutation analysis, baseline and the Temporal comparison are not run.
9. Limits and related work.

## Threats to validity and limits
- The docs' own words: a passing fault test proves things "for this component, on the cordis-py reference tier, at this one injection point" (`docs/fault-tests.md:200-210`); it assumes one tier, one point (until swept), top-level steps only, synchronous inverses observed synchronously, residue as the runtime sees it, and stub stdlib (Pool, Map, Job are deterministic fakes).
- "Ran" for an inverse that returns an awaitable means "was invoked", not "completed" (`docs/fault-tests.md:226-228`).
- The round trip is "a test the author did not write, not a proof" (`docs/verified-effect.md:25-31`), py reference tier only, with in-process state only. Blind spots: aliased references, external emissions, clock or random values (`docs/verified-effect.md:163-179`). No shrinking: the first failing round is reported.
- The fingerprint is derived from the trace ledger, so anything the ledger lacks is invisible by construction.
- The six-runtime claim is in contribution 3 and the tier map (`src/revl/fault.py:913`), not in the title. Main shows executing tests for four (py, go, java, rust), toolchain-gated, each on one small composition. ts and wasm are mapped in code but have no executed test.
- The Cordis finding is a single instance, and the upstream fix is cited by the doc, not verified here. The doc says the reproducer had "no revl code in the loop", but it is a hand-built IR emitted and plugged directly (`docs/fault-tests.md:297-322`).
- The sweep uses one failure model; success on it does not extend to crashes (see P15) or to concurrent schedules. Its own blind spots, per `docs/fault-tests.md:449-460`: on the in-process py tier a body that calls `os._exit(0)` still ends the checker with status 0, and host code that reads the runner's memory is not covered.
- That a shape-proven idempotent tier cannot be derived soundly is a limitation, not a result: declarations stay fail-closed with a tripwire test.
- The corpus-wide sweep result depends on provider state being observable. A wrong inverse is caught when the provider is a revl `Map` (`wrong-release-sku`) and missed behind an opaque host body (`wrong-refund-order`), so the caught count is optimistic for an external service.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 40 checked, 9 corrected.
