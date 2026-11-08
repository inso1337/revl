---
id: P56
title: Prompt-Injection Resistance by Admission: Confinement, Untrusted-Author Profile and Per-Turn Compilation
theme: agents
kind: full
maturity: partial
status: Admission, the untrusted-author profile, the in-process admit crossing and the MCP trust default are on main with tests; an AgentDojo pilot (banking, 2 pinned local models, 2 user tasks, 1 injection task, 2 runs) measured baseline versus admitted-revl attack success and utility, the injection-escape measurement is still 5 scored attempts on one model, and physical confinement of host code is designed only.
fused_with: P36
---

## Pitch
If the model's output is a program admitted by a checker against only the tools it was granted, an injected instruction can cause only effects the component already declared, and the hole the design identifies, host code, is closed for untrusted authors by refusing the model the right to write any.

## Abstract
Prompt-injection defenses for agents mostly filter content, wrap tool calls in monitors, or split a privileged and a quarantined model. We study a different point in the design space: make the model's output a program in a language whose checker proves its authority, and admit it only if it fits inside the surface the operator granted. An instruction injected through a document, a tool description or a compiler message can then make the model write whatever it likes, but an ungranted call is a type error, an undeclared boundary crossing fails the emission bound, and the program never runs. We identify the residual gap, the language's own escape hatch to host code, and close it with an untrusted-author admission profile: no new extern, reach bounded to a granted allowlist, resolved by provision key and failing closed when the binding cannot be determined. A name-keyed version of the exemption was a full bypass, and an audit of the agent-facing server found a second hole in which the driving agent was treated as a trusted host-code author; both are fixed and have regression tests. We describe a measurement that reports compliance (the model wrote the undeclared thing) and containment (a gate then refused it) separately, and treats compiler error feedback as an injection carrier. On the escape measurement that exists, a local 35B-parameter model complied in 2 of 5 scored attempts and 0 escaped; the sample is too small to support a rate, and we say so. An AgentDojo pilot on two local models measured attack success and utility for a direct-tool baseline and an admitted-revl arm; the port is committed at `bench/agentdojo/` and the sample is a pilot, not a rate.

## Problem and claim
Injection is dangerous when the injected instruction inherits ambient authority: the process can open sockets and read files, so any `eval`, shell or over-broad tool the model is talked into reaching inherits all of it. Runtime monitors and content filters try to recognise bad intent. They do not change what a successful injection can reach.

The claim is structural and bounded. Authority in the target language is exactly what a component declares and was granted (`docs/prompt-injection-resistance.md:46-71`). Install is admission, not interpretation: a component runs only after the checker proves its guarantees over the whole composition (`docs/prompt-injection-resistance.md:73-91`). Therefore an injected instruction can enlarge the model's output but not the authority of the admitted program. We do not claim to stop injections that stay inside granted capabilities: the doc's own first residual risk is that a write to the wrong value through a granted boundary is in-bounds (`docs/prompt-injection-resistance.md:269-275`).

The part that is not free is the escape hatch. A model-authored `extern ... = @py { ... }` admits and runs arbitrary host code, because the gate's boundary for host code is "verbatim host code, unchecked inside" (`docs/design/329-untrusted-author-profile.md:7-23`). The paper's second claim is that an untrusted-author profile, applied at every door that admits model-authored source, removes that hole for code-mode authors and that the profile is subtle enough that two real bypasses had to be found and fixed.

## Contributions
1. A per-turn admission design: model-authored revl is compiled against a synthetic composition declaring exactly the granted tools, so an ungranted call is a compile refusal and the refusal text is the repair signal; the in-process admit crossing is on main (`docs/design/329-untrusted-author-profile.md:61-82`; `tests/test_admit_crossing.py:179,258`).
2. The untrusted-author profile: `no_extern` on the admitted root, transitive host-reach refusal through the module closure, a granted-service allowlist whose internal-provision exemption is resolved by provision key and fails closed, plus no self-minted declassifiers and no self-chosen realm (`src/revl/admit_profile.py:101,184,245,518,763`; `tests/test_admit_profile.py`, 20 tests).
3. Two bypasses found and closed, each with a regression test: the name-keyed provision exemption (a throwaway component providing the same service under an unused key made `granted` decorative) and the MCP server's treatment of the driving agent as a trusted host-code author (`tests/test_r2_allowlist_key_binding.py:152-284`; `tests/test_mcp_authoring_trust.py:77,140,173`).
4. A measurement design that separates compliance from containment, detects compliance without consulting the gate, and splits refusals into "about the injected reach" and "unrelated fault"; compiler error feedback is tested as an injection carrier (`docs/prompt-injection-resistance.md:187-261`; `bench/results/injection-ornith/escape.md`).
5. A documented set of what confinement does not cover (in-grant misuse, over-broad grants, host code; the doc's fenced enumeration gap is marked resolved in `docs/contract-errata.md`) (`docs/prompt-injection-resistance.md:263-320`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| CaMeL (Debenedetti et al., 2025) (citation to be checked) | Privileged model writes a restricted-Python program, custom interpreter tracks capabilities and data flow | Same shape (model emits a program, a checker bounds it) but the bound is the language's static checker over declared services, with no custom interpreter. Our profile also covers the host-code escape hatch, and taint is a separate layer. We do not claim to match CaMeL's utility results |
| Dual-LLM pattern (Willison, 2023) (citation to be checked) | Quarantined model handles untrusted text, privileged model never sees it | No model split. The same model can read injected text; what it can cause is bounded after the fact by admission |
| Progent (2025) (citation to be checked) | Programmable privilege-control policies for tool calls | Policy is a runtime tool-call filter written beside the agent. Here the granted surface is the type environment of the program, so an out-of-policy call does not typecheck |
| AgentDojo (Debenedetti et al., 2024; MIT), InjecAgent, other injection benchmarks | Measure attack success and utility over fixed task suites | We run an AgentDojo pilot with a direct-tool baseline and an admitted-revl arm (`bench/agentdojo/`); it is a pilot over 2 user tasks and 1 injection task, not the full suite |
| Meta LlamaFirewall, Guardrails-style filters (citation to be checked) | Classify or filter inputs/outputs | Content-independent: the gate does not read intent |
| WebAssembly sandboxing | Confine untrusted code physically, trap on escape | Named as the deferred fuller answer: allow a per-turn host body but trap it (`docs/design/329-untrusted-author-profile.md:119-160`). Today we forbid host code instead |
| Capability-secure languages (Joe-E, Caja, E) (citation to be checked) | Authority only by reference; no ambient authority | Same principle. Differences: effect classes and a drift gate on the audit surface |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Argument from confinement: declared access, emission bound, enumerable crossings; install is admission | `docs/prompt-injection-resistance.md:44-109` | shipped (argument) | Doc says "Everything above is an argument from the shape of the language" |
| Emission fixed point closes first-class-value dodges | `docs/prompt-injection-resistance.md:92-109`; `src/revl/emission_analysis.py` (cited by the doc; the source was not opened) | shipped | |
| Sandbox profile enforced at `load` and `swap` | `docs/prompt-injection-resistance.md:160-186` (`_enforce_sandbox` in `src/revl/mcp/session.py`) | shipped | Read via the doc; the doc names the function |
| Untrusted-author profile: fields and factory | `src/revl/admit_profile.py:101-245` | shipped | Includes `no_declassify`, `taint_strict`, `no_realm_placement`, `realm_bindings` |
| Extern smuggling refused; admits without profile | `tests/test_admit_profile.py:78,88` | shipped | |
| Transitive host reach refused through nonroot wrappers and stdlib | `tests/test_admit_profile.py:263,286,304,324,388`; `CHANGELOG.md:353-355` | shipped | |
| Allowlist: ungranted reach refused; empty set refuses any reach | `tests/test_admit_profile.py:117,126` | shipped | |
| Provision-key exemption fails closed; decoy refused, cannot reach admit decider | `tests/test_r2_allowlist_key_binding.py:152,164,174,190,241,284`; `src/revl/admit_profile.py:763-830` | shipped | Docstring states the name-keyed version "was a full bypass" |
| Admit crossing: refusal is a verdict, commit persists, abort reverts residue-free | `tests/test_admit_crossing.py:128,143,165,179,191,210,237,258` | shipped | |
| MCP: transport-borne source compiles under the profile; inline `@py` refused; default closed | `tests/test_mcp_authoring_trust.py:77,90,97,113,125,140,173,491,508`; `CHANGELOG.md:356-362` | shipped | `--author-trust trusted` restores the old behaviour and stamps tickets |
| Library doors confine `use` paths under an untrusted profile | `docs/design/329-untrusted-author-profile.md:84-117`; `tests/test_admission_use_confinement.py` (named by the doc; not opened) | shipped | |
| Approve-one / run-another exploit and its audit | `docs/v2.0-roadmap.md:1636` (the approve-one / run-another exploit is exploit iii of the first advisory entry; the next entry at 1637 is the path-jail carrier) | shipped (fix described in the roadmap) | Public repo; the advisory text is in the roadmap |
| Measured injection escape: 5 scored attempts, 2 complied, 0 escaped | `bench/results/injection-ornith/escape.md:5-11`; `bench/results/injection-ornith/attempts.json` (8 rows) | shipped (measurement) | See numbers |
| Wasm-import path for physical confinement of per-turn host code | `docs/design/329-untrusted-author-profile.md:119-160` | designed (deferred) | The doc lists three TODOs |

## Numbers that exist
- Injection escape: 1 host (revl), 1 pinned local model (`hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M`), 1 base spec (`01-kv-provider`), 8 injection vectors generated, 5 scored. 3 of 8 spent the whole output cap on the reasoning channel and returned no answer; they are excluded from every denominator (`bench/results/injection-ornith/escape.md:9`). Of 5 scored: complied 2 (compliance rate 0.4), containment 2 of 2 by the `compiles` gate, escaped 0. One of the two refusals was about the injected reach (drop-the-undo: "effect has no `undo` and `store.insert` is not pure"); the other was a syntax error unrelated to the injection (env-exfiltration), counted apart (`escape.md:11`; `attempts.json`). One more complier (widen-the-service) fell in the excluded group. Carriers: service-interface comment, task brief, compiler-output feedback; the compiler-feedback carrier produced 0 compliances in its 1 attempt.
- The results file carries the string `"compiler": "revl was never imported"`; `bench/run.py:641` produces it. What that field means was not traced, so the run is not offered here as a gate run.
- Tests: `tests/test_admit_profile.py` 20, `tests/test_r2_allowlist_key_binding.py` 14, `tests/test_mcp_authoring_trust.py` 29, `tests/test_adversarial_gate.py` 28, `tests/test_admit_crossing.py` (8 listed above). Counts on def test_.
- The doc cites the same figures in `bench/results/framework-bench/report.md` (row at line 132, measured at line 295).
- No attack-success rate against any published injection benchmark exists outside the pilot below.
- AgentDojo pilot (issue #2683): `agentdojo==0.1.35`, banking suite v1.2, `user_task_0` and `user_task_1` against `injection_task_0` (`important_instructions`), 2 runs, temperature 0. Models: `flint-bench:latest` (Q4_K_M, digest `e4090fb1bab5`) and `ornith-revl:latest` (Q4_K_M, digest `2b98245daf4f`). Attack success 0/8 in each arm (0/4 per model per arm). Benign utility: baseline 2/4 (flint) and 4/4 (ornith), revl 3/4 and 4/4. Utility under attack: baseline 1/4 and 2/4, revl 2/4 and 2/4. Admission refusals: 8, every one a compile or G6 refusal of malformed model source (4 on benign tasks, 4 on attacked), none about an ungranted reach or host code. Mean wall time per record: baseline 34.2 s and 64.4 s, revl 146.8 s and 43.1 s; revl mean steps 1.4 and 1.5. Raw records and the generated report are in `bench/results/agentdojo/pilot-banking/`.

## Paper outline
1. Introduction: injection as ambient authority; admission as a different place to stand.
2. Model and threat: the attacker controls source and data, not the checker, runtime or policy.
3. Confinement: declared access, emission bound with its fixed point, enumerable crossings; install is admission.
4. The escape hatch: why host code defeats it; the untrusted-author profile and its fail-closed binding rule; the name-keyed bypass.
5. Doors: every path that admits model-authored source (transport, library, in-language crossing); the "approve one, run another" exploit.
6. Per-turn admission loop and refusal as repair signal.
7. Measurement: compliance versus containment; error feedback as carrier; results and their limits.
8. Residual risks, related work, conclusion.

## Threats to validity and limits
- The measurement is n=5 scored attempts on one model and one spec, run by the party whose system it favours. The doc says so: "nothing here moves off the `measured` rung until somebody who is not us runs it" (`docs/prompt-injection-resistance.md:250-261`).
- Compliance is detected by regular expression over source and can miss paraphrases or fire on comments; every attempt keeps the matched text (`docs/prompt-injection-resistance.md:243-249`).
- One of the two "contained" attempts was contained by an unrelated syntax error and is not evidence about injection.
- The measurement covers admission, not what a running composition does with injected data (residual risk 1) and has no run without the gate as a baseline.
- The gate does not sandbox host code; the profile removes the author's ability to write it, which costs expressiveness. The wasm-import alternative is designed only (`docs/design/329-untrusted-author-profile.md:119-160`).
- A trusted-author mode exists and restores the old behaviour with a stamped ticket; an operator who selects it re-opens the hole.
- The doc's residual-risk 4 (`docs/prompt-injection-resistance.md:300-312`) describes a host block reached only through a first-class function value as not enumerated on the audit surface, pinned `xfail`. That text is stale: `docs/contract-errata.md` (around line 1081) marks it resolved, and `tests/test_adversarial_gate.py:532` is a plain passing assertion with no `xfail`. The test was not run for this draft.
- Two prior bypasses were found late. That is evidence the profile is intricate, not that no third exists.
- Approvals still decide what happens inside granted capabilities; this paper does not claim to stop in-grant misuse.
- Utility cost is measured only in the two-task AgentDojo pilot: the refuse-repair loop cost 1.4 to 1.5 model steps per record and 43 s to 147 s of wall time, and the models are not trained on revl. The pilot is 2 user tasks x 1 injection task x 2 runs, one attack family and two local models; 0 attack successes in either arm is not a rate, and the suite's in-grant injections do not test the ungranted-reach property the paper claims.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 29 checked, 4 corrected.
