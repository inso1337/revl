---
id: P42
title: "Intent-vs-Action Refinement: Refusals That Name the Intent an Agent Action Exceeds"
theme: security
kind: short
maturity: shipped-evidence
status: The compile-time half and the approval-gate half are shipped with tests and a controlled 4-by-2 matrix; the lease path and a verb vocabulary at the gate are open, and no realistic case study exists.
fused_with:
---

## Pitch
An agent's action is refused not only for lacking a capability but for exceeding the specific intent its operation declared, and the refusal names that intent, the declared value, the requested value and the source line, with controls that show the refusal comes from the declaration and not from the boundary.

## Abstract
Capability systems answer whether an agent may reach a resource. They rarely answer whether the action about to run is the one the operation was approved to do. A caller authorized to write under a directory can be steered by injected content to write somewhere else, spend more, act for another tenant, or call an unrelated capability; the capability check passes and the refusal, if any, names the capability reached, not the intent exceeded. We describe intent-vs-action refinement in a typed capability language. An operation declares an intent (`within`: object, verbs, ceilings, tenant, explicitly related objects, named scopes). Each crossing in its body states what it does (`acting`). A pure kernel `refine(intent, action)` returns either nothing or the first dimension exceeded, with declared and requested values. The kernel fails closed on omission: an action that states no tenant against a stated tenant, no amount against a stated ceiling, or an amount against no ceiling is refused. The check runs at compile time over the body, including direct extern calls, value-position emissions and helper hops, and at run time at the approval gate, where a standing grant is compared as a declaration and an operator's mint is bounded by a declared `may mint` line. The exit criterion is pinned as a 4-by-2 matrix (wider verb, higher amount, different tenant, extra capability; emit step and let binding) with controls. By the project's own mutation figures, disabling the check turns 16 of 26 cases red and dropping only the naming turns 8 red. We state what is not covered: lease paths, a verb vocabulary at the gate, and runtime counts.

## Problem and claim
Authority checks in agent systems compare an authority with a request, both computed at the same moment. The refusal can name the capability reached. It cannot name the intent exceeded, because nothing holds an intent across time. The design note shows the same four dimensions (object, tenant, verb, ceiling) checked today in four places with four vocabularies, none against a stated declaration (`docs/design/470-intent-refinement.md:25-69`).

The claim: if an operation declares its intent in the same capability spelling the rest of the system already speaks, and each crossing states what it does, then refinement becomes a single pure decision with a first-exceeded-dimension answer, and the refusal can carry the operation, the declared value, the requested value and the source line. The scope is explicit: "refinement checking against a declared intent, not inferring an intent the caller never stated" (`docs/v2.0-roadmap.md:1926`, quoted from the item text). Nothing is inferred.

The property that matters is not the refusal of the wider case but the refusal of the silent one. A declaration that says nothing about a dimension must not read as permission. The kernel makes every field required, refuses unknown-against-stated and spend-against-unstated, and the surface refuses a body that declares an intent and then crosses without stating itself.

## Contributions
1. A pure kernel: `Intent`, `Action`, `Refusal`, `Violation` and `refine`, a leaf module over `cap_order.covers`, with six violation kinds and a fixed order of dimension checks (`src/revl/intent.py:106-137`, `:594-660`).
2. A source surface: `within` on a service operation and `acting` on an `emit` step or a `let` binding, both contextual keywords so existing programs compile to byte-identical IR (`docs/design/470-intent-refinement.md:221-296`; `src/revl/lower.py:15320`, `:15375`, `:15482`).
3. A completeness check so a declaration cannot be bypassed through a direct extern call, a value-position emission or a helper hop (`src/revl/lower.py:15549`; `docs/design/470-intent-refinement.md:297-340`).
4. The exit criterion as an executable 4-by-2 matrix with two controls: every refused program compiles once the declaration is removed, and a refining crossing still compiles (`tests/test_470_exit_criterion.py`).
5. The runtime half: a standing grant compared as a declaration at the class-(c) approval gate, which repaired the grant-side defect caused by ceiling erasure at mint (one erasure shape on both grant predicates, `docs/design/470-intent-refinement.md:403-407`), and a `may mint` bound on what an operator may mint (`docs/design/470-intent-refinement.md:393-500`; `src/revl/mcp/session.py:7767-7860`; `src/revl/mcp/operator.py:325`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Refinement types (Freeman and Pfenning, PLDI 1991; Liquid Types, Rondon et al., PLDI 2008) | Types carry predicates checked by a solver | The predicate is a fixed structure over a capability order, no solver; the refusal is a diagnostic naming a declaration, and the declared object is a point in the system's one partial order |
| Design by contract (Meyer, Eiffel) | Pre and postconditions on routines | The contract bounds what authority an operation may exercise, checked per crossing, not functional correctness |
| Macaroons and caveats (Birgisson et al., NDSS 2014) (citation to be checked) | Tokens attenuated by caveats checked at use | Caveats are checked on a presented token. Here the declaration is in the source, checked at compile time and at the grant gate, and the missing-statement case is refused |
| Spending-limit capabilities, restricted API keys (Stripe, Plaid) (citation to be checked) | Server-side limits on a key | Limits are provider configuration. Here the limit, verb and tenant are in the program, in the same spelling as the capability |
| Policy languages (Rego, Cedar) (see the [related-work table](../related-work.md)) | Attribute-based allow rules evaluated per request | The repo's own boundary-policy grammar is flat globs with no verb or ceiling (`docs/design/470-intent-refinement.md:49-56`). Policy languages decide per request; here the declaration is held with the operation |
| Intent-centric systems in blockchains (citation to be checked; weak analogy) | A user states an outcome, a solver finds actions | Opposite direction: nothing is solved or inferred, the declared intent bounds the action |
| Confused deputy (Hardy, 1988) (citation to be checked) | Motivating problem | Addressed as a refusal naming the declaration instead of an ambient-authority argument |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Kernel exists, leaf module over `cap_order` | `src/revl/intent.py:106-162`, `:594-660`; `docs/design/470-intent-refinement.md:110-124` | shipped | 938 lines. Six `Violation` values. Dimension order: object, verb, ceiling, scope, tenant |
| Fail-closed omission rules | `docs/design/470-intent-refinement.md:124-145` | shipped | Three asymmetries; every field required |
| `related` objects and set-valued `scopes` | `docs/design/470-intent-refinement.md:147-220` | shipped | Recipient type is explicitly out of scope |
| `within` and `acting` clauses, refusal naming operation, declared value, line | `src/revl/lower.py:15320`, `:15375`, `:15482`; `tests/test_470_exit_criterion.py:99-114` | shipped | Self-host gate does not parse the clauses (`docs/design/470-intent-refinement.md:286`) |
| Completeness over extern calls, value emissions, helper hops | `src/revl/lower.py:15549`; `tests/test_470_intent_surface.py` | shipped | 41 test functions in the file |
| Byte-identical IR for programs without the clauses | `docs/design/470-intent-refinement.md:294`, `:333`, `:391` | shipped (claimed in design) | The IR comparison was not run for this draft; the doc says it is pinned "for the third time" |
| Exit criterion as a matrix, both slots, four excess kinds | `tests/test_470_exit_criterion.py:121-163` | shipped | 26 parametrized cases (3 tests x 8 plus 1 test x 2). 4 test functions |
| Matrix is non-vacuous (mutation numbers) | `docs/v2.0-roadmap.md:1926` | shipped (self-reported) | The roadmap states the check disabled turns 16 of 26 red with both controls green, and the naming dropped while still raising turns exactly the 8 naming cases red. Not rerun for this draft |
| Standing grant compared as a declaration, ceilings kept at mint | `src/revl/mcp/session.py:7767`, `:7814`, `:7856`; `docs/design/470-intent-refinement.md:393-500` | shipped | 22 tests in `tests/test_470_approval_gate.py` |
| Refusal reaches the operator's prompt | `src/revl/mcp/session.py:8026-8084` (`standingGrantRefusals`) | shipped | Design note cites issue #1098 |
| `may mint` bound at mint | `src/revl/mcp/operator.py:325`, `:408`; `docs/design/470-intent-refinement.md:501-589` | shipped | Grandfathered: an operator with no line mints as before |
| Lease path | `docs/design/470-intent-refinement.md:605-620` | designed | Open and stated; no declaration is in scope where a lease is acquired |
| Verb vocabulary at the class-(c) gate | `docs/design/470-intent-refinement.md:621-642` | designed | Open. The gate uses a sentinel verb on both sides |
| Kernel unit tests | `tests/test_470_intent_refinement.py` | shipped | 71 test functions |

## Numbers that exist
- Matrix: 4 excess kinds by 2 stating slots = 8 refused programs, 26 parametrized cases (`tests/test_470_exit_criterion.py`, counted from the parametrization).
- Mutation figures, from the roadmap and not rerun: check disabled turns 16 of 26 red with both controls green; naming dropped turns exactly the 8 naming cases red (`docs/v2.0-roadmap.md:1926`). The 16 is the 8 refusal cases plus the 8 naming cases, which is consistent with the test layout (an inference from the layout, not a stated fact).
- Test counts by file: 71 kernel, 41 surface, 22 approval gate, 4 exit criterion (function counts).
- No performance numbers, no false-positive rate on real agent programs, no user study, no count of attacks stopped.

## Paper outline
1. Introduction: the capability check passes, the action is still wrong. One example with an injected path.
2. The gap in existing checks: four dimensions, four places, no declaration.
3. Design: Intent, Action, refine; dimension order; the three fail-closed asymmetries; related objects and scopes.
4. Source surface: `within`, `acting`, stating slots, completeness, contextual keywords.
5. Runtime half: a grant as a declaration; erasure at mint and why it hid two bugs; `may mint`.
6. Evaluation: the 4-by-2 matrix, controls, mutation figures, case study.
7. Limits: spellings versus counts, leases, verbs at the gate, no inference.
8. Related work.

## Threats to validity and limits
- The amount check compares two spellings, so a crossing spelled `calls=1` inside a loop is bounded at runtime by `remainingUses` (section 3.6 of the design note), not by this check; "a higher amount is refused" is a statement about declarations (`docs/v2.0-roadmap.md:1926`; `docs/design/470-intent-refinement.md:712-716`). Runtime spend is not bounded by this check.
- The matrix proves the four named excess kinds on a handful of program shapes, not that the declaration language can express every real intent.
- Mutation numbers are self-reported by the project.
- The self-host compiler does not parse `within` or `acting` (`docs/design/470-intent-refinement.md:286`).
- The kernel decides only over what the caller states. The design says it does not infer intent and treats recipients as a caller-supplied set with no recipient type (`docs/design/470-intent-refinement.md:643-659`).
- The grandfathering of operators with no `may mint` line leaves the old unbounded mint in place until adopted (`docs/design/470-intent-refinement.md:569-580`, `:634-637`).
- Both open stages (lease, gate verbs) are real gaps for an end-to-end claim.
- Author-supplied `acting` can be wrong in a way the kernel cannot see: the object and amount are read from the crossing's own spelling, but the verb, tenant and scopes are what the author states. A mislabelled verb is not caught.
- A reader may take this as a contract system with a custom syntax; the paper has to show the surface is thin on top of one kernel.
- Not yet done: rerun the mutation figures on a pinned commit, a case study with adversarial inputs, annotation-burden measurement, and a formal note on monotonicity of `refine`.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 30 checked, 5 corrected.
