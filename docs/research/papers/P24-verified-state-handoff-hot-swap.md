---
id: P24
title: "Hot Swap That Refuses Unsafe State Migration: Typed Hand-Off and the No-Laundering Rule"
theme: lifecycle-swap
kind: full
maturity: partial
status: The declared hand-off gate, warm start and undo reporting are shipped with tests on the Python tier; the default instance-migration path has a named, test-pinned, unfixed correlation hazard, and no measurements exist.
fused_with:
---

## Pitch

Erlang's code_change hands state from old code to new and trusts the new module to cope; revl checks the hand-off type at admission, before teardown, so a mismatch is refused with the running composition untouched, and it refuses to carry state across a cold-start path.

## Abstract

Hot code replacement is only useful if long-lived state survives it, and only safe if the new code can actually hold that state. Erlang/OTP leaves the decision to a user-written code_change callback; dynamic software updating systems such as Ginseng, Kitsune and Proteus verify state transformers against types or update points, usually for C programs. We describe the state transfer protocol of revl, a language whose components acquire resources through effects with declared inverses and are hot-swapped in a running composition. A swap drains in-flight calls, captures state, tears the old provider down in derived LIFO order, loads the successor and restores. Two channels carry state. Live instances of a spawned template migrate through a snapshot and restore protocol (`__revl_state__` and `__revl_restore__`) gated by a same-length, same-type check on the resource vector. Composition-level providers declare a handoff type; the admission gate checks the successor's accepted type against the predecessor's exported type with the same compatibility relation used for interface drift, refuses a mismatch with a trace that names both shapes before teardown, and threads the value into the successor on success. The instance channel checks the whole cohort before writing and rolls the swap back on rejection; a defence-in-depth check on the hand-off path also rolls back. State never crosses a respawn or a rollback (the no-laundering rule). Under the declared policy an agent-authored successor receives operator state either through a declared and type-compatible hand-off, or through the instance channel onto a template it re-declares unchanged (the state is re-seated on the operator's own text). It is refused before teardown when it changes a live template, or changes or adds a holder or a service consumer that can observe one. We document one unfixed hazard, same-typed resources reordered by a successor, pin it with a test, and list the evidence on main: 13 test functions in the hand-off grammar and gate file (8 gate, 5 grammar and parse), 4 execution tests, 7 undo-dossier tests, 4 instance-migration tests, and 4 instance-capture tests plus 4 declared-consumer tests in the two declared-swap modules. These are counted from the files, not run for this draft.

## Problem and claim

A stateful provider (a cache, a session store, a rate limiter) loses its world when the old version is torn down. The swap then degrades to restart with extra steps and no diagnostic. The two usual cures both have a hole. Trusting the successor to accept any state, as OTP does, lets a type mismatch surface later as wrong data. Positional or structural migration, as in many live-reload tools, can silently put state in the wrong slot.

The claim: a swap can be made all-or-nothing with respect to state, for the shapes the gate can see. Either the successor is checked to accept the predecessor's exported shape (checked at admission, re-checked on the live resource vector as defence in depth), or the swap is refused and the running composition is exactly as it was. And the negative half: no path that starts the successor cold (respawn, rollback, undo) carries state, because carrying it would launder one generation's writes into the other generation's code.

What the paper does not claim: that the declared hand-off type is a full behavioural contract (it states shape, not meaning), or that instance migration is sound when a component holds two resources of one type (that case is a known, pinned wrong result).

## Contributions

1. A snapshot and restore protocol for effect-acquired resources with a same-length, same-type gate and cohort-wide check-then-apply, with rollback on rejection (`StateIncompatible`).
2. A declared hand-off (a prelude declaration, one per component, additive IR field) whose type reads as "exports" for the predecessor and "accepts" for the successor, checked with the covariant case of the existing compatibility relation; correlation by provided key, so renamed successors still match.
3. The no-laundering rule: respawn, cold start, rollback and undo carry no state, and the undo report names the state it drops. Under the declared policy an agent-authored successor receives operator state through a declared, type-compatible hand-off, or through the instance channel onto a template it re-declares unchanged; a live template it changes, and a holder or service consumer of that template that it changes or adds, refuses with `STATE_UNDISCLOSED` before teardown.
4. A named and test-pinned hazard (same-typed resource reorder under positional migration) with the narrowed fix stated.
5. A statement of what instantiation as acquisition does not give: request-scoped instances leak without sub-component teardown scopes.

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Erlang/OTP code_change | Old process calls the new module's callback to convert state; callback is user code, unchecked | Shape is declared and checked at admission; rejection rolls back; no callback to get wrong, but also no arbitrary transformation (see limits) |
| Ginseng (Neamtiu et al.), Kitsune (Hayden et al.), Proteus (Stoyle et al.) (exact references: citation to be checked) | Dynamic software updating for C-like programs, with state transformers checked at update points or by type (per-system detail: citation to be checked) | State is the effect-created resource vector of a component; teardown and acquisition are already tracked by the language, so there is no update-point selection problem |
| OSGi | Bundle update with service re-binding; state is the bundle's problem | Admission refuses a swap whose state cannot be held; consumer and provider are protected by the interface relation |
| Elm and React hot reload | Preserve state when the shape appears unchanged, reset otherwise (heuristic) (citation to be checked) | Reset is an explicit author choice (a successor with no hand-off starts cold); mismatch is a refusal, not a silent reset |
| Live migration (CRIU, VM) | Copies the whole process image to another host, same code | Different code; a declared hand-off value or an unchanged re-declared template's instance state crosses, and any other live state is refused or starts cold |
| Schema migration tools | Versioned transformations of stored data | Not a transformation framework; shape-compatibility only |

## Evidence on main

| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Swap protocol: gates, capture, teardown, load, restore, rollback | `src/revl/mcp/session.py:1692` (`Session.swap`), `src/revl/mcp/session.py:1750` (`_plan_swap`), `src/revl/mcp/session.py:1901-1917` | shipped | Failure before teardown puts the session back from a checkpoint (issue #1446 text at `src/revl/mcp/session.py:1722-1727`) |
| Migration policies generational / respawn / declared | `src/revl/mcp/session.py:1696-1727` | shipped | Default is generational |
| `__revl_state__` / `__revl_restore__` snapshot protocol | `backends/python/runtime.py:7585-7592` (Map), `backends/python/runtime.py:1743-1778` (`capture_state`, `check_state`) | shipped on cordis-py | Other tiers: not verified for this draft |
| Same-length same-type gate, `StateIncompatible`, rollback | `backends/python/runtime.py:1694-1698`, `backends/python/runtime.py:1760-1778`; `src/revl/mcp/session.py:1901-1917`; `tests/test_instance_migration.py::test_incompatible_successor_is_rejected_and_rolled_back`, `::test_state_migrates_onto_a_compatible_successor` | shipped | `docs/design-v2-instances.md:510-525` states the protocol |
| Same-typed reorder hazard named and pinned | `docs/design-v2-instances.md:529-541`; `tests/test_instance_migration.py::test_reorder_hazard_same_typed_resources_migrate_to_the_wrong_slot` | shipped as a known defect | The test asserts the WRONG (crossed) result so a fix trips it. Fix (stable-key correlation) still open |
| Handoff declaration, additive IR field, `ir_version` stays 3 | `docs/state-handoff.md:20-60` (`ir_version` 3 at line 47); `tests/test_state_handoff.py` (parse, lower, prelude position, at most one) | shipped | |
| Admission gate on the hand-off type, why.kind `state-handoff-drift` | `docs/state-handoff.md:62-100`; `src/revl/admission.py` (state-handoff-drift trace, about line 502); `tests/test_state_handoff.py::test_incompatible_successor_is_refused_at_admission`, `::test_refusal_names_both_shapes_and_carries_a_why_trace`, `::test_narrowed_acceptor_is_refused`, `::test_widened_acceptor_is_admitted` | shipped | Refusal happens at admission, before teardown; no rollback is needed on this path |
| Warm start and admission refusal on cordis-py | `tests/test_state_handoff_exec.py::test_state_crosses_to_a_compatible_successor`, `::test_incompatible_handoff_is_refused_at_admission_state_intact`, `::test_successor_without_handoff_starts_cold`, `::test_stateless_swap_reports_no_handoff` | shipped | cordis-py only; no test in this file names a rollback on the hand-off channel |
| No laundering across respawn | `src/revl/mcp/session.py:1708-1712` (docstring), `src/revl/mcp/session.py:1803-1809` (capture skipped under respawn) | shipped | No test found that names the rule for this path; undo tests below cover the user-visible part |
| Undo reports dropped state, never restores it | `src/revl/mcp/session.py:2910-2920` (`_undo_state_dropped`); `tests/test_mcp_undo_handoff_state_2346.py` (7 tests, e.g. `::test_undo_after_a_handoff_swap_reports_the_state_it_drops`) | shipped | |
| Declared policy: agent successor gets state via a declared compatible hand-off, or via the instance channel onto an unchanged re-declared template; STATE_UNDISCLOSED otherwise, including an added holder or service consumer | `src/revl/mcp/session.py:1716-1727`; `src/revl/gate.py:1119-1120`; `tests/test_self_extending_334.py::test_propose_refuses_to_inherit_live_instance_state_across_trust_boundary`, `::test_provider_handoff_state_migrates_across_propose`, `::test_propose_refuses_type_incompatible_handoff_inheritance`; `tests/test_mcp_declared_instance_capture_2673.py`; `tests/test_mcp_declared_consumer_2679.py` | shipped | Test names checked; bodies not read in full. The STATE_UNDISCLOSED refusal is on the propose gate |
| Defence in depth accept-vs-export type check | `src/revl/mcp/session.py:2372-2384` | shipped | |
| Structural interface compatibility at admission | `docs/service-compat.md:1-83`; `tests/test_service_compat.py` (16 tests) | shipped | Model for the state gate; belongs mainly to P34 |
| Instantiation as acquisition and request-scoped leak | `docs/design-v2-instances.md:79-106` (the "unbounded leak" text, about line 100); spawn form `src/revl/parser.py:647-661`; `SpawnHandle` `backends/python/runtime.py:1701` | partial | The doc's own words: "an unbounded leak" without sub-component teardown scopes |
| Ambient hot admission against running manifest | `src/revl/mcp/admit_bridge.py` (module docstring) | shipped | Cited as part of the admission path, not read deeply |

## Numbers that exist

- Test counts by file on main: `tests/test_state_handoff.py` 13 test functions (8 gate: admitted, widened, narrowed, incompatible, refusal trace, renamed, no-handoff, cold-key; 5 grammar and parse: parses and lowers, absent, must target a key, must precede effects, at most one), `tests/test_state_handoff_exec.py` 4, `tests/test_instance_migration.py` 4 named (lines 113, 140, 165, 235), `tests/test_mcp_undo_handoff_state_2346.py` 7, `tests/test_service_compat.py` 16. These are counts of test functions, not run results; none were run for this draft.
- No performance or scale measurement of capture, restore or swap latency exists in the files read. No corpus of real swaps.

## Paper outline

1. Introduction and the state loss problem on hot swap.
2. Background: components, effects with inverses, derived LIFO teardown, swap and admission.
3. Two channels: instance migration (snapshot protocol, same-type gate) and declared hand-off (exports and accepts, covariant check, key correlation).
4. Atomicity: capture before teardown, check the cohort before applying, rollback; defence in depth.
5. No laundering: respawn, rollback, undo; the declared policy for untrusted successors.
6. Hazards: same-typed reorder (pinned), request-scoped instance leak.
7. Evaluation (to be done), comparison with OTP code_change and DUS systems.
8. Limits and conclusion.

## Threats to validity and limits

- A declared type states shape, not meaning. A successor that accepts Map[Str, Str] but reinterprets keys passes the gate. The paper must not claim semantic safety.
- The instance channel is positional and has a pinned wrong result for same-typed reorders; the design doc calls it a "Named hazard (canonical, not yet closed)".
- Everything executed is on cordis-py. The state protocol on the other five tiers was not checked for this draft.
- The widened-acceptor rule (Opt[T] accepts T) relies on the compatibility relation; its soundness here is by reuse of that relation, not a new proof.
- There is no state transformer: a change of representation cannot be expressed, so the only options are a compatible type, an opt-out cold start, or a refusal. This is a design choice that limits expressiveness relative to code_change and DUS transformers.
- The leak with request-scoped instances means "instantiation is acquisition" is only partly true today.
- No measurements exist.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 19 checked, 5 corrected.
