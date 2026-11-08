---
id: P08
title: Cost Ceilings Read Off the IR: Emission Cardinality Bounds and SLO Gates
theme: formal-pl
kind: full
maturity: partial
status: Cardinality analysis, budget ceilings and the SLO contract (compile-time gate, observed half, live producer, rollout gate) ship with tests on main; no precision, soundness-at-scale or overhead evaluation yet, and the check that links an SLO bound to the cardinality verdict is designed only.
fused_with:
---

## Pitch

How many times an agent activation may call its model or tools is a compile-time fact in a capability-typed language, reported with an explicit "unbounded" verdict instead of an optimistic number, and an SLO contract can then be refused or paused against it.

## Abstract

Agent frameworks meter cost after the fact: a runaway loop shows up as an invoice. We show that in a language where every effect crosses a declared capability boundary, an upper bound on crossings per activation can be computed from the IR and published as an audit fact next to the capability table. The analysis counts crossings exactly for non-looping bodies, takes the maximum across branches, certifies a finite ceiling for linear self-recursion with a strictly decreasing literal fuel under a dominating base guard, and reports `unbounded` for everything else, including every branching recursion and every crossing it cannot see through (a first-class emitting arrow, a host extern). An adversarial review caught a recognizer that accepted `f(n-1); f(n-1)` as linear while it crosses `2^n - 1` times; the repaired recognizer requires at most one recursive call site per path. A typed-model retry budget enters the count as a constant factor `N + 1`. Declared budget ceilings are checked against the computed bound, and a dedicated ceiling-attenuation check stops a delegated child from holding a wider budget than its parent. On top sits a composition SLO contract with a compile-time gate (declaration against declaration) and an observed half (measurement over the stored trace and, since slice 5, a live producer inside `Session`; a signed receipt tied to the generation; a rollout gate on the predecessor's receipt). We report what is implemented, what the docs say it does not show, and the evaluation still to run.

## Problem and claim

Runtime cost accounting says what an activation spent. It does not say what it can spend, and it cannot refuse a rollout before the spend. Static resource analyses (RAML, gas analyses) bound cost for functional programs or contracts but are not wired to an audit surface, a delegation algebra, or an SLO contract.

The claim has three layers, each with different evidence strength. (1) A crossing-count fold designed to mirror the existing boundary walk line for line, with an honest `unbounded` verdict; shipped and tested, with soundness argued in the design doc and not proved. (2) Budgets that ride the existing ceiling parameters, checked statically and attenuated on delegation by a dedicated check; shipped. (3) A predicted-versus-observed SLO gate in which the prediction rests on ranked evidence E1 to E4; E1 and E4 are shipped, E2 (refusing an SLO bound over a route the cardinality verdict calls `unbounded`) and E3 (canary witness) are designed only.

The docs themselves limit the claim. The SLO design note says no arrangement of evidence "proves that a future SLO will hold" and lists exactly what the gate can refuse. This draft keeps that framing.

## Contributions

1. A per-capability crossing-count analysis with a named `unbounded` verdict, joined to the boundary surface, with exact counts for non-looping bodies, max over branches, and counts of the `emit` step, the req-target emission call and the spawn-handle provision call (`src/revl/cardinality.py:1-25`, `tests/test_cardinality_260.py`).
2. A bounded-iteration certifier for linear self-recursion, and the adversarial finding that the first version certified tree recursion as linear; the repaired clause (at most one in-SCC call site per path) with regression tests (`docs/design/260-emission-cardinality-bounds.md:3-37`, `tests/test_cardinality_260.py:372,397`).
3. Budget ceilings checked against the computed bound, and a dedicated attenuation check on delegation, with the finding that the existing coverage fold is ceiling-blind (`src/revl/lower.py:17931` for the ceiling-blind comment, `:17939` for `_ceiling_attenuation_check`; `tests/test_budget_260.py`).
4. A typed-model retry budget that multiplies the bound (`src/revl/cardinality.py:49-62`, `tests/test_validated_retry_257s2.py:203`).
5. An SLO contract with a closed datum set, a compile-time gate, a measured observed half, signed receipts tied to a generation, and a rollout gate with an evidence ladder (`docs/design/473-slo-contracts.md`, `src/revl/slo.py:1290`).
6. An account of four stale premises found when the planned work item met the tree, as a methods lesson.

## Related work

| prior system or line of work | what it does | what revl does differently |
|---|---|---|
| RAML / Resource-Aware ML (Hoffmann et al.) (citation to be checked) | Automatic amortized polynomial cost bounds for functional programs | revl counts boundary crossings, not instructions, accepts only linear recursion with a decreasing literal fuel, and says `unbounded` otherwise rather than inferring polynomials |
| Gas analysis for smart contracts (GASTAP) (citation to be checked) | Upper bounds on EVM gas via cost relations | The unit is capability calls, joined to the audit table and to delegation, not gas |
| Termination / ranking function analysis | Prove loops terminate | Only the narrow fuel pattern is recognized; the contribution is the soundness discipline, not generality |
| WCET analysis (citation to be checked) | Worst-case execution time for real-time code | Counts effects crossing a trust boundary; no hardware model |
| Agent framework budget counters (token and step caps) | Runtime counter, abort at limit | Static ceiling with refusal at compile time, `unbounded` verdict, attenuation on spawn, and an attestation of declared budgets |
| SRE SLO tooling: error budgets, burn-rate alerts (citation to be checked) | Monitor an external SLI and alert | The contract is in the composition source, is checked against declarations at compile time, and receipts are signed and tied to a generation; scope is explicitly only what the runtime measures |
| Object-capability attenuation (E, Macaroons, seL4) | Narrow authority on delegation | Ceiling parameters are ordered like capabilities; the point here is the ceiling-blindness bug and its dedicated check |

## Evidence on main

| claim | path:line or test | status | note |
|---|---|---|---|
| Exact counts for non-looping bodies, spawn-handle counted not zero | `tests/test_cardinality_260.py:38,51` | shipped | |
| Branch count is max, not sum | `tests/test_cardinality_260.py:280` | shipped | |
| Loops and recursion are `unbounded` with a reason | `tests/test_cardinality_260.py:154,171` | shipped | |
| Host-extern and first-class-arrow crossings are `unbounded`, never 0 | `tests/test_cardinality_260.py:195,211` | shipped | |
| Linear recursion certifies a finite ceiling; config fuel is `bounded-symbolic` | `tests/test_cardinality_260.py:308,326,346` | shipped | |
| Branching recursion reported unbounded, not linear | `tests/test_cardinality_260.py:372,397`; `src/revl/cardinality.py:1-25` | shipped | recognizer rule at `src/revl/cardinality.py:112-140` (`_path_max_calls`) |
| Non-decreasing fuel, no base guard, unresolvable fuel, mutual recursion stay unbounded | `tests/test_cardinality_260.py:442,455,467,484` | shipped | |
| Agent loop that carries state certifies; arrow in data never dropped; arrow counted per invocation; unbounded attributed to reaching loop | `tests/test_cardinality_agent_loop_1755.py` (7 tests), `tests/test_cardinality_invocations_1757.py` (10), `tests/test_cardinality_reach_1905.py` (8) | shipped | later fixes in commits 92e4dd281, 0be638f42, 212cdbf00, b049295b6 |
| Retry budget multiplies bound by N+1 | `tests/test_validated_retry_257s2.py:203,214,222`; `src/revl/cardinality.py:49` | shipped | |
| Static over-budget refused; finite budget over unbounded body refused | `tests/test_budget_260.py:65,88`; `src/revl/lower.py:17909,17924` (`src/revl/lower.py:17857` starts the check's docstring) | shipped | category `emission-budget` |
| Wider, dropped and grandchild-widened budgets refused by the dedicated check | `tests/test_budget_260.py:149,162,181,204`; `src/revl/lower.py:17939` | shipped | |
| Budget carried into attestation | `tests/test_budget_260.py:231` | shipped for `time`; other kinds not checked | the doc lists `size`, `time`, `requests` accumulators; only the `time` test was verified |
| Cardinality in audit JSON and text | `tests/test_cardinality_260.py:255,267` | shipped | |
| Design doc header says "design, not implemented" | `docs/design/260-emission-cardinality-bounds.md:39` | stale | code and tests ship slices 1 to 3; follow the tree |
| SLO block, closed datum set, compile-time gate E1 | `docs/design/473-slo-rollout-gate.md`; `tests/test_473_slo_rollout_gate.py` (20 tests) | shipped | gates only `p95_latency` (vs `time`) and `max_pending_tasks` (vs `calls`) |
| Observed half: measure, receipt, pause, divert, rollout gate E4 | `src/revl/slo.py:810,1194,1232,1290,1425,1489,1587`; `tests/test_slo_473.py` (44 tests) | shipped | over a stored trace |
| Live producer inside `Session` | `tests/test_slo_473_live_producer.py` (19 tests); table in `docs/design/473-slo-contracts.md` | shipped | slice 5 |
| Rollout refused on predecessor breach, not on absence or unmeasured | `tests/test_slo_473.py:565,632,649,682` | shipped | |
| E2: refuse an SLO bound over an `unbounded` cardinality verdict | `docs/design/473-slo-contracts.md:185` ("designable now, not landed"); no `cardinality` reference in `src/revl/composition.py` | designed | this is the link between the two halves; not built |
| E3: canary witness feeds the SLO gate | `docs/design/473-slo-contracts.md` (E3) | designed | |
| `recovery_time`, `approval_wait` measured | `docs/design/473-slo-contracts.md` ("what is missing" table) | designed | no timestamps on the needed entries |
| `success_rate` measured for one denominator (`of activations`) | `docs/design/473-slo-contracts.md` table, slice 3 | shipped | `of crossings` remains `unmeasurable` |

## Numbers that exist

- Test counts at the cited commit, from `def test_` lines: 22 (`tests/test_cardinality_260.py`), 16 (`tests/test_budget_260.py`), 7, 10, 8 (follow-up cardinality files), 20 (`tests/test_473_slo_rollout_gate.py`), 44 (`tests/test_slo_473.py`), 26 (`tests/test_slo_473_window.py`), 19 (`tests/test_slo_473_live_producer.py`).
- `src/revl/slo.py` is 2091 lines; `src/revl/cardinality.py` is 1187 lines.
- The branching counterexample: `f(n-1); f(n-1)` crosses `2^n - 1` times (design doc, revision paragraph); no experimental count.
- No precision data exists: no figure for how many real components certify `bounded` versus `unbounded` across the examples or the agent-loop fixtures. None yet.
- No overhead, no false-refusal rate, no data on how often the SLO gate refuses.

## Paper outline

1. Introduction: invoices as the only cost signal; the static alternative.
2. Setting: capability boundary, emissions, the boundary walk.
3. The count analysis: exact, max over branches, `unbounded` and `bounded-symbolic`; the soundness tie to the boundary walk.
4. Bounded iteration: the fuel recognizer, the 2^n counterexample, the repair; the agent-loop extensions.
5. Budgets: ceilings on the existing parameter order, static check, the ceiling-blind coverage bug, dedicated attenuation; the retry multiplier.
6. SLO contracts: closed datum set, E1 gate, observed half, receipts, E4 rollout gate; E2 and E3 as the evidence ladder's open rungs.
7. Evaluation: precision study, differential soundness test, replayed breaches.
8. Related work, limits.

## Threats to validity and limits

- The SLO design note says E1 "refuses a rollout against a promise nothing currently checks" because the `time` ceiling has no runtime meter; it is "exactly 'the document contradicts itself' and no more" (`docs/design/473-slo-contracts.md`, E1).
- `max_pending_tasks` backed by a per-activation `calls` ceiling is a unit mismatch, noted in the doc.
- Three of five SLO datums are never gated against a declaration; `recovery_time` and `approval_wait` are not measurable today.
- Multi-function and mutual recursion stay `unbounded` (doc section 5.2, open); symbolic bounds are proof schemas, not numbers.
- Counts are per activation per component, not realm aggregates; a realm-aggregate budget would be a new ceiling kind.
- The analysis trusts the boundary walk; an opaque host body is counted `unbounded` only if the extern is surfaced.
- "Refused because predicted to breach" means one of three narrow things; this draft does not claim to predict latency.
- Lean now covers the crossing count: `RevL.Cardinality.runs_le_bound` proves that any execution of a crossing shape makes at most the number the fold reports (a sequence sums, a branch takes the worse arm, a bounded loop multiplies its body by its iteration ceiling), `body_bound_bounds_runs` ties it to L0's `Body`, and the `CARD` oracle row compares that fold with the number `src/revl/cardinality.py` reports for every finite-bound capability whose crossing command the export can build (the ones it cannot are counted `unmodelled`, never guessed). What is still not proved is the checker's CLASSIFICATION: which call is a crossing, the retry multiplier, and when a loop, recursion or host reach must be reported `unbounded`. The ceiling and budget theorems in `formal/RevL/Theorems/CapCeilings.lean` (`attenuation_monotone`, `lineage_ceiling_le`, `budget_never_exceeds_root_ceiling`) remain different facts and are not evidence for the bound.
- Some design-doc status statements are stale: `docs/design/260-emission-cardinality-bounds.md:39` says "design, not implemented", and `docs/design/473-slo-contracts.md` has a status table (`:29-36`) saying the rollout gate E4 and the live producer landed while other text says the E4 gate is "the one that does not exist" (`:224`) and "There is no producer inside `Session`" (`:521`). This draft cites the current code.
- No empirical precision or overhead data exists yet; all current evidence is unit tests.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 55 checked, 6 corrected.
