---
id: P19
title: "Counterfactual Incident Replay: Asking What a Different Policy or Action Would Have Changed, With Zero Live Effects by Import Shape"
theme: effects-replay
kind: full
maturity: partial
status: "Policy reader over a WAL and action reader over a session log are shipped with tests; no incident corpus, fidelity measurement or causal-cone computation yet."
fused_with: P18, P20, P21
---

## Pitch
Because revl's admission rules and action classifier are pure functions, a logged incident can be re-graded offline under another policy, or with one agent action swapped, and the policy reader imports no runtime tier, so nothing in it can fire an effect.

## Abstract
Post-incident questions of the form "would the new policy have stopped this" are usually answered by re-running the incident or by reading a policy simulator that never saw the incident. Both are unsatisfying for agents that act on the world: a re-run repeats the effects, and a simulator answers about a hypothetical rather than about the logged run. We describe two counterfactual readers in revl, a language whose compiler and gate mediate an agent's capability use. The first, `revl replay WAL --under POLICY`, regrades the boundary crossings logged in a write-ahead log under an alternate policy and a candidate composition, names the earliest crossing the new policy would have refused, and reports whether the candidate is admitted. The second, `revl_counterfactual`, takes a session's action log, replaces, inserts or drops one action, decides both arms with the gate's own pure classifier and ticket rules, and reports the first divergence and the downstream steps it changes. Neither module imports the runtime tier. For the policy reader, a test runs a full counterfactual in a fresh interpreter and asserts the runtime never enters the module table, and another pins the WAL digest equal before and after. The action reader is pinned only by a test that makes runtime calls raise, and eight of its nine tests are skipped unless the cordis package is installed. We state what the approach cannot say: the WAL holds no admit decision and no host response, so the first reader recomputes reach rather than re-deciding the crossing, and the gap for a free host-extern emission depends on the tier that wrote the log: on the Python tier such an emission is now recorded (`backends/python/runtime.py:1388-1414`), while the bound the reader prints still says it leaves no record, so for those logs the printed text is stale; other tiers were not checked. The implementation is covered by 14 tests for the policy reader, 9 for the action reader and 26 for the related policy-diff.

## Problem and claim
Incident review asks counterfactual questions: would policy B have refused the crossing that hurt us, would this candidate component have been admitted, would the gate have decided differently had the agent done something else at step k. Existing answers either re-execute (unsafe for an agent whose actions are real) or evaluate the policy on synthetic inputs (not about the incident).

The claim is narrow and checkable: when the decision procedure is a pure function of logged data and static compiler output, a counterfactual can be answered with no effect authority, and the absence of authority can be a property of the code (no import path to a runtime) rather than a discipline. The claim is paired with an equally precise statement of where the answer is only a recomputation. The design doc says so itself: the crossing report recomputes the alternate policy over the reach the log names rather than re-deciding the crossing that was logged (`docs/design/467-counterfactual-replay.md`, section "What this surface cannot answer").

On main, the WAL reader recomputes reach; it does not re-decide the crossing and it does not compute a causal cone. The session-log reader does re-decide, with the gate's own classifier, and reports downstream steps changed by an approval shifting, but over the in-memory session log, not a WAL.

## Contributions
1. A WAL reader that regrades every logged emission crossing, in logged order, as `permitted`, `refused` or `not-in-candidate` under an alternate policy, names the earliest refused crossing, and separately reports the whole-policy admission verdict for a compiled candidate (`src/revl/counterfactual.py:375` `replay_under`; CLI `src/revl/cli/parser.py:1951-1965`).
2. A structural zero-effect property for the policy reader: no runtime import, WAL digest equal before and after, and a report field stating `liveEffects: 0`, each pinned by a test (`tests/test_467_counterfactual_replay.py:499`).
3. A catalogue of printed bounds that the answer cannot cross (authority absent from the log, free host-extern emissions on a tier that does not record them, reach-leg only per crossing, torn WAL, realm rules without a candidate), carried on every report rather than in prose.
4. A single-action substitution reader over a session's action log (`replace`, `insert`, `drop`) that decides both arms with the gate's pure parts, shifts approvals with the surrounding calls, and reports per step whether the original arm reproduces the receipts, so a divergence is not read off an arm that did not reproduce (`src/revl/mcp/counterfactual_act.py:52-226`, `tests/test_counterfactual_act_1752.py`).
5. A policy-diff preview over a WAL with a bounded blast radius and a refusal to report "unchanged" on any leg the WAL cannot decide (`src/revl/policy_diff.py`, `tests/test_468_policy_diff.py`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| OPA / Rego (`opa eval`, conftest) policy testing | Evaluates a policy on supplied input documents | Input is the logged incident plus a compiled candidate; output names the first crossing of that incident, in logged order |
| AWS IAM policy simulator and Access Analyzer | Simulates requests against attached policies, reasons about reachability of resource policies | Works over an agent's logged crossings and a candidate composition; refuses to answer when the token is not recoverable instead of guessing |
| Cedar policy equivalence analysis (see the checked [related-work table](../related-work.md)) | Reduces policy-equivalence questions to SMT and can produce a counterexample request when two policy sets differ | revl does not prove policy equivalence; it grades a concrete log and states what the log lacks |
| Pernosco / rr replay debugging | Replays a deterministic execution to inspect it | The replay here is not an execution at all; no component runs |
| Chaos-engineering and incident replays (traffic replay) | Re-send logged traffic to a staging system | No system receives traffic; re-grading is offline |
| Counterfactual explanations in ML (Wachter et al.) (citation to be checked) | Smallest input change that flips a model output | Here the change is chosen by the operator (one action, one policy) and the oracle is a pure gate, not a model |
| Datalog policy analysis | Static queries over policy and configuration | Combines a static query with a logged incident ordering |

The Cedar row was checked against its primary paper in the [related-work table](../related-work.md); the other rows still need a proper citation pass.

## Evidence on main
Tree: revl main at commit 45bec431f.

| Claim | path:line or test | Status | Note |
|---|---|---|---|
| `revl replay WAL --under POLICY [--candidate FILE...]` exists | `src/revl/cli/parser.py:1951-1965`; `src/revl/counterfactual.py:375` | shipped | First slice |
| First refused crossing is the earliest in logged order; admission verdict is separate | `tests/test_467_counterfactual_replay.py:165`, `:210` | shipped | |
| Without a candidate the question is "not answerable", exit 1 | `tests/test_467_counterfactual_replay.py:302` | shipped | token is not on the WAL |
| Refusal with no logged crossing behind it is reported as such | `tests/test_467_counterfactual_replay.py:260` | shipped | |
| Torn WAL: intact prefix read, `tornWal` bound added | `tests/test_467_counterfactual_replay.py:376` | shipped | |
| Zero live effects: no runtime tier in `sys.modules`, WAL unchanged | `tests/test_467_counterfactual_replay.py:499` | shipped | Only checks the Python reader's own interpreter |
| The report carries the bounds | `tests/test_467_counterfactual_replay.py:392` | shipped | |
| Free host-extern emission recorded in the WAL (issue #841) | `docs/design/467-counterfactual-replay.md` section "What this surface cannot answer"; `backends/python/runtime.py:1388-1414` | shipped (py, cordis-gated) | The Python tier records a direct host-extern emission through `extern_emit` before the host body runs (`backends/python/runtime.py:1388-1414`), and `tests/test_crash_recovery.py:766` asserts exactly one emission step for it (the test is skipped when cordis is not installed). The `freeHostExterns` bound text in `src/revl/counterfactual.py:75-90` still says no record is written, so it is stale for logs written by that tier; other tiers were not checked. The design doc's pointer to `backends/python/replay.py` lines 1328-1339 is stale: that range is unrelated code and holds no #841 comment |
| Gate re-decision of the original admit | same doc, section on authority missing from the log | not built, not intended | The WAL holds no admit decision |
| `revl_counterfactual` replace / insert / drop over a session log | `src/revl/mcp/counterfactual_act.py:52`; `docs/mcp-reference.md:671` | shipped | Second slice, issue #1752 |
| Original-arm reproduction check; runs nothing | `tests/test_counterfactual_act_1752.py:92`, `:173` | shipped (cordis-gated) | |
| Downstream step changes when an approval moves | `tests/test_counterfactual_act_1752.py:129`, `:146` | shipped | |
| Verb advertised read-only | `tests/test_counterfactual_act_1752.py:215` | shipped | |
| Policy-diff over a WAL, newly allowed / denied, bounded blast radius | `src/revl/policy_diff.py:1-80`; `tests/test_468_policy_diff.py:127`, `:179` | shipped, with a major caveat | See next row |
| Policy-diff over a WAL written by today's writers | `src/revl/policy_diff.py` module docstring | partial | The docstring states every WAL written today is unscoped and its action set is empty; such entries are withheld, not reported clean (`tests/test_468_policy_diff.py:272`). The diff is only useful on a WAL with annotated scope |
| Replay readiness analysis | `src/revl/replay_modes.py:86`; `tests/test_250_replay_modes.py` | shipped (readiness only) | A different mechanism from this paper |
| Causal cone of downstream actions from a WAL crossing | none found | missing | The WAL reader does not compute it. The action reader reports only approval-driven step changes |
| Re-execution of either arm against a forked workspace | `docs/design/467-counterfactual-replay.md` "What was left out" | designed, deferred | A live executor |

## Numbers that exist
None as measurements. Test counts only, from counting test functions in the files (not a run): 14 in `tests/test_467_counterfactual_replay.py`, 9 in `tests/test_counterfactual_act_1752.py`, 26 in `tests/test_468_policy_diff.py`. No latency, accuracy or case-study data exists.

## Paper outline
1. Introduction: the incident question and why re-running is not acceptable for acting agents.
2. Background: gate, WAL, capability tokens, boundary policy (`docs/boundary-policy.md`).
3. Policy counterfactual over a WAL: crossings, candidate, verdicts, ordering.
4. Action counterfactual over a session log: classifier, tickets, approval shifting, reproduction check.
5. Zero effects by shape: import discipline, digest pinning, report field, the test with teeth.
6. What the log cannot say: the printed bounds, and why not logging the decision is deliberate.
7. Evaluation: incident corpus, fidelity of recompute, comparison with a generic policy engine.
8. Related work and limits.

## Threats to validity and limits
- The docs name the largest caveat: a crossing reported permitted may have been refused at the time, and one reported refused may have been covered by an approval edge the log does not hold. The design deliberately keeps the WAL free of decisions (`src/revl/recovery.py` holds the same line). This is a design trade, not a defect to be hidden.
- Per-crossing verdicts read only the capability reach leg; taint, register floors, evidence bundles and tenant partitions are heard only in the whole-candidate admission verdict.
- A crossing is identified by `(component, label)`, so a candidate that moves a crossing between scopes under the same label is not distinguishable.
- "Admitted" means the boundary policy admits the candidate, not that the gate would boot it.
- The action reader's reach is the static closure, so a listed crossing may not fire for this data; no result value or data-dependent path is computed; ttl is not simulated; a log spanning a swap is refused.
- The zero-effect test checks the Python module's imports; it says nothing about other tiers, and nothing about the action reader beyond a test that makes runtime calls raise.
- The live half (re-executing arms) is not built.
- Eight of the nine action-reader tests carry the `needs_cordis` skip, so a run without cordis does not exercise them.
- Policy-diff on WALs written today yields an empty action set per its own docstring; it is not a working end-to-end preview without a scoped WAL.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 60 checked, 6 corrected.
