---
id: P05
title: "Function Types and Async Colour in an Effect-Checked Language: Fixpoint, Monomorphization and Refusal Across Six Tiers"
theme: formal-pl
kind: methods
maturity: partial
status: "The colour fixpoint, monomorphization, caller-decided extern colour and the self-colouring fix are on main with tests and a Lean fragment; colour polymorphism as a type-system feature is not built, and cross-tier evidence for go, java and rust is mostly emitted shape, not executed behaviour."
fused_with:
---

## Pitch

Async colour can be a checked property of the program rather than of the target: one source runs unchanged on tiers that need colour (py, ts) and tiers that erase it (go, java, rust), and the shapes the checker's colour rules cover that would silently mis-emit on the coloured tiers are refused at the frontend with an A1 diagnostic.

## Abstract

Languages that compile to several runtimes meet an awkward fact: on some targets a suspension is a function colour (a coroutine or a promise), on others it is invisible (a blocking call). If the front end ignores colour, a program that is correct on the blocking targets leaks a coroutine or a promise on the coloured ones, and the failure is a wrong answer, not a crash. We describe how revl, an effect-checked language with six backends (python, typescript, go, java, rust, wasm), treats colour as a checked property. Function types are real, checked types, and a callback whose declared type returns `Async[T]` carries the colour. A least-fixpoint over the static call graph colours module functions, function values and spawned-handle emissions, and the checker refuses a sync context that reaches a suspension through the expression positions its call-graph walk visits (fixtures cover ternaries; no test quantifies over every expression form). A sync-to-async monomorphization lets one loop serve both a sync and an async caller, and a caller-decided extern colour lets one host body be both. A written `Async` return annotation was found in adversarial review to let an arrow certify its own colour and launder arbitrary async reach out of its enclosing scope; the rule that colour is positional closes it. The Lean model of the A1 rules proves that a Boolean per-site decision equals the per-site rule (`siteB_iff`); that the printed checker verdict matches that decision is checked on corpus rows by the oracle gate, not proved; the model states that arrow types and colour polymorphism are outside it. The test suite has 14 A1 rejection fixtures and per-file tests for each mechanism. We report what remains open: there is no colour variable in the type language, so polymorphism exists only as monomorphization at declared positions, and the cross-tier evidence for go, java and rust is largely "emits one host function", not executed async behaviour.

## Problem and claim

Nystrom's "What Color is Your Function" names the cost of colour as a user burden. revl has a different problem: the source language is meant to mean one thing before a tier is chosen (`docs/design/async-extern.md:291-294`), yet the tiers disagree on whether colour exists. The py and ts tiers colour a function that suspends; go, java and rust block and erase it (`docs/design/async-extern.md:142-167`). If colour is left to the emitter, the front end cannot know which `await` to place, and a sync arrow handed an async operation produces `resp = complete(current)` with no await on both py and ts, returning a coroutine that leaks into the next call (`docs/design/async-function-values.md:12-20`).

The claim is narrow and checkable. Colour is a property the checker computes and enforces: it is declared at exactly two kinds of position (an `async` extern or service operation, and a function type whose declared return is `Async[T]`), propagated by a monotone fixpoint, and any shape where a sync context reaches a suspension is refused with code A1. Colour is designed never to appear in an expression type (`docs/design/async-function-values.md`, section 2); no test quantifies over every expression form. `Async[T]` is legal only as the return of a function type in a parameter position (`tests/test_async_fn_values.py:91`, `:101`), and the call eliminates it.

A second claim concerns soundness of the mechanism. The `async` flag on a lowered arrow is a certificate that some declaration promised to await it, and two analyses trust it. Allowing the author to write the annotation lets an arrow forge the certificate. The paper reports this as a hole found in review, its fix (colour is positional), and where the fix is stated in the code.

## Contributions

1. Function types as checked types, replacing arrows typed `any` on the ts backend, with `Async[T]` confined to one position (`docs/function-types.md:1-20`; `tests/test_function_types.py`, 54 tests).
2. A colour fixpoint over module functions, with a witness chain per derived name, extended to function values whose parameter is declared `(...) -> Async[T]` and to spawned-handle emissions (`src/revl/emission_analysis.py:281-327`; `src/revl/lower.py:10613`).
3. Refusal of sync-reaching-async through the expression positions the walk visits, shown by 14 fixtures in `examples/rejections/` (12 `a1_*`, plus `t34_arrow_self_declared_async.rvl` and `v2_async_signature_mismatch.rvl`).
4. Sync-to-async monomorphization at provide-method, module-fn and test-block call sites, so one loop serves both callers, and the extern analogue where colour is decided at the call site and colour-erasing tiers de-duplicate to one host function (`src/revl/lower.py:11005`; `tests/test_async_arrow_polymorphism.py`, `tests/test_async_arrow_polymorphism_phase2.py`, `tests/test_poly_extern_colour.py`).
5. The self-colouring escape, its account and its two-layer fix: refuse the annotation in the checker (`src/revl/typecheck.py:2683`), assert positionality where the flag is set (`src/revl/lower.py:2421`, `:7596-7612`).
6. A mechanized model of the A1 site rules with an explicit statement of what it does not cover (`formal/RevL/Theorems/A1_AsyncColour.lean:30-43`).

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Nystrom, "What Color is Your Function?" (2015 essay) | Names the problem; colour is a user burden | revl treats colour as a derived, checked property with a refusal diagnostic and a call-site rewrite rather than a user-managed twin |
| Rust async fn / Kotlin suspend / C# async | Colour is in the signature; sync callers cannot await; libraries ship duplicate APIs | One revl `.rvl` source targets tiers that erase colour; sync callers of a colour-polymorphic loop get a sync clone instead of a hand-written twin |
| Koka effect polymorphism (Leijen; see the [related-work table](../related-work.md)) | Row-polymorphic effect types, colour as an effect variable | revl has no effect variable; polymorphism is monomorphization at declared `Async[T]` parameter positions, which is weaker but needs no change to unification (`tests/test_async_fn_values.py:85`) |
| Zig async (removed in 0.11) (citation to be checked) | Function colour inferred, call-site chooses blocking or async | revl keeps colour static and refuses maybe-sync await, because on py an unconditional await of a sync value raises (`docs/design/async-function-values.md:92-99`) |
| Bracha-style pluggable types / gradual effect systems (citation to be checked) | Optional checks over a host language | revl's checker owns emission, so the same fixpoint places the `await` and refuses the unsafe shape |
| Rytz, Odersky, Haller, "Lightweight polymorphic effects" (ECOOP 2012) (citation to be checked) | Effect polymorphism via annotations | Comparable goal; revl's route is call-site cloning of colour-polymorphic functions detected structurally (`docs/design/388-caller-decided-extern-colour.md:130-135`) |

The defensible novelty is the combination: a colour fixpoint tied to a multi-target compiler where some targets erase colour, a refusal discipline justified by "py runs while ts refuses" divergences, and the account of a self-certifying flag. Each piece alone is known.

## Evidence on main

| Claim | Where | Status | Note |
|---|---|---|---|
| Colour fixpoint over module fns, with witness | `src/revl/emission_analysis.py:281-327`; `tests/test_async_extern.py::test_async_coloring_is_a_transitive_fixpoint` | shipped | The docstring states there is deliberately no `*` widening for first-class values |
| Async-typed parameter seeds the fixpoint | `src/revl/emission_analysis.py:29-34`, `:304-322`; `tests/test_async_fn_values.py::test_agent_loop_is_colored_async_by_its_async_typed_param` | shipped | |
| `Async` legal only as fn-type return in a module fn parameter | `tests/test_async_fn_values.py::test_async_is_legal_only_as_a_fn_type_return_in_a_param`, `::test_async_fn_type_refused_outside_a_module_fn_param` | shipped | |
| Sync arrow flows into async slot; async does not flow into sync | `tests/test_async_fn_values.py::test_sync_flows_into_async_as_a_coercion`, `::test_async_does_not_flow_into_sync` | shipped | |
| Handle-based emissions are seen by the analysis | `src/revl/lower.py:10613` (`_async_service_op`); `tests/test_async_spawn_handle_color_98.py`; `tests/test_async_handle_colour.py` | shipped | The module docstring describes the pre-fix py failure: "coroutine ... was never awaited" |
| Sync ternary reaching async is refused | `examples/rejections/a1_async_op_sync_ternary.rvl`; `tests/test_formal_a1_async_1808.py:44` | shipped | `docs/roadmap-archive/07-open-in-rough-priority-order.md:2086` describes the pre-fix `{}` on ts (not re-run) |
| Monomorphization at provide, module-fn and test sites | `src/revl/lower.py:11005`; `tests/test_async_arrow_polymorphism.py::test_exit_py_executes_both_paths`; `tests/test_async_arrow_polymorphism_phase2.py::test_py_executes_both_sync_paths` | shipped | The second phase exists because the first hooked only `_lower_provide`; py ran while ts refused (module docstring) |
| Genuinely async reach from a sync caller is still refused | `tests/test_async_arrow_polymorphism.py::test_sync_caller_passing_an_async_arrow_is_still_refused`; `tests/test_async_arrow_polymorphism_phase2.py::test_test_reaching_loop_with_a_genuinely_async_arrow_is_refused` | shipped | |
| Caller-decided extern colour; py and ts goldens equal two hand-written externs | `tests/test_poly_extern_colour.py::test_poly_extern_emits_byte_identical_to_two_hand_written_externs` | shipped | Soundness rests on an author claim that the host body is await-free; lint only (`docs/design/388-caller-decided-extern-colour.md:485-499`) |
| go, rust, java emit one host function for a two-colour extern | `tests/test_poly_extern_colour.py::test_colour_erasing_tier_emits_one_host_function` | shipped (shape only) | Asserts text count, does not execute |
| Self-colouring `Async` annotation refused | `src/revl/typecheck.py:2683`; `examples/rejections/t34_arrow_self_declared_async.rvl`; `tests/test_frontend.py:223` | shipped | The header of `docs/design/75a-arrow-parameter-annotations.md:3` still says "design proposed"; the code landed |
| Positional-colour assertion at the flag site | `src/revl/lower.py:2421`, `:7596-7612` | shipped | |
| Self-host gate agrees on the refusal | `tests/test_selfhost_lower.py:4703-4755` | shipped | The fixture moved out of the pinned type-layer gap |
| Explicit `await` in activations, LIFO teardown across awaits | `backends/python/tests/test_async_effect_runtime.py::test_abort_during_inflight_acquisition_is_lifo`; `tests/test_async_effect_composition.py`; `docs/v2.0-roadmap.md:1380` (marked done) | shipped on py; ts has `backends/typescript/tests/async_effect_composition.test.ts` | The header of `docs/design/131-async-effect-composition.md:3` still reads "design proposed". Not run for this write-up |
| ts emits async signature and awaited call; py emits `async def` | `tests/test_async_extern.py::test_ts_emits_async_signature_and_awaited_call`, `::test_py_emits_async_extern_as_async_def_and_awaited_call`, `::test_exit_py_async_extern_await_runs_with_no_coroutine_leak` | shipped | |
| wasm refuses awaited effect and emit steps | `backends/wasm/emit.py:1673-1685` | shipped | |
| wasm refuses an async extern | design text only, `docs/design/async-extern.md:404-415` | designed, not found in code | The code does the opposite for poly externs: `backends/wasm/emit.py:6965-6976` treats wasm as colour-erasing |
| Mechanized A1 site rules | `formal/RevL/Theorems/A1_AsyncColour.lean:44-211`; `formal/STATUS.md:61`, `:370-375` | proved (fragment) | The Lean file says arrow types and colour polymorphism are out of fragment (`:37-43`) |

## Numbers that exist

- 14 A1-family rejection fixtures in `examples/rejections/` (12 `a1_*`, `t34_arrow_self_declared_async.rvl`, `v2_async_signature_mismatch.rvl`).
- Static test-function counts (not parametrized expansions): `tests/test_async_extern.py` 21, `tests/test_async_fn_values.py` 17, `tests/test_function_types.py` 54, `tests/test_poly_extern_colour.py` 13, `tests/test_async_arrow_polymorphism.py` 8, `tests/test_async_arrow_polymorphism_phase2.py` 8, `tests/test_async_sync_scope.py` 8, `tests/test_async_match_block_arm.py` 6, `tests/test_async_spawn_handle_color_98.py` 5, `tests/test_async_handle_colour.py` 4, `tests/test_formal_a1_async_1808.py` 10, `tests/test_async_effect_composition.py` 4, `tests/test_async_provide_method_acquisition_322.py` 3.
- Lean: 10 `RevL.A1Async` rows in `formal/STATUS.md:370-379`, matching the ten named A1 theorems in `formal/RevL/Theorems/A1_AsyncColour.lean`. Axiom lists are as the status file states them and were not re-checked, since the Lean tree was not built.
- Nothing else is measured. There is no benchmark of how often the refusals fire, no count of real programs saved, and no user study.

## Paper outline

1. Introduction: one source, two colour regimes; the leak that motivated it.
2. Background: tier families (colour-bearing, erasing, refusing).
3. Declaring colour: externs, service operations, `Async[T]` function types; why no expression has the type.
4. The fixpoint: seeds, witness chains, handle emissions, expression positions, stop-at-async-arrow.
5. Reuse without twins: monomorphization of callers; caller-decided extern colour; the author-claim limit.
6. The certificate problem: self-colouring annotation, positional colour, the two analyses that trust the flag.
7. What is mechanized and what is not.
8. Evaluation: a divergence ledger (what the unchecked system did on each refused shape), fixture inventory, cross-tier execution.
9. Related work and the open problem (colour variables).

## Threats to validity and limits

- The Lean model "has no arrow types" and colour polymorphism "is not in the call graph" (`formal/RevL/Theorems/A1_AsyncColour.lean:37-43`). The soundness evidence for the central mechanism (arrows, monomorphization) is tests, not proof.
- What is genuinely open is narrow: there is no colour variable in the type language. Polymorphism is detected structurally (a function is colour-polymorphic iff its only async reach is its own async-typed parameters, `docs/design/388-caller-decided-extern-colour.md:130-135`), applies to declared `Async[T]` parameters of module fns, and is excluded from the Lean model.
- Caller-decided extern colour rests on an unchecked author claim that the host body is colourless; only a syntactic `await` lint backs it (`docs/design/388-caller-decided-extern-colour.md:485-499`).
- The fixpoint is name-based. The stream design names the same blind spot as a failure mode of a "name-based walk" (`docs/design/130-stream-reactive-types.md:154-160`). New suspension sources must be seeded explicitly, so the guarantee depends on enumeration.
- Erasing tiers are argued equivalent, not shown equivalent: ordering within a task is promised and interleaving is not (`docs/design/130-stream-reactive-types.md:447-453`).
- Cross-tier evidence for go, java and rust is "one host function is emitted" (`tests/test_poly_extern_colour.py:282-296`), not executed async behaviour.
- Design documents on main disagree with the code in places: several headers still say "design only" or "design proposed" for features whose code landed, and the wasm async-extern refusal in the design text is not in the code.
- Citations marked "to be checked" have not been checked.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 19 checked, 3 corrected.
