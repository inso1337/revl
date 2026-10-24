---
id: P59
title: "A System May Evolve Behaviour, Never the Rules Governing Its Authority: The Admission Kernel as a Capability, and Honest Rollback of Evolved Layers"
theme: agents
kind: full
maturity: partial
status: Controller, kernel capability, layer-state registry and promotion barrier ship on main as a first slice with synthetic differential corpora; stage adapters, a computed authority diff and lifecycle wiring of rollback are designed, not written.
fused_with: P60, P61
---

## Pitch
A self-evolving system's central safety rule, "it may change its behaviour but not the rules governing its authority", can be made structural: enumerate the admission kernel as capability tokens, refuse any candidate whose effective authority meets them, and put the authority check in control flow ahead of every measurement.

## Abstract
Self-improving agent systems usually state their safety boundary as a policy: a list of files the loop must not edit, or a weight that makes an authority regression expensive. Both fail in the same way. The loop's proposal channel is the mechanism for proposing changes to rules, so a policy the loop can edit is not a boundary, and a weight is one edit from being tuned away. We describe a governed self-evolution lifecycle implemented in the revl language and compiler. The invariant is checked three ways, and the first has a known hole for host code. First, the admission kernel (the admit decider, attestation, taint lattice, retention discipline, the gate crate and the formal development) is enumerated once as seven capability tokens, and the compiler's attenuation product refuses any component whose effective authority is not disjoint from them, including an undeclared reach, which is read as unbounded rather than empty. Second, a controller walks four preconditions (trigger, propose, admit, authority) and returns on the first unmet one before any measured result (shadow, canary, observe) is read, so a perfect canary cannot buy a widened capability. Third, a layer-state registry classes the state of seven evolvable layers as revertible, compensatable or neither, and a rollback report separates what was restored from what was compensated and what was not. On main, a differential corpus shows 8 programs admitted without the kernel check and refused with it, with one control admitted in both. We state what is not yet built: stage results are supplied rather than produced, and termination is bounded per proposal rather than proved.

## Problem and claim
Existing self-improvement work (a Godel-machine style self-rewrite, or an agent that edits its own scaffold) treats the safety boundary as something the system is told. If the thing being improved can propose any change to the repository, then "do not touch the admission rules" is a rule that a later generation can propose a change to. The design note for the kernel capability says this directly (`docs/design/545-kernel-boundary-capability.md:20-34`). A file-diff check is a necessary and cheap defence, but its own design note states that it "cannot refuse a candidate that changes kernel behaviour through a path that touches no enumerated file" (`docs/design/545-kernel-boundary-capability.md:36-58`).

A second weakness is ordering. A promotion rule that reads canary numbers and an authority diff in the same scope can weigh one against the other. Static properties such as capability reach and taint edges are exactly the ones a 1 percent canary does not exercise, so the canary is the wrong evidence for them (`docs/design/537-evolution-controller.md:129-150`).

A third weakness is the word "rollback". Reverting a component restores its effects, but not an accumulated routing table, trust score or tuned threshold. A system that reports "rolled back" after reverting a layer that learned state has made a false statement.

Claim: the invariant is enforceable by (a) a capability enumeration folded by the same disjointness predicate the compiler already uses for parallel emissions, (b) a barrier expressed as a return statement rather than a weight, and (c) a rollback report whose vocabulary cannot say "rolled back" for accumulated state. The claim is about enforcement structure, not about the completeness of the kernel list.

## Contributions
1. The kernel as a capability set: `KERNEL_CAPS` has seven tokens (admission, attest, taint, retention, gate, census, formal), each citing the guarantee it defends and the tree paths it stands for (`src/revl/kernel_boundary.py:139`). A component whose effective set meets them is refused by name, folded with `cap_order.disjoint` rather than a new algebra (`docs/design/545-kernel-boundary-capability.md:109-125`).
2. Retention as a kernel authority: a retention deadline is a kernel token, and an untrusted author may not declare a retention policy at all (`src/revl/admit_profile.py:368`, `docs/design/545-kernel-boundary-capability.md:172-207`).
3. A seven-stage lifecycle with a barrier in control flow: four preconditions, three measurements, one logged verdict with three outcomes (`tools/evolution_controller.py:171-177`, `:882`), with the file-level authority diff measured from git rather than accepted from the candidate (`tools/evolution_controller.py:676`). The axis-level diff is still declared by the caller.
4. A shared promotion-barrier rule with a tree sweep that found two promotion paths nobody had registered (`src/revl/promotion_barrier.py:1-60`).
5. A layer-state registry whose aggregate class is the weakest part, in which an undeclared part resolves below `neither`, and a four-outcome rollback report (restored, compensated, uncompensated, untouched) (`src/revl/layer_state.py:197`, `:324`, `:667`).
6. A differential non-vacuity method: each check is run against a corpus with the check neutralised and live, with a control that must be admitted on both sides.

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Godel Machine (Schmidhuber) (citation to be checked); Darwin Godel Machine (citation to be checked) | Self-modification gated by a proof or by empirical benchmark improvement | The gate is a static capability refusal of the candidate's authority, not a proof of utility, and the rules of the gate are named as kernel tokens that a candidate's reach is folded against; host code that reaches the kernel without naming it is outside the fold (`docs/design/545-kernel-boundary-capability.md:298`) |
| STOP, Self-Taught Optimizer (citation to be checked); Voyager skill library (citation to be checked) | A scaffold or skill library improves itself, with sandboxing as the boundary | The boundary is a compile-time admission product over declared authority, with undeclared reach counted as maximal |
| AlphaEvolve (citation to be checked) | Evolutionary code search with an automated evaluator | The judge and its test file are fenced from the candidate (`AUTHORITY_FENCE`, `tools/evolution_controller.py:210`, which lists those two paths only; the evaluator in general is not fenced) |
| Constitutional AI (citation to be checked); reward-hacking literature | Constraints expressed in training signal or rubric | Constraints expressed as refusals ahead of any measurement, so no score can compensate |
| Capability systems (object-capability languages, seL4 (see the [related-work table](../related-work.md))) | Authority as unforgeable references | Same idea applied to the evolution loop's own governing code: the kernel is a named token set and "can this component hold authority that reaches the kernel" is a compile-time question |
| MLOps registries and rollback (MLflow, feature stores) (citation to be checked) | Version and revert model artifacts | Distinguishes revertible, compensatable and neither classes of layer state and refuses to name an uncompensated revert "rolled back" |
| Database PITR and event sourcing | Restore state by replay or snapshot | Applies only where the layer declares an inverse or a tested compensation; accumulated statistics are classed as having no inverse |
| Argo Rollouts and canary analysis (citation to be checked) | Metric-driven progressive delivery | Metrics may not override a static authority diff; the comparison is unwritable because the values are not in scope |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Kernel enumerated as seven capability tokens with guarantee and paths | `src/revl/kernel_boundary.py:139`, paths at `:107` | shipped | `KERNEL_PATHS` has ten entries; seven caps |
| Refusal folded with disjointness, fails toward refusing, unnameable `*` is disjoint from nothing | `src/revl/kernel_boundary.py:269`, `src/revl/lower.py:18007`, `docs/design/545-kernel-boundary-capability.md:109-170` | shipped | Scope switch `undeclared_reaches_kernel` is a required keyword |
| Untrusted author may not declare retention | `src/revl/admit_profile.py:368` | shipped | |
| Differential corpus: 8 admitted before, 8 refused after, 1 control admitted on both | `tests/test_kernel_boundary_544.py` (29 test functions); numbers in `docs/design/545-kernel-boundary-capability.md:209-237` | shipped | Not run here; counts are the design note's |
| Undeclared emission reach arm is live | `docs/design/545-kernel-boundary-capability.md:243-287` (issue #2105), `docs/design/561-undeclared-emission-boundary.md` | shipped | The note keeps the older residual text beside the update |
| Controller: four preconditions, three measured stages, returns before measurements read | `tools/evolution_controller.py:171-177`, `:882-935`; `tests/test_evolution_controller.py` (45 test functions; the design note says 77 tests counting parameters) | shipped (slice 1) | The v2.0 roadmap entry reads "PARTIAL, SLICE 1 LANDED" (`docs/v2.0-roadmap.md:1978`) |
| Authority diff is measured from git; declared diff that understates is `DIFF_DISAGREES`; unreadable tree cannot promote | `tools/evolution_controller.py:676-700`, `docs/design/537-evolution-controller.md` section 7 | partial | File-level diff from git, `DIFF_DISAGREES` and `DIFF_UNVERIFIED` are on main. Axis-level diff (capability, taint, budget, realm, retention) is still caller-declared (roadmap entry marked "Open") |
| Control comparison: an any-green rule promotes the widened tree, the controller does not | `docs/design/537-evolution-controller.md:356-384` | shipped | |
| Shared barrier rule and sweep over promotion paths | `src/revl/promotion_barrier.py`, `tests/test_promotion_barrier_543.py` (25 test functions) | shipped | Registry holds four paths per the module docstring |
| Layer-state registry, ceilings, weakest-part aggregate, four-outcome report | `src/revl/layer_state.py:197,324,602,667`, `tests/test_layer_state_546.py` (48 test functions) | partial | Slice 1 only; `untouched` is not checked and `tested` is a boolean the author writes (`docs/v2.0-roadmap.md:2035`) |
| Nine plans admitted by the effects-only rule and refused by the registry, across seven distinct links | `docs/design/547-evolvable-layer-state.md:327-335`; `effects_only_admits` in `tests/test_layer_state_546.py` | shipped (test-encoded baseline) | Doc count. The "prior rule" is re-encoded in the test file; no shipped predecessor exists to compare against |
| Seven things the loop may not do (grant capability, disable taint, widen secret access, remove approval, change rollback semantics, replace the kernel, alter "committed") each mapped to a refusing mechanism | roadmap text (`docs/v2.0-roadmap.md:1978-1982`) | designed | No code under `tools/` or `src/revl/` enumerates these seven as a set. Only some have a mechanism (grant refusal, taint, kernel) |
| Termination | roadmap text: "termination is bounded per proposal, not proved" | partial | Controller carries `attempt` and `budget` |

## Numbers that exist
- 8 programs admitted without the kernel check and refused with it, 1 control admitted both ways, plus a ninth refusal for the retention declaration: `docs/design/545-kernel-boundary-capability.md:209-237` (corpus of 9 plus control, a design-note count; the test file has 29 functions).
- Controller non-vacuity: 8 cases (4 preconditions, missing result and emptied evidence), 8 refusals; a complete proposal promotes; the any-green control promotes 3 of 3 while the controller returns PROMOTE, REFUSE, REFUSE: `docs/design/537-evolution-controller.md:356-384`. The note says 77 tests; the file defines 45 test functions (parametrisation accounts for the difference, not verified).
- Layer-state: nine plans admitted by the effects-only rule (as re-encoded in the test file) and refused by the registry, across seven links; five controls admitted by both: `docs/design/547-evolvable-layer-state.md:327-340`.
- Measurement history: the kernel note reports that its own earlier measurement (14 tests across 7 files) was wrong, re-measured as 13, 17 and then 9 across 4 files, and finally 7 reds across 2 files when the arm was taken (`docs/design/545-kernel-boundary-capability.md:243-287`). Usable as a methods anecdote.
- No evaluation on an external corpus, no false-positive rate on real candidate streams, no counts of autonomous cycles run.

## Paper outline
1. Introduction: the invariant, why a policy is not a boundary, claim and contributions.
2. Background: revl admission, attenuation product, guarantee codes G8/G9/G-RETAIN.
3. The lifecycle and the barrier: seven stages, the three ways to write the barrier and why two are wrong.
4. The kernel as a capability: enumeration, disjointness fold, failure direction, retention case, scope switch.
5. Honest rollback: layer classes, ceilings, weakest-part aggregate, four outcomes.
6. Non-vacuity as method: neutralised-check differentials and controls.
7. Evaluation (to be run): proposal corpus, red team, rollback accounting.
8. Limits and related work.

## Threats to validity and limits
- The docs say the kernel enumeration is a name for what the compiler can see: "Nothing here refuses a kernel reach made by host code" (`docs/design/545-kernel-boundary-capability.md:298`). Host code is outside what the fold can see; the profile answers with `no_extern` and `check_no_host_extern_reach`.
- "The `*` arm is dormant for a candidate's own reach on `main`" because earlier checks close both routes; the claim that it bites rests on an injected test.
- Of the kernel capability design note: no service in the tree declares a `kernel.*` token, by intent, so the corpus is synthetic.
- Controller slice 1: stage results are supplied, the axis diff is declared by the caller, termination is not proved (`docs/v2.0-roadmap.md:1978`).
- Layer-state slice 1 does not check an `untouched` claim and reads `tested` as author-written (`docs/v2.0-roadmap.md:2035`). Later slices are designed only.
- The roadmap lists two requirements with no owner: a control plane separated from the data plane, and an externally governed root of trust (`docs/v2.0-roadmap.md:1980`). A reviewer will ask who governs the kernel enumeration; the answer today is the maintainers editing a file on the fenced list.
- The design docs admit several of their own measurement errors; counts should come only from re-run tests.
- The seven non-evolvable surfaces are a roadmap paragraph on main, not code.
- No evaluation of proposal streams, red-team study or rollback accounting has been run; those are the next results needed.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 19 checked, 10 corrected.
