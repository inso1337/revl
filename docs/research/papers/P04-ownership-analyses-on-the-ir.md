---
id: P04
title: "Ownership Analyses That Ride the IR: Unique Accumulation and Owned/Borrowed Teardown Obligations"
theme: formal-pl
kind: short
maturity: partial
status: "The accumulation analysis is on main with deterministic element-copy counts on Python and TypeScript, and the owned/borrowed checks have tests; no timings, no soundness argument and no real-code coverage numbers exist, and shared handles are specified but not built."
fused_with:
---

## Pitch

Decide once, in the frontend, whether a name uniquely owns its object, stamp that fact on the IR, and let each backend choose what to do with it. On the Python backend this took accumulation loops from quadratic to linear copying, and three of six backends read the marker today.

## Abstract

Compilers that target several runtimes face a repeated problem: a source-level aliasing fact is needed on every target, and each backend re-derives it. In revl, a language compiled to six targets, `out = out.push(x)` is a persistent write that copies its receiver, so the loop a programmer wrote as linear was emitted as quadratic on each audited target that copies on write (Python, Go and TypeScript). Two backends had independently written the same unsoundness-prone aliasing proof. We moved it into the frontend as a flow-sensitive forward dataflow with a fixpoint on loop back edges, plus a whole-program retention summary for calls, and stamp the result on the IR as a fact (`unique: true` or `"copy"`), not an instruction. On the Python backend, element copies when building a 1000-element list drop from 500500 to 1000, growth per doubling from x4.0 to x2.0, and the library `list_sort` and `list_dedup` reach the hand-written baseline in copy count. The TypeScript and Go backends read the same marker; Rust discharges the obligation with its borrow checker; Java and WebAssembly decline it for named representation reasons. We also describe a second ownership analysis on the same pattern, over the resource teardown accumulator: acquired handles are nominal opaque types (R0), the declared inverse cannot be hand-called (O1), and a seven-clause check keeps borrows from escaping (B1). The first analysis is on main and measured by deterministic element-copy counts (no timings); the second ships for owned and borrowed modes, with shared handles specified and deferred.

## Problem and claim

Two separate ownership questions recur in revl. For accumulation: is the object this local names reachable through any other name at this write? For resources: which component owns the inverse of an acquired handle, and can any other position close it or keep it? Both are questions about the source program, not about a target language, and both have a wrong answer that is silent: an aliasing bug that changes a value, or a double close.

The claim of the first half: computing the uniqueness fact once, flow-sensitively and per write, in `src/revl/ownership.py`, is designed to fail safe, with every uncertainty resolving to not owned. That safety is argued from the fallback and the test suite, not proved. The analysis gives backends a shared fact in place of per-backend proofs. Ignoring the marker is always correct, which is what lets backends adopt it independently. The claim of the second half: owned and borrowed modes can be inferred over the teardown accumulator with no new grammar, and a does-not-escape check over the existing scope structure is enough for v1.

These are two analyses with different machinery (a dataflow over locals, a nominal-type and escape discipline over handles). They share a design principle (compute in the frontend, ride the IR or the checker, keep the conservative direction) but not code beyond a common statement of aliasing. The paper either leads with the first half and treats the second as a second application, or is split.

## Contributions

1. A flow-sensitive forward dataflow (lattice FRESH < COPY < absent, join is maximum, births kill escapes, escapes gen) that decides per write whether a self-rebinding assign owns its object (`src/revl/ownership.py:1-111`).
2. A marker protocol that carries a fact and not an instruction: `unique: true`, `unique: "copy"` with `unique_birth` on the introducing `let`; absent when no write qualifies, so IR for other programs is byte-identical (`src/revl/ownership.py:27-47`).
3. A whole-program retention summary (least fixpoint over the call graph, optimistic start) that answers whether a callee retains a parameter, closing the `list_dedup` case (`src/revl/ownership.py:617-678`).
4. Measurements on the Python backend with a deterministic element-copy counter that rewrites the emitted module's AST (`bench/codegen/python/copycount.py:1-30`): 500500 to 1000 at n=1000, `list_sort` 171700 to 5050 at n=100 and 1353400 to 20100 at n=200, `list_dedup` 5050 to 100 at n=200.
5. A backend-by-backend account of what the marker buys and where it is declined: TypeScript and Go read `true` only, Python also reads `"copy"`, Rust relies on its borrow checker, Java's list container is immutable, and Wasm's list header has no capacity field.
6. For resources: R0 (acquire returns are nominal opaque handles), O1 (no hand-call of a declared inverse, owner or borrower), B1 (a borrow does not escape, seven clauses), with owned and borrowed landed and shared teardown specified for a counted-grant ledger and deferred (`docs/design/308-effect-ownership-modes.md:1-22`, `:130-220`, `:398-440`).

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Perceus reference counting (Koka; see the [related-work table](../related-work.md)) | Precise reference counting with reuse; in-place update when the count is one at run time | revl decides at compile time, flow-sensitively, from the source, and passes a fact to backends that have no reference counts (JS, Go) |
| Lean 4 destructive updates (citation to be checked) | Runtime uniqueness check, update in place when exclusive | No runtime check; a static marker per write |
| Swift copy-on-write and `isKnownUniquelyReferenced` | Run-time uniqueness test on a buffer | Static, source-level, one analysis shared by six targets |
| Rust ownership and borrowing | Types enforce unique ownership and borrows | revl does not expose it to users; the Rust backend gets the marker's obligation discharged by rustc (`docs/v2.0-roadmap.md:1860-1861`: Rust needs none of it) |
| Clojure transients | Programmer opts into mutation of a fresh persistent structure | Compiler infers the safe cases and the programmer writes the persistent form |
| Linear Haskell, Cyclone regions, Vault | Linear types, regions, resource protocols in the type system | revl's resource half is not a lifetime system: "no lifetime parameters, no region variables" (`docs/design/308-effect-ownership-modes.md:186-192`); positional inference with false positives allowed |
| RAII and capability-based resource management | Scope-bound release; authority tokens | The owner is the activation whose teardown accumulator registers the inverse; O1 closes the out-of-band channel |
| Escape analysis (Choi et al.) and alias analyses in JITs (citation to be checked) | Intraprocedural/interprocedural escape for allocation decisions | Here the analysis result is serialized onto an IR consumed by multiple independent code generators, with a self-hosted reimplementation checked for IR equality on the corpus |

## Evidence on main

| Claim | Where | Status | Note |
|---|---|---|---|
| Frontend analysis exists, flow-sensitive with loop fixpoint, conservative fallback | `src/revl/ownership.py:1-111` (701 lines) | shipped | Called from lowering: `src/revl/lower.py:9446` (`ownership.annotate_ir`) |
| Marker semantics: `unique`, `"copy"`, `unique_birth` | `src/revl/ownership.py:27-47` | shipped | |
| Python backend honours `true` and `"copy"` | `backends/python/emit.py:4364-4381`; `tests/test_py_inplace_accumulation.py` (16 tests, for example `test_push_loop_becomes_append:77`, `test_local_born_off_a_name_takes_one_defensive_copy:135`) | shipped | |
| Flow-sensitivity case: escaped accumulator re-born from a literal is owned again | `tests/test_py_inplace_accumulation.py:440` | shipped | The `list_sort` shape |
| Library push loops are linear | `tests/test_py_inplace_accumulation.py:118` | shipped | |
| Aliasing negatives (escape keeps the copy, parameter never written, iterating keeps the copy) | `tests/test_py_inplace_accumulation.py:249`, `:376`, `:392` | shipped | |
| Retention summary, extern calls always retain | `tests/test_py_inplace_accumulation.py:258`, `:286`, `:310`, `:330` | shipped | |
| TypeScript honours `true`, declines `"copy"` | `backends/typescript/emit.py:3754-3802`; `backends/typescript/test_unique_writes_ts.py` | shipped | |
| Go honours `true` only | `backends/go/emit.py:7821-7826`, `:8148` | shipped | The Go lowering predates the frontend marker; its tests were not opened for this write-up |
| Self-host reimplementation's IR `functions` section equals the reference (parsed-JSON equality) on the whole lower corpus | `selfhost/lower.rvl:29569` and the `own_*` block; `tests/test_selfhost_lower_ir.py:221`, `:980` (`CORPUS_UNIQUE_AWARE = CORPUS`, so every corpus document, not an accumulation-only subset) | shipped | The earlier strict-xfail was deleted |
| Self-host emitters read the marker | `selfhost/emit_py.rvl:1269-1290`; `selfhost/emit_ts.rvl:1512-1524`; `selfhost/emit_go.rvl:1826` | shipped | |
| Rust, Java, Wasm do not read the marker | no `unique` reads in `backends/rust/emit.py`, `backends/java/emit.py`, `backends/wasm` (grep); reasons in `docs/v2.0-roadmap.md:1860-1861` | shipped (as a documented non-adoption) | Java and Wasm were first "not audited here" (line 1859), later given concrete blockers |
| 500500 to 1000 element copies, x4.0 to x2.0 growth | `docs/v2.0-roadmap.md:1754`, `:1769`; `bench/codegen/python/copycount.py`; reproduce with `PYTHONPATH=src python bench/codegen/python/run.py` | shipped | No timings were taken or quoted, by the audit's own choice |
| `list_sort` 171700 to 5050 (n=100), 1353400 to 20100 (n=200); `list_dedup` 5050 to 100, 20100 to 200 | `docs/v2.0-roadmap.md:1857-1858` | shipped | Counts match the hand-written arm |
| TypeScript: 79800 to 0 element copies at n=400 | `docs/v2.0-roadmap.md:1862`; `backends/typescript/test_unique_writes_ts.py:1-20` | shipped | The TypeScript bench in `bench/codegen/typescript` was not opened |
| R0, O1, B1 checks, owned and borrowed v1 | `docs/design/308-effect-ownership-modes.md:1-22`; `src/revl/resources.py:1-40`; `tests/test_ownership_modes_308.py` (49 tests, for example `test_r0_primitive_acquire_return_refused:73`, `test_o1_borrower_close_refused:111`, `test_b1_clause2_closure_capture_refused_owner_too:167`, `test_owner_holds_and_lends_admits:279`) | shipped | O1 and B1 live in `src/revl/lower.py` per the `resources.py` header |
| Method-scope early release decided, refused shape stays refused | `tests/test_ownership_modes_308.py:328-408`; `tests/test_ownership_f9_runtime_308.py` (5 tests) | shipped | |
| Retention audit, report only | `tests/test_ownership_modes_308.py:437-487` | shipped | |
| Shared and transfer | `docs/design/308-effect-ownership-modes.md:3-22`; `tests/test_ownership_modes_308.py:510-630` | designed / partial | The `shared` marker is carried and checked at acquire; the counted-grant ledger teardown "stays deferred" (design doc line 15) |
| Closure capture decision: by-value only, reference capture refused | `docs/closures.md:1-60`; `tests/test_v2_emit.py:364` | shipped | Pinned by `test_arrow_captures_var_by_value_at_creation_time` |

## Numbers that exist

- Python element copies at n=1000: `build`, `doubled`, `fill` 500500 to 1000; `drop_keys` 499500 to 2000; `shift_all` 503500 to 4000 (`docs/v2.0-roadmap.md:1754`). Five of six probes match the hand-written column; growth x4.0 to x2.0 per doubling. Deterministic counts, one program per probe, one machine-independent counter.
- `list_sort`: 171700 to 5050 at n=100, 1353400 to 20100 at n=200, growth x7.9 to x4.0; `bytes` 1.50x to 1.00x; `ops` 1.26x to 1.27x (the stated cost) (`docs/v2.0-roadmap.md:1857`).
- `list_dedup`: 5050 to 100 at n=200, 20100 to 200 at n=400, x4.0 to x2.0 (`docs/v2.0-roadmap.md:1858`).
- TypeScript: 79800 to 0 elements copied at n=400 (`docs/v2.0-roadmap.md:1862`).
- Java `list_push`: 221224 B/op emitted against 286760 hand-written (`docs/v2.0-roadmap.md:1723`); line 1861 calls the hand-written Java file a persistent yardstick, so the win needs a mutable-arm case.
- Test counts: 16 (Python accumulation), 49 (ownership modes), 5 (runtime), counted from `def test_` lines.
- No timings exist, on purpose; the audit states none were taken because the host was under load (`docs/v2.0-roadmap.md:1753`).
- No numbers for how many real programs have a qualifying write, analysis compile-time cost, or the false-positive rate of B1 on real code.

## Paper outline

1. Introduction: one aliasing proof written twice, six backends; why a fact on the IR.
2. The marker protocol: `unique`, `"copy"`, `unique_birth`; why a fact and not an instruction; byte-identical IR when absent.
3. The analysis: lattice, birth, escape, self-rebind kills, loop fixpoint, conservative fallback list.
4. Calls: retention summary as a least fixpoint; extern, host root and function-value callees always retain.
5. Backends: what each does with the marker (Python, TypeScript, Go lower it; Rust relies on rustc; Java and Wasm blockers).
6. Evaluation: copy counts, growth curves, timings (to be added), real-code coverage, fuzzing.
7. Resource ownership as a second application: R0, O1, B1, owned and borrowed, shared deferred; closure capture decision (by value only) as the rule that keeps both analyses simple.
8. Related work and limits.

## Threats to validity and limits

- Soundness is not proved. The module docstring says "a wrong answer here is a wrong ANSWER and not a crash" (`src/revl/ownership.py:16-18`); safety is the conservative fallback plus tests.
- Measured improvements are element-copy counts from an instrumented rewrite of the emitted Python module, not time. The audit chose this deliberately; `ops` got slightly worse for `list_sort` (1.26x to 1.27x).
- Only Python and TypeScript have measured wins; Go's lowering predates the frontend marker; Rust, Java and Wasm are unmeasured or declined. Three of six backends read the marker, so "six backends share a fact" is not what main shows.
- `concat` is never marked because its receiver type is unknown at the node and no tier can mutate a `Str` in place (`src/revl/ownership.py:115-119`). A common string-building loop is therefore not helped.
- A local born from a call result is refused; it needs a freshness summary that "was not attempted" (`docs/v2.0-roadmap.md:1860`).
- Resource half: the inference is positional and the design doc accepts false positives; a legitimate checkout/checkin shape is "deliberately inexpressible in v1" (`docs/design/308-effect-ownership-modes.md:164-171`). Shared and transfer are not implemented. B1's closure clause was chosen as an outright refusal ("a strictly smaller check", design doc line 417), not because the closure-taint alternative was shown unnecessary.
- The self-host agreement is equality of the parsed IR `functions` sections on the corpus, not a proof.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 50 checked, 11 corrected.
