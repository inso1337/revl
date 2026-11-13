---
id: P36
title: "No Authority From Untrusted Data: Authority-Tracking Information Flow With a Single Declared Declassification"
theme: security
kind: full
maturity: partial
status: The rule is proved in Lean over an abstract flow model, the checker is shipped, and component-body walk coverage is decided on a corpus; the interprocedural fixed point is unproved, and no agent-benchmark evaluation of G9 exists: the committed AgentDojo pilot measures admission reach, not the taint rule.
fused_with: P56
---

## Pitch
A static checker that treats "untrusted data became authority" as a type error, proves in Lean that, for the modelled flows, the only way across is a declared, granted, audited declassifier, and was itself caught, by executed exploits, walking fewer paths than its own rule covers.

## Abstract
Prompt injection is an information-flow problem: text from a tool result or fetched page reaches a position where a value is authority, such as a shell command, a capability name or a policy update. We describe G9, a compile-time refusal in the revl language that tracks origin labels (web, fs, input, secret and others) over a powerset lattice with join as union, marks authority sinks by a `Trusted[T]` parameter or by a derived sink class, and refuses any flow whose label is not empty unless a declared declassifier sits on the path. The checker has two static declassifiers, a verified parser returning `Trusted[T]` and `endorse`. The origin form `endorse[origin]` must be granted by the enclosing declaration, clears only its own origin, and appears as an audit token that the drift gate treats as a widening; an originless `endorse` is accepted and clears every origin, which the Lean model does not cover. A bound provider key (`secret`) has no declassifier at all. We prove in Lean 4 that an admitted flow into an authority sink carries an explicit declassification of that very origin (`no_authority_from_untrusted`), that label growth is monotone absent one (`declassification_is_the_only_escape`), and that an `endorse` whose origin the enclosing declaration does not grant cannot discharge a flow (`declassifier_must_be_declared`), with anti-vacuity theorems (`g9_not_vacuous`, `secret_rules_not_vacuous`, and three anti-tautology guards) after a review found other guarantees in the same development to be tautologies. We then report what the proof did not cover: two real checker bugs (a component activation body never walked; a `Secret[T]` receiver body seeing a stripped value) and a cross-call laundering hole through `endorse` helpers. A coverage theorem over a component-body calculus, decided against the real checker on a corpus, addresses the first two for component bodies; it is not a proof about the Python checker. The interprocedural fixed point remains unproved. The committed AgentDojo pilot measures admission, not G9, so we still do not report a G9 agent-benchmark evaluation.

## Problem and claim
Agent frameworks that defend against prompt injection mostly do so at run time (filters, spotlighting, separate model roles) or by policy on tool calls. The repo's own threat note says confinement removes ambient authority, but leaves "injection that stays within already-granted capabilities" as residual risk 1 (`docs/prompt-injection-resistance.md:253`, `docs/prompt-injection-resistance.md:263`; `docs/design/249-taint-provenance.md:40-50`). If a component legitimately holds a shell emission and fetched text steers what goes through it, confinement is satisfied and nothing complains.

The claim is narrow and checkable. In revl, untrusted-origin data cannot reach a position that confers authority unless a declassifier granted by the enclosing declaration sits on the path, and this is a compile error (tag G9), upstream of every runtime. The Lean model covers the `endorse[origin]` form only. The same label algebra carries a `secret` origin that has no declassifier and a `confidential` origin with exactly one (`endorse[confidential]`).

The second claim is methodological: the proved rule and the shipped checker diverged in the part nobody proved, which is which paths the checker walks. The bugs that mattered were walk bugs, not rule bugs, and the paper reports how that was found, stated and partly closed.

## Contributions
1. A label lattice (powerset of origins, bottom trusted, join union) with sink classes (authority, disclosure) and two static declassifiers, a verified parser returning `Trusted[T]` and `endorse`. The origin form `endorse[origin]` must be declared in the enclosing declaration and clears only its own origin; an originless `endorse` clears every origin (`src/revl/taint.py:22-27`, `src/revl/taint.py:2080-2100`, `src/revl/taint.py:2167-2172`; `tests/test_taint_endorse_laundering.py:39`).
2. Lean theorems for the rule: `origin_persists_or_is_declassified`, `no_authority_from_untrusted`, `untrusted_gains_no_authority`, `declassification_is_the_only_escape`, `declassifier_must_be_declared`, `untrusted_author_needs_granted_declassifier`, plus secret confinement (`formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:133`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:172`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:184`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:197`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:212`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:231`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:249`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:287`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:300`).
3. Anti-vacuity as a first-class obligation: `g9_not_vacuous`, `secret_rules_not_vacuous`, `authority_refusal_is_not_universal`, `sink_rules_are_distinct`, `secret_refusal_is_load_bearing`, `g9_context_hypotheses_are_inhabited` (same file, lines 361, 394, 427, 443, 467, 497).
4. A walk-coverage theorem over a component-body calculus, `coversBodyB_iff` and `walk_covers`, with refusal witnesses for a dropped scope, a shortened walk, a stripped `Secret` parameter and a wrongly seeded `Trusted` parameter, decided against the real checker by a differential row (`formal/RevL/Theorems/G9Coverage.lean:214`, `formal/RevL/Theorems/G9Coverage.lean:248`, `formal/RevL/Theorems/G9Coverage.lean:275`, `formal/RevL/Theorems/G9Coverage.lean:395-428`).
5. An exploit corpus and the repairs it forced: relay laundering through an unannotated service (interprocedural signatures), ambient `endorse` (scoped and granted), `endorse` helper laundering a foreign origin across a call (`_Signature.clears`, origin-aware), body-minted foreign origin under `endorse`, activation body not walked, `Secret[T]` receiver stripped (`docs/design/249-taint-provenance.md:156-221`; `tests/test_taint_endorse_laundering.py`; `tests/test_taint_activation_body.py`).
6. A model-authored-turn profile in which no declassifier of the turn's own making counts: `untrusted_author_needs_granted_declassifier`, with `admit_profile.check_no_declassify` refusing both doors on the root AST (`src/revl/admit_profile.py:630`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Jif, JFlow (Myers and Liskov decentralized labels) (citation to be checked) | Static IFC with per-principal labels and declassification by authority | Origins are derived from the declared capability scope of a crossing, never written by hand; declassification is a granted declaration slot with an audit token, not a runtime authority check |
| FlowCaml, LIO (Haskell) | Type-based IFC; LIO adds dynamic labels with a monad | Labels are a side table stripped before base typing, so a program using no qualifier is byte-identical on every backend (`docs/design/249-taint-provenance.md:102-115`) |
| Perl taint mode, other dynamic taint | Run-time bit, cleared by regex match | Static, refuses at compile time; the clearing operation is a named, granted, origin-precise form |
| Goguen-Meseguer noninterference | Semantic security property | The Lean development proves a flow-level rule over an abstract walk, not noninterference of the language; it says so (`formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:80-110`) |
| Object capabilities (E, Caja) | Authority is the reference; no ambient authority | Taint is the missing half: a legitimately held capability steered by untrusted data. G1/G4/G8 confinement handles reach, G9 handles steering |
| CaMeL (Debenedetti et al.), FIDES (citation to be checked) | Separate privileged and quarantined models; IFC over agent data and control flow | Checker lives in the language and admission gate, not in an interpreter wrapped around a model; the author of the code can itself be the untrusted party (model-authored-turn profile) |
| Spotlighting and prompt-level defenses (citation to be checked) | Mark or delimit untrusted text for the model | No reliance on model behavior; a refusal is deterministic |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Untrusted/Trusted lattice, G9 refusal at declared sinks | `src/revl/taint.py:1-40`; `docs/guarantees.md:77-90` | shipped | First slice of the staged plan in the design doc |
| Interprocedural propagation over signatures | `src/revl/taint.py:3070-3100`, `_Signature` at `src/revl/taint.py:1336` | shipped | `_infer_signatures` and `_Signature` exist; the "least fixed point" wording was not checked against the code; the design doc staged plan lists sub-steps B1-B4 |
| Scoped, granted `endorse[origin]`; audit tokens | `docs/design/249-taint-provenance.md:351-400`; `src/revl/audit_diff.py:309-316`; `tests/test_taint_endorsement.py` (19 tests) | shipped | |
| Derived sinks (`taint_strict`) | `src/revl/taint.py:642`, `src/revl/taint.py:845`; `tests/test_taint_slice_d.py` (26 tests) | shipped | |
| Runtime origin tag | `docs/design/249-taint-provenance.md:533-547` | designed | Nothing on main |
| `endorse` does not launder foreign origin across a call | `tests/test_taint_endorse_laundering.py:39`, `tests/test_taint_endorse_laundering.py:57`, `tests/test_taint_endorse_laundering.py:98`; `src/revl/taint.py:2080-2100`; `CHANGELOG.md:350-352` | shipped | Cross-call and service-boundary cases |
| Activation body and `Secret[T]` receiver body walked | `tests/test_taint_activation_body.py` (22 tests) | shipped | Header cites both holes |
| Origin persisted across host round trip; IR keeps qualifiers | `CHANGELOG.md:484-491`; `tests/test_taint_persistence_roundtrip_1983.py` (12), `tests/test_taint_ir_qualifiers_1937.py` (6) | shipped | |
| Agent successor inherits no operator authority | `CHANGELOG.md:484-491` | shipped | Cited by issue numbers in the changelog; the test file was not opened for this draft |
| Lean: no authority from untrusted, only escape, declared declassifier | `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:172`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:197`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:300` | shipped | No `sorry` in the three G9 files (only in prose comments). The model's `Declassifier` is the origin form only (lines 338-344) |
| Lean: anti-vacuity | same file, lines 361, 394, 427, 443, 467, 497 | shipped | Added after the review the file cites (lines 427, 443, 467 sit under the anti-tautology header at 414) |
| Lean: walk coverage | `formal/RevL/Theorems/G9Coverage.lean:214`, `formal/RevL/Theorems/G9Coverage.lean:248`, `formal/RevL/Theorems/G9Coverage.lean:275` | shipped | `formal/STATUS.md:56` marks it full for the rule and the coverage of the walk; that "full" is for the L0 component-body calculus, not a statement about Python |
| Differential rows against the real checker | `tests/test_formal_g9_taint_1811.py`; `tests/test_formal_g9coverage_2108.py` (20 tests) | shipped | The taint row checks the rule on the corpus, not coverage of the walk |
| Interprocedural fixed point proved | `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:84-90`; `formal/STATUS.md:56` | not done | Named as open in both |
| Path coverage of the interprocedural discovery | same | not done | A path the walk never learns of is outside the theorem |

## Numbers that exist
- Rejection fixtures for the taint differential row: 4 files (`tests/test_formal_g9_taint_1811.py:41-46`), all decided `agree-G9`.
- Coverage rows: "15 files over 21 components today" per `formal/STATUS.md:56`; this is the status table's count, not a re-run count.
- Test counts by file (functions matched by name pattern, not run): taint_provenance 24, endorsement 19, endorse_laundering 12, activation_body 22, slice_d 26, persistence 12, ir_qualifiers 6, g9coverage 20, g9 differential 8.
- Number of Lean theorems named G9 in the status table: "18 + 16" in the status row (`formal/STATUS.md:56`); the second column's meaning was not checked.
- No false-positive rate, no attack success rate, no overhead figure. None yet.

## Paper outline
1. Introduction: injection as flow to authority; residual risk after confinement.
2. Model: origins, lattice, sinks, the one declassifier; derived sinks.
3. The checker: per-body walk, signatures iterated to a fixed point, cross-component and state environments.
4. Proof: the flow model in Lean; the headline theorems; secret confinement.
5. Anti-vacuity: why extra theorems exist.
6. The walk gap: two bugs, why the rule proof could not see them, the body calculus and coverage theorem, the differential row and its ratchet.
7. Exploit corpus: relay, closure capture, spawn config, service return, endorse helper, foreign origin under endorse.
8. Evaluation (to be written).
9. Related work and limits.

## Threats to validity and limits
- The Lean model proves the rule over a given flow. The file says so: "Path coverage is NOT proved" (`formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:70-90`) and no Lean theorem is a statement about the Python checker (`formal/RevL/Theorems/G9Coverage.lean`, "Not a proof about Python"). That header predates `G9Coverage.lean`, which decides component-body walk coverage; the interprocedural discovery stays unproved (`formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:84-90`).
- `Admits` classifies a sink, but that the checker labels the right positions as sinks is "extraction, not theorem" (same file, scope section). A missed sink is invisible to the proof.
- The taint differential row takes the checker's own report as premise; it cannot tell "holds here" from "looked here and found nothing" (`G9Flow.lean`, header). Only the coverage row addresses that, and only over component bodies.
- The header of `G9_NoAuthorityFromUntrusted.lean` says no differential-oracle row references its definitions. That is stale: `G9Flow.lean` and `G9Coverage.lean` add such rows.
- The runtime tag is not built; the static origin is coarse (`web`, not `web:host`).
- Trusted authors must annotate. Derived sinks (`taint_strict`) lower the burden but by how much was not measured.
- A prior adversarial review found G4/G5/G6/G8 theorems to be tautologies over their inductives (cited in the G9 file as a 2026-09-02 review). That finding is about other guarantees; this paper does not lean on them.
- The wider claim "revl is the only language with this" is not made; no survey was done.
- The Lean model covers one of the checker's downgrade forms (`endorse[origin]`); the verified parser and the originless `endorse` are outside it.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 60 checked, 11 corrected.
