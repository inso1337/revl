---
id: P37
title: "Secrets as a Flow Property: Capability-Bound Secrets With Static Confinement and a Cross-Backend Value Registry"
theme: security
kind: full
maturity: partial
status: The static rule, a Lean proof over an abstract model and the runtime registry on five backends (a refusal on the sixth) are shipped with tests; the trace funnel covers three tiers, the value channel none, and no evaluation exists.
fused_with:
---

## Pitch
A provider key can be confined, by a flow rule with no declassifier whose core statements are proved in Lean over an abstract model, to the emissions of one capability, and the honest cost of that claim, including the type guarantee the design note had to retract, is itself the result.

## Abstract
Agent systems hand a provider key to exactly one call and then rely on discipline to keep it out of logs, prompts, tool returns and traces. We describe the secret machinery in revl, a typed language for capability-scoped agent programs. A top-level `secret NAME for CAPABILITY` declaration binds a value, supplied by the driver at run time and never present in the source or the manifest, into the extern bodies of one capability. The design note for the feature describes an adversarial review that retracted its first claim: the bound key lives in opaque host code, so no revl type can forbid its use there, and the guarantee is a flow guarantee over two disjoint origins, `secret` (the bound key, no declassifier, refused at every crossing kind) and `confidential` (the `Secret[T]` value qualifier, admitted only at a declared receiver and downgraded only at a declared `endorse`). We state three Lean theorems for the rule (`secret_persists`, `secret_confined`, `confidential_needs_declassification`), a semantic non-interference statement over a small value model that is not a consequence of the rule (`secret_noninterference`, issue #2738), and the explicit gap between that model and the code. For values that escape the static rule, five backends carry a registry that scrubs registered values and their escaped renderings, and the wasm tier refuses at its one framing site. The trace funnel covers python, typescript and go; the roadmap states that java, rust and wasm are out of scope for it, and that the provider's return value channel has no funnel on any tier. We report the defect series that building the registry across backends exposed. We have no leak-rate measurement yet; we list the experiment that would supply one.

## Problem and claim
Existing secret handling falls in two camps. Secret managers and injected-credential agents control where a value is stored and delivered, and say nothing about where the process then sends it. Wrapper types (a `Secret<T>` newtype, Java's `SecretKey`) forbid reading in the host language but do not cross a language boundary or a serialization boundary, and an agent system crosses both constantly: into a model prompt, an MCP tool return, a write-ahead log, a console trace.

The claim is narrow and the design note insists on narrowness. No revl construct and no declared boundary crossing can carry a capability-bound key out of its capability's extern bodies (`docs/design/256-capability-bound-secrets.md:76-107`). It does not claim the process cannot send the key: a host body that splices the key into its own outbound request is outside revl's reasoning (the note's attack A3, status "CRITICAL, OPEN", line 796). The paper's contribution is the precise boundary of the guarantee, the disjointness argument that makes it sound, and the runtime registry that covers what the static rule cannot see.

A second claim is methodological. The first draft of the note asserted a no-eliminator `Secret` type for the bound key. Review showed this was vacuous because the checker never descends into host-body text (lines 38-54 and 215-243 of the note). The revision removed the mechanism and rebuilt the claim on flow alone. We present the retraction as a result: it is a concrete case of a type-level secret guarantee that does not exist when the secret is held in an untyped host scope.

## Contributions
1. A declaration surface (`secret NAME for CAPABILITY`, a contextual keyword) with plug-time injection into one capability's extern bodies, fail-loud when the value is absent, name-only in the audit table, with a documented rotation seam (`docs/design/256-capability-bound-secrets.md:159-215`, `docs/design/256-capability-bound-secrets.md:281-365`).
2. Two disjoint origins, `secret` and `confidential`, so that a permissive receiver rule for `Secret[T]` is unreachable by the bound key. The note identifies the earlier single-token design as a critical flaw and gives the exploit (`docs/design/256-capability-bound-secrets.md:23-36`, attack A8 at line 878).
3. A per-crossing-kind refusal list (emit, plain extern call, unnameable callable, provide-method return, nested value) with a reach-completeness row in the test suite that fails if a crossing kind is added without the raise (`docs/design/256-capability-bound-secrets.md:414-497`, `tests/test_reach_completeness.py:265-285`).
4. Three Lean statements for the rule, with a non-vacuity witness showing the label algebra can clear a `secret`, so the theorem holds because of two explicit refusals (`formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:55-64`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:212-258`), and a stated gap between the Lean model and the runtime registry (`formal/STATUS.md:57`). Since issue #2738 the development also proves a semantic noninterference theorem over a small value semantics (`secret_noninterference`): two executions of the same declassifier-free step list that differ only in the secret input end with the same public projection, and `public_changes_require_a_declassifier` shows the exception is exact. The property belongs to the value semantics, not to the flow rule. That is noninterference of the model, not of the language.
5. A cross-backend value registry: leaf registration at the door a declared secret enters through, scrub of the escaped renderings (raw, JSON-escaped, repr-escaped), and a funnel at the host trace choke point (python, typescript, go); plus a catalogue of the defects found while porting it (roadmap item 421, F6 series).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Jif, FlowCaml, other information-flow type systems (citation to be checked) | Confidentiality labels on values, checked in the host language | Labels are on a value graph over declared boundary crossings; the bound key has no typed binding at all, and the note says so. Disjoint origins are the soundness device, not a lattice join |
| TaintDroid and dynamic taint tracking | Runtime propagation of labels | The runtime part here is not propagation: it is exact-value scrubbing of registered values at a funnel, bounded by minimum length, with no pattern matching |
| HashiCorp Vault, KMS, cloud secret injection | Controls storage, delivery, rotation | Out of scope for revl; revl consumes the injected value and constrains its onward flow. The injection seam only reads from the driver's map |
| Rust `secrecy`, Java `SecretKey`, wrapper newtypes | Non-printing and zeroizing wrappers in one language | Same intent per backend, but the registry here is a cross-backend contract with a shared test shape, and the static rule is independent of any one backend |
| WebAssembly component-model capability handles | Opaque handle, not the value, crosses the boundary | revl's wasm tier takes the opposite route and refuses at the framing site; see evidence table |
| Biscuit and macaroon tokens | Attenuable bearer credentials | Orthogonal: revl does not attenuate the key, it confines where its value may go |

The novelty is in the combination and in the honest accounting, not in a new label calculus. The Lean development now carries both the rule-level statements over the abstract walk and a semantic noninterference theorem over a small value semantics on the same walk (`secret_noninterference`); that is noninterference of the model, not of the language, so it should not be oversold.

## Evidence on main
| Claim | path:line or test | status | note |
|---|---|---|---|
| Design note states the revised, narrower guarantee and retracts the no-eliminator type | `docs/design/256-capability-bound-secrets.md:14-107` | shipped (doc) | The note says its first lines are the reasoning, "not as the current status" (lines 4-9) |
| Declaration, injection, fail-loud helper (py) | `backends/python/emit.py:4678-4687`; `src/revl/run.py:499-520`, `src/revl/run.py:1150-1167`, `src/revl/run.py:1663-1667` | shipped | `secrets_may_be_absent` and `_Driver.set_secret` rotation seam described at `docs/design/256-capability-bound-secrets.md:325-365` |
| Same helper on ts | `backends/typescript/emit.py:5111-5131` | shipped | |
| Same helper on java and rust | none: no `_REVL_SECRETS` hit in `backends/java` or `backends/rust`. The hits at `backends/java/emit.py:4045` and `backends/rust/emit.py:9312` are a `secret_return` wrapper branch, a different mechanism | not shipped as the plug-time helper | go and wasm: no `_REVL_SECRETS` hit found (checked) |
| `secret` raise with code `G-SECRET`, `confidential` with `G-SECRET-FLOW` | `src/revl/taint.py:1925-1975`, `src/revl/taint.py:2114-2126` | shipped | `SECRET_ORIGIN` at `src/revl/taint.py:1271`, `CONFIDENTIAL_ORIGIN` at `src/revl/taint.py:1280` |
| Five crossing kinds covered in a reach-completeness row | `tests/test_reach_completeness.py:265-285` | shipped | `seam` kind is exempt, with a stated reason |
| Bound-key suite pins that a host body handing the key to its own call compiles and runs | `tests/test_secret_bound.py:1-22` | shipped | 30 test functions by grep, not executed |
| `Secret[T]` flow rules, refusal example | `tests/test_secret_flow.py` (31); `examples/rejections/gsecret_service_return_discloses.rvl` | shipped | |
| Config field declared `Secret[T]` mints `confidential` at the checker | `src/revl/taint.py:502`, `src/revl/taint.py:1074`; roadmap item 421 F6(b) | shipped | |
| Lean: `secret_persists`, `secret_confined`, `confidential_needs_declassification` | `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:212`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:231`, `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:249`; `formal/STATUS.md:199-201` | proved | `formal/STATUS.md:57` marks the group "partial": the rule is proved, coverage of the checker's walk is not |
| Lean: semantic non-interference and the endorse invariant (issue #2738) | `RevL.G9.secret_noninterference`, `RevL.G9.public_changes_require_a_declassifier`, `RevL.G9.endorse_gate_preserves_foreign_origins` | proved in the model | the two-run statement over `RevL.Lemmas.TaintLemmas`'s `Val`/`stepVal`/`exec`, a property of the value semantics rather than the flow rule; the exception is a theorem, and the witnesses are registered non-vacuity rows. Not a theorem about the runtime registry or the Python checker |
| Non-vacuity witnesses | `formal/scripts/nonvacuity.tsv:111-113` | shipped | |
| py registry, renderings, text funnel | `backends/python/confidential.py:205`, `backends/python/confidential.py:534`, `backends/python/confidential.py:636` | shipped | |
| ts registry | `backends/typescript/runtime.ts:190-256`, `backends/typescript/runtime.ts:345` | shipped | |
| go registry | `backends/go/emit.py:5205`, `backends/go/emit.py:5471-5506` | shipped | tests `backends/go/test_secret_registry_go.py` (19) |
| java registry | `backends/java/emit.py:151`, `backends/java/emit.py:314-356` | shipped | `backends/java/test_secret_registry.py` (32) |
| rust registry | `backends/rust/placement_runner/src/confidential.rs:48-177` | shipped | `backends/rust/test_rust_secret_registry.py` (27) |
| wasm: no registry; refusal at the framing site | `backends/wasm/test_secret_leaf_sinks_1577.py:1-40` | shipped as a refusal | On main, five backends scrub and wasm refuses |
| Trace funnel at the choke point (the python runtime trace hook, the ts runtime trace hook, and go), provider return value channel still unfunnelled | roadmap `docs/v2.0-roadmap.md:1587-1597`; `tests/test_tier_host_trace_secret_421_f6.py` (8) | partial | The roadmap says java, rust and wasm are out of scope for the trace funnel (line 1587), and that the value channel has no funnel on any of the five placement tiers (line 1589) |
| Runner console funnel on five placement tiers | roadmap `docs/v2.0-roadmap.md` item 421 F6(c) and the 2026-09-11 correction at line 1591; `tests/test_seam_leak_gaps_815.py` (14) | shipped, with residuals | A go host body that panics prints its message from the Go runtime, outside every funnel; the roadmap leaves this unchanged (line 1591) |
| Name-only audit table | `src/revl/audit_diff.py:99-109`; design note section 5 | shipped | |

## Numbers that exist
- Test counts by static grep of `def test_` (not executed): `tests/test_secret_flow.py` 31, `tests/test_secret_bound.py` 30, `tests/test_seam_leak_gaps_815.py` 14, `tests/test_tier_host_trace_secret_421_f6.py` 8, go registry 19, java registry 32, rust registry 27, wasm leaf sinks 8, ts bound 6. These are counts of tests, not results.
- The roadmap lists a lettered defect series for the registry and its walks, F6(c) to F6(r) in item 421 (`docs/v2.0-roadmap.md` rows near 1587-1606): console bypass, escaped renderings, container faces, bytes faces, rust build defect, java bytes, rust leaves, cyclic walks on py, ts, java, go depth cap, map keys, variant payloads, and the Opt-is-not-indirection boxing defect (F6(k)). The letter range F6(c) to F6(r) holds 16 entries; how many of them count as registry-walk defects was not recounted.
- No leak-rate, overhead or false-scrub measurement exists. The minimum markable length (4) and minimum matchable argument length (3) are design constants (`backends/python/confidential.py:65`, `backends/python/confidential.py:80`), not measured.

## Paper outline
1. Introduction: the provider key problem in an agent process; what is and is not claimed.
2. Threat model: host-body opacity (G8), the three sinks that matter (model context, durable file, tool return).
3. Design: declaration, injection, two origins, crossing-kind raises, no declassifier.
4. The retraction: why the no-eliminator type was vacuous (note lines 38-54) and the A8 exploit that forced disjoint origins.
5. Formalisation: the three theorems, the non-vacuity witness, the stated gap.
6. The registry: leaf registration, renderings, the single funnel, bounded matching; wasm as a refusal.
7. Evaluation (to be done): canary matrix, mutant study, expressiveness cost.
8. What the defect series teaches (short; the bug-class argument belongs to the reach-completeness paper).
9. Limits and related work.

## Threats to validity and limits
- The note's own words: the guarantee is "no revl construct and no declared crossing carries the secret out", never "the process cannot send the key" (`docs/design/256-capability-bound-secrets.md:104-107`). Attack A3 is open.
- Exact-match scrubbing misses a value the host reformats (truncation, case fold, a `%.2f`); `redact_call_text` says so itself (`backends/python/confidential.py` docstring at line 661).
- The note says the secret raise lives in a flow analysis whose walk coverage is a separate obligation; `formal/RevL/Theorems/G9_NoAuthorityFromUntrusted.lean:73` states that path coverage is not proved.
- The registry is per process and short values are not registered, so a short secret depends on positional marking alone.
- The note's header says its status section is stale in places; every status claim here was checked against code, not the note. The funnel lives in `backends/python/confidential.py` and `src/revl/_process_runner.py`.
- The trace funnel covers three tiers and the value channel none; a go host body that panics is a residual outside every funnel.
- The defect series was found by the authors and is not independent evidence. An outside red-team on the registry would strengthen it.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 45 checked, 10 corrected.
