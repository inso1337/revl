---
id: P44
title: A Fail-Closed Admission Gate You Can Embed: Three-Valued Verdicts, a Generated Rust Crate and an Import-Free WASI Component
theme: security
kind: tool-demo
maturity: partial
status: The refuse-only gate, the drift gate, the import-free component and its conformance vector are on main with tests; the latency headline belongs to the Python gate, not to the Rust or wasm forms, nothing is published to a registry, and agreement and cost studies at scale are still needed.
fused_with:
---

## Pitch
An admission gate whose non-refusing answer is deliberately not "admitted", generated from a self-hosted spec, drift-gated byte for byte, and packaged as a WASI component with an empty import section so the verdict is a deterministic function of its arguments (determinism only; termination and memory rest on the resource bounds).

## Abstract
Systems that let an agent author code need a check they can call in their own process. Common embeddable policy engines return allow or deny, and a host that reads "no deny" as "allow" fails open. We describe revl's embeddable admission gate, built around the opposite default. The verdict type of the Rust crate and the wasm component has three arms, Refused, NoObjection and OutsideFrontier, and no admitting arm: their wire form reports `admitted:false` on every arm (the Python `revl.gate.Verdict` is a separate surface that does admit), so a consumer written against a two-valued shape reads the gate as "never admits" instead of misreading silence as approval. The Rust and wasm gate is a refuse-only layer that, on its covered corpus (the composition and guarantee layer, plus one slice of the type layer in Rust), never admits what the reference refuses; in one measured 12-candidate batch it returns no-objection for 3 of the 8 candidates the reference refuses, which is the tolerated direction. A separate type, reachable only after NoObjection and only inside a deliberately small admission surface, can issue an admission. The Rust crate is generated from the self-host compiler sources and committed, and a test regenerates it and fails on any byte difference (`tests/test_gate_crate_drift.py:47`; CI wiring not checked). The same crate is packaged as a WASI-P2 component whose import section is empty, so no clock, file system or random source can change a verdict, and a test checks the built binary rather than the interface. We state the gate's threat model (an agent author whose code disagrees with its advertised effects) and its non-goals. We also report what it costs: the Python facade admits a representative candidate in a median 0.165 ms, whereas the Rust form measures 7.1 ms on a 276-byte candidate and grows super-linearly, which we attribute to the emitted front end. We report no wasm latency.

## Problem and claim
An embedder (an MCP server, a CI step, an edge worker) wants a local, in-process verdict on agent-generated code. Two failure modes recur. A gate that is permissive on its blind spots admits what the full compiler would refuse. A gate that is correct but reachable only over a subprocess or network hop is too slow or too far from the host to sit inside an agent loop.

The claim has three parts. (1) Verdict design: with no admitting arm on the verdict surface, "this gate found nothing it can refuse" cannot be confused with "admitted", and the unsafe divergence direction (admitting what the reference refuses) is closed on that surface by having no admitting arm; on the separate admission surface it is checked by a test on the covered corpus, not proved. (2) Provenance: a gate generated from the self-host spec and committed with a regenerate-and-compare gate cannot drift silently from its source. (3) Determinism, not termination: a component with an empty import list is a deterministic function of its arguments, and that is checkable from the artifact.

The claim is not that the gate is complete, fast in every form, or sufficient for safety. The dependency contract says the gate "never confines its host" and that admission is a compile-time judgment scoped to the frontier.

## Contributions
1. A three-valued verdict (Refused, NoObjection, OutsideFrontier) with no admitting arm and `admitted:false` on every wire arm, plus a separate `Admission` type reachable only after NoObjection and inside a stated admission surface.
2. A two-layer API: layer 1, pure verdict functions that reproduce the reference verdict verbatim on the Python tier; layer 2, a stateful `Gate` session facade that fails closed.
3. A generated Rust crate with a derived (not hand-listed) frontier table, resource bounds that turn stack-exhaustion inputs into refusals, and a byte-drift CI gate.
4. A WASI-P2 component built for `wasm32-unknown-unknown` so that its import list is empty, with a test that reads the built binary's import section.
5. A four-clause dependency contract and a gate threat model with non-goals and an offensive test suite.
6. A measured cost profile for the Python and Rust forms, including the negative result for Rust.

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| WebAssembly component model, WASI | Capability-style imports; a component's imports define what it can reach | The gate is built to have zero imports, and a test fails if one appears; the argument is about the verdict's determinism, not about sandboxing a guest |
| seL4 and reference-monitor work | A small trusted mediator on every access | revl's gate mediates admission of code at compile time, not access at run time, and says so ("the gate does not confine you") |
| eBPF verifier | Kernel-side static check before load; refuses unsafe programs | Similar refuse-before-load stance; revl adds an explicit "no objection is not admission" arm and an outside-frontier arm for inputs it declines to decide |
| Cedar, OPA/Rego (including Rego-to-Wasm) | Embeddable authorization evaluators returning allow/deny | Those evaluate request policy; the revl gate checks agent-authored components and has no allow arm on its verdict surface |
| Java Security Manager | In-process sandbox for untrusted classes | revl does not sandbox host code; extern bodies are surfaced on a review surface, not confined (`docs/threat-model.md:113-125`) |

Citations to be checked: eBPF verifier documentation and Gershuni et al. PLDI 2019 (citation to be checked); Cedar (Cutler et al., OOPSLA 2024) (citation to be checked); Haas et al. PLDI 2017 for WebAssembly (citation to be checked); Klein et al. SOSP 2009 for seL4 (citation to be checked).

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Verdict has no admitting arm; `admitted:false` on every arm | `crates/revl-gate/README.md:55-85`; `tests/test_gate_crate_drift.py::test_the_verdict_surface_still_issues_no_admission`, `::test_the_crate_is_admit_only_and_says_so` | shipped | |
| Admission is a separate type behind NoObjection and an admission surface | `crates/revl-gate/README.md:87-157`; `tests/test_gate_crate_drift.py::test_the_admission_arm_is_reachable_only_behind_the_refusal_gate` | shipped | The surface is "deliberately tiny": interface declarations and components with parameter reads and calls on a required service, over scalar types |
| Rust form admits into a running composition (`admit_into`) and every Rust admission is a Python admission on the covered corpus (one direction only) | `crates/revl-gate/README.md:193-228`; `bench/results/inprocess-gate-rust.md` batch table; `tests/test_inprocess_gate_rust.py::test_every_rust_admission_is_a_py_admission` | shipped | The rust gate refuses 5 of the 8 candidates the Python gate refuses; the other 3 are no-objections (bench file, "What the screen catches, measured") |
| Generated from self-host sources, drift-gated | `crates/revl-gate/README.md:6-12`; `tests/test_gate_crate_drift.py::test_the_committed_crate_matches_a_fresh_generation`, `::test_generation_is_deterministic`, `::test_the_frontier_table_is_derived_not_hand_written` | shipped | Test needs no cargo (pure regenerate and compare) |
| Fail-closed at the frontier (size, per-level item count, panic, unknown wire shape, manifest row bound) | `crates/revl-gate/README.md:158-181` | shipped | Host must build with `panic = "unwind"` (`crates/revl-gate/README.md:295-302`) |
| WASI-P2 component with empty import list | `crates/revl-gate-wasm/README.md:76-91`; `tests/test_gate_wasm_vector.py::test_the_built_component_imports_nothing` | shipped | Target is `wasm32-unknown-unknown`; wasip2 would link WASI imports. The `.wasm` is not committed (toolchain-dependent bytes) |
| wasm form agrees with reference on corpus, issues no admission, no corpus program traps | `tests/test_gate_wasm_vector.py::test_wasm_gate_and_reference_agree_on_the_covered_corpus`, `::test_the_wasm_gate_issues_no_admission_for_anything_in_the_corpus`, `::test_no_corpus_program_traps_the_instance`, `::test_no_false_alarm_on_the_covered_corpus` | shipped | Needs cargo, wasm-tools and wasmtime; not run for this draft. Corpus size not stated here |
| wasm trap is not a verdict | `crates/revl-gate-wasm/README.md:93-104` | shipped (documented) | Panic strategy is abort on wasm, so the `catch_unwind` path is absent; host must fail closed on a trap |
| Component is edge-sized | `tests/test_gate_wasm_vector.py::test_the_component_is_small_enough_to_be_an_edge_artifact` | partial | Bound is `< 8 MiB`, "a regression tripwire on the order of magnitude, not a byte budget"; actual size not stated |
| Python facade: layer-1 verdicts and layer-2 `Gate` | `src/revl/gate.py:120` (`Verdict`), `src/revl/gate.py:258` (`admit`), `src/revl/gate.py:287` (`admit_into`), `src/revl/gate.py:780` (`Gate`); `tests/test_gate_verdict_parity.py` | shipped | `admit` delegates to the reference compile plus `refuse_admission` so it cannot admit what `revl` refuses |
| Four-clause dependency contract | `docs/gate-dependency-contract.md:26-116` | shipped | Clause 3: runtime half is a separate, py-only adoption |
| Threat model, non-goals, offensive suite | `docs/threat-model.md:17-35`, `docs/threat-model.md:113-173`; `tests/test_adversarial_gate.py` (28 test functions) | shipped | `docs/threat-model.md:159-171` still lists a G8 first-class-reach gap as a pinned `xfail`; the suite has no `xfail` marker and its test says the gap is resolved (`tests/test_adversarial_gate.py`, around lines 531-548). The doc is stale |
| Publication to registries | `docs/gate-dependency-contract.md:466-520` | not done | "Nothing is on npm", not on crates.io, PyPI name is taken by an unrelated project |

## Numbers that exist
- Python in-process admission, `compile_source(candidate, manifest=running)`: median 0.165 ms, p90 0.179 ms, p99 0.659 ms, n = 4000; compile-only baseline median 0.149 ms; gate share about 0.015 ms. Apple M1 Max, Python 3.14.3. Source: `bench/results/admission-latency.md` (Result and Methodology). This is the Python facade, and the scenario is one candidate of two declarations (a `service` and a `component`) against a running composition of two components (`bench/admission_latency.py`).
- Python size sweep: medians 0.409 ms (small) to 3.435 ms (large, 48 methods), n = 400 per cell; the representative scenario re-measured at 0.276 ms. Source: `bench/results/inprocess-gate.md` (cost table and headline).
- Rust form, one `revl_gate::admit` call: median 7.239 ms (218 B), 79.898 ms (636 B), 935.718 ms (2364 B), n = 25 per cell; representative 276 B scenario median 7.125 ms, p99 42.275 ms. The file says "super-linear", that cost "tracks TOKENS", and "Do not quote a single figure from this table as 'the rust gate's latency'". Source: `bench/results/inprocess-gate-rust.md`, "Cost" section.
- Rust agreement: of 8 candidates the Python gate refuses, the Rust gate refuses 5 (same code and sentence); the other 3 return NoObjection. Source: same file, "What the screen catches, measured". Batch size 12.
- wasm form: no latency number and no component size number exist yet.
- Test counts (functions, not expanded): `tests/test_gate_crate_drift.py` 19, `tests/test_gate_wasm_vector.py` 13, `tests/test_adversarial_gate.py` 28, `tests/test_gate_verdict_parity.py` 12.

## Paper outline
1. Motivation: embedding a check in an agent loop; the fail-open reading of "no deny".
2. Design: the three-arm verdict, the separate admission type, the dependency contract.
3. Generation and drift: self-host spec to Rust crate to component; the regenerate-and-compare gate.
4. The empty import section: what it proves and what it does not (traps, aborts, host obligations).
5. Evaluation: agreement, cost per form, size, adversarial suite.
6. Limits and related work.

## Threats to validity and limits
- The non-refusing arm is explicitly not an admission. A host that must admit still needs a reference verdict, except inside the small admission surface on the Rust tier (`crates/revl-gate/README.md:99-127`). The wasm tier has no admitting arm at all (`docs/gate-dependency-contract.md:17-24`).
- The wasm gate does not run the reference type layer (`crates/revl-gate-wasm/README.md:14-19`). The Rust crate runs one slice of it, the fn-body statement layer (`crates/revl-gate/README.md:60-64`), and its README gives a concrete example the reference refuses and the gate does not.
- Three of eight reference-refused candidates in the Rust batch pass as NoObjection. The design tolerates this direction; a reader will still count it as low recall.
- The Rust form's cost is milliseconds and super-linear, measured with `--iters 25` on a shared machine, and the bench file warns the medians "move by a FACTOR of a few between runs". The cost is attributed to the emitted lexer and parser (a declaration-heavy 1212 B input at 225 ms against a comment-padded 1248 B input at 8.7 ms, n = 6); a profile of the emitted front end has not been done, and the measured Rust form does not meet the sub-millisecond edge goal stated in `docs/design/335-wasm-edge-gate.md`.
- The empty-import property makes the verdict deterministic, not sound. It gives no termination or memory bound; the resource bounds cover those. Soundness rests on agreement with the reference, measured on a fixed corpus and a 12-candidate batch, not at scale.
- A wasm trap is not a verdict; correctness of the failure path is a host obligation.
- The gate "defends the declaration, not the runtime" and does not sandbox host code (`docs/threat-model.md:113-135`).
- The offensive suite targets the reference compiler; it has not been run against the Rust and wasm forms.
- No measured baseline against Cedar or OPA-Wasm exists.
- Nothing is published; no external user has depended on it. The `.wasm` is not committed, so reproducibility of the binary is by source pin and conformance vector only.
- The corpus and the offensive suite were written by the project itself.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 28 checked, 9 corrected.
