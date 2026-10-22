---
id: P28
title: Revertible Effects on a Durable Workflow Engine: A Temporal Emission Target With Derived Compensation
theme: lifecycle-swap
kind: tool-demo
maturity: partial
status: Emitter, golden output, retry derivation and a live dev-server saga test exist on main; signals, timers, realms, versioning and the Python SDK target are not built, and no fault sweep exists.
fused_with:
---

## Pitch

A Temporal workflow whose compensation order, write-ahead registration and retry policy are derived from checked declarations instead of written by hand, and which refuses, with a why-trace, anything it cannot map.

## Abstract

Durable workflow engines such as Temporal ask authors to follow two rules that the engine can only document: workflow code must be deterministic, and all input and output must live in activities. A third rule, the correct order and idempotence of saga compensations, is entirely hand-written. We describe `revl emit --target temporal`, a rendering mode of the revl TypeScript emitter that turns a component's activation into a Temporal workflow and each boundary crossing into an activity. Because revl already separates pure and effect code from emissions at compile time, the workflow/activity split is derived rather than authored. Each `effect ... undo ...` and `emit ... compensate ...` registers its inverse on an explicit stack before the forward activity is awaited, and the stack drains in reverse source order, the order revl's derived teardown (G7) fixes. Retries are derived from per-declaration idempotency evidence: only a crossing with a forward idempotency key, and not declared `validated`, gets a bounded retry policy; every other activity is at-most-once. Any step or expression kind in an activation body that falls outside a closed allowlist is refused with the construct and source line; the allowlist covers human-approval gates, spawn, and host-pinned inverses. Component-level realm, reactive, lease and hot-swap declarations are not checked by the emitter. The target is a code generator: it does not provide durable execution, and it cannot observe Temporal's replay. On main it is exercised by 26 emitter tests against a committed golden, and by a five-test live saga run against a Temporal dev server in a CI job that does not run on pull requests. No CI result was inspected for this draft. We report what the live run shows and what it does not.

## Problem and claim

Hand-written Temporal sagas have three known failure points: a compensation registered after its forward activity returns is lost when the activity commits but reports failure; a compensation that inherits retry policy can double-apply (a second refund); and a workflow that quietly performs IO or non-deterministic work diverges on replay. The design note for this target lists the first as CRITICAL 1 of its 2026-09-01 adversarial review, and attributes the second to an earlier review (`docs/design/253-temporal-target.md:14-45`). That is itself evidence that they are easy to get wrong.

The claim is narrow. For the subset of revl that maps, the compiler already holds the facts needed to emit the correct shape, so the emitter derives it: split from the stratum discipline, order from G7, retry from the idempotency ledger. For everything else the emitter refuses. The claim is not that revl provides durability, nor that the emitted workflow is replay-safe in Temporal's sense beyond "the source has no illegal IO in workflow position" (`docs/design/253-temporal-target.md:277-280`, the design's own limit of claim 1).

The accurate description of the determinism protection is: the refusal happens when emitting, not in the type checker; and the protection is (a) the existing rule that boundary crossings are legal only in emission position, (b) the closed allowlist of step and expression kinds, and (c) a fail-closed test over the pure-builtin table.

## Contributions

1. A target-variant seam, `revl emit --backend typescript --target temporal`, orthogonal to backend, that leaves the default native output byte-identical (`src/revl/cli/parser.py:2573-2594`; `docs/commands-reference.md:1905-1920`; `backends/typescript/test_temporal_target.py:400`).
2. Write-ahead derived saga: the compensation is pushed before the forward `await`, drained in reverse order with continue-on-error collection and a workflow-side budget, then a residue sink (`backends/typescript/emit_temporal.py:577-742`; tests at `backends/typescript/test_temporal_target.py:100`, `:199`, `:213`, `:240`).
3. A closed-allowlist refusal that names the construct and line, which closes the gap where a human approval gate could be silently dropped (`backends/typescript/emit_temporal.py:124-146`, `backends/typescript/emit_temporal.py:273-318`; tests `backends/typescript/test_temporal_target.py:134`, `:152`, `:171`).
4. Evidence-derived retry policy that deliberately reads the forward-side facts and not the folded register, with the bug class documented and tested (`backends/typescript/emit_temporal.py:442-514`; tests `backends/typescript/test_temporal_target.py:497-680`).
5. A live exit test that runs the emitter's own output on a Temporal dev server and measures derived LIFO order, a retried keyed activity and a never-retried undeclared activity (`tests/test_temporal_exit_saga.py:114-160`; `tests/temporal_exit/saga.revl`; CI job `.github/workflows/ci.yml:2473-2518`).

## Related work

| prior system or line of work | what it does | what revl does differently |
|---|---|---|
| Temporal (workflow-as-code; see the [related-work table](../related-work.md)) | Its workflows replay recorded history and use a deterministic TypeScript sandbox; external I/O runs in Activities | The split and the compensation order are derived from checked declarations; nonconforming constructs are refused before any run |
| Temporal TypeScript SDK workflow bundle and sandbox (see the [related-work table](../related-work.md)) | Isolates workflow code and restricts nondeterministic APIs | revl refuses at emit time from IR node kinds, so there is no path to render inline IO |
| AWS Step Functions (Catch and compensation states) | Compensation is a hand-drawn state machine | Order derived from source order |
| Azure Durable Functions | Orchestrator determinism constraints documented; an analyzer exists for some rules (citation to be checked) | Same: derived, not linted |
| Restate | Durable execution with journaled side effects in handler code | revl is a source-to-source generator for an external engine; it does not provide the journal |
| Saga pattern libraries (Axon, Eventuate Tram, MassTransit sagas) (citation to be checked) | Libraries for orchestrated compensation | Order and idempotence evidence come from the language, not the library user |
| Temporal workflow replay and testing tools (see the [related-work table](../related-work.md)) | Detect or replay-test non-determinism | Different point in the pipeline; revl's is conservative by closed allowlist |
| Long-running transactions and sagas (Garcia-Molina and Salem 1987) (citation to be checked) | Original saga model | Gives the vocabulary; revl's contribution is derivation from a compiler-checked effect system |

## Evidence on main

| claim | path:line or test | status | note |
|---|---|---|---|
| `--target temporal` exists as an emitter rendering, default output unchanged | `src/revl/cli/parser.py:2573-2594`; `backends/typescript/test_temporal_target.py:400`, `:410` | shipped | |
| Component activation to workflow, crossing to activity, golden frozen | `backends/typescript/emit_temporal.py:577-742`; `backends/typescript/golden/temporal_booktrip.ts` (134 lines); test at `backends/typescript/test_temporal_target.py:83` | shipped | |
| Write-ahead registration before forward await | `backends/typescript/emit_temporal.py:636-672` (pushes at 636-640 for `emit ... compensate` and 651-668 for `effect ... undo`); test `test_compensation_registered_write_ahead_before_forward_await` at `backends/typescript/test_temporal_target.py:100` | shipped (text-level) | The test checks ordering in emitted text only. Lines 604-618 are the `register_reachable` helper, not the push |
| LIFO drain, continue-on-error collection, workflow-side budget, per-call timeout | `backends/typescript/emit_temporal.py:692-720`; tests `backends/typescript/test_temporal_target.py:199`, `:213` | shipped | |
| Derived LIFO order on a real server | `tests/test_temporal_exit_saga.py:136` | shipped; CI job `temporal-exit` exists but is gated off pull requests (`.github/workflows/ci.yml:2473-2478`, `if: github.event_name != 'pull_request'`) | The job was not run for this draft and no CI result was inspected. No pull-request result exists for this job |
| Residue envelope on workflow failure, query handler, redaction of arg text | `backends/typescript/emit_temporal.py:690-742`, `backends/typescript/emit_temporal.py:808-930`; tests `backends/typescript/test_temporal_target.py:240`, `:252`, `:273`; live `tests/test_temporal_exit_saga.py:125` | shipped | |
| Closed allowlist refusal of approval, `witnessed[fs]`, spawn, host builtin | `backends/typescript/emit_temporal.py:124-146`, `backends/typescript/emit_temporal.py:188-318`; tests `backends/typescript/test_temporal_target.py:134`, `:152`, `:171` | shipped | Covers step and expression kinds in activation bodies only (`_walk_steps`, `backends/typescript/emit_temporal.py:288-318`) |
| Realms, hot swap, reactive withdrawal, leases refused with why-trace | claimed in `backends/typescript/emit_temporal.py:27-28` and `docs/design/253-temporal-target.md:445-459` | not evidenced | The refusal code walks the activation body only (`backends/typescript/emit_temporal.py:273-283`); no test and no check on component-level provides, isolate or reactive declarations was found. The module header says these are refused and the code does not check them. Spawn is tested; the others are not. The refusal is not claimed |
| Evidence-derived retry: only `keyed` earns retry, bounded | `backends/typescript/emit_temporal.py:442-514` (`_retry_class` at 480); tests `backends/typescript/test_temporal_target.py:497`, `:508`, `:521`, `:542`, `:564`, `:599`, `:642`, `:662` | shipped | A crossing declared `validated` is pinned to at-most-once even when keyed (`_retry_class`; test `:599`). The rule is "keyed and not validated" |
| Live proof keyed is retried, undeclared is not | `tests/test_temporal_exit_saga.py:148`, `:156` | shipped | `settle` attempts equal `[1, 2]` |
| Determinism guard over pure builtins | `backends/typescript/test_temporal_target.py:437` | shipped | guards the builtin table only |
| Injected fault where the forward effect committed but the activity reported failure, asserting compensation | design requires it (`docs/design/253-temporal-target.md:410-415`); `tests/temporal_exit/saga.revl` fault is an always-failing `boom.trigger` | designed, not built in the live test | The write-ahead property is the headline correctness fix and it is only tested at text level |
| Query as pure fn with proven `readOnlyHint`; signal as provided operation | `docs/signals-and-queries.md:39-100` | shipped for the MCP bridge, not part of this emitter | The Temporal emitter does not emit signal handlers; it refuses constructs that would need one (`backends/typescript/emit_temporal.py:271-283`, approval). The only Temporal query it emits is a residue query (`backends/typescript/emit_temporal.py:699`) |
| Signals, timers, approval as signal, workflow versioning, Python SDK | `docs/design/253-temporal-target.md:455-460`, `docs/design/253-temporal-target.md:665-677` | not built | explicitly deferred |

## Numbers that exist

- 26 test functions in `backends/typescript/test_temporal_target.py` (681 lines) and 5 in `tests/test_temporal_exit_saga.py`, counted by pattern, not by running them.
- Emitter size: 930 lines (`backends/typescript/emit_temporal.py`). Golden: 134 lines.
- Retry policy emitted for the keyed class: bounded to 5 attempts, 1 second initial interval, backoff coefficient 2, 30 second maximum interval (`backends/typescript/emit_temporal.py:119-122`; asserted at test `:497-519`). These are constants, not tuned values.
- Live run: `settle` attempts were `[1, 2]`; `worldRemaining` equals 2 on abort (`tests/test_temporal_exit_saga.py:125-160`). One saga, one run, no repetition counts.
- Workflow-side compensation budget constant 5000 ms and per-call timeout "1 minute" (`backends/typescript/emit_temporal.py:93-97`).
- No performance, overhead, or user data.

## Paper outline

1. Motivation: the three hand-written failure points in a Temporal saga.
2. Mapping: component, effect, emission, fault to workflow, activity, compensation, failure (`docs/design/253-temporal-target.md:117-135`).
3. What is derived: split, LIFO order, write-ahead registration, retry class.
4. What is refused and why: the closed allowlist and its why-trace.
5. Demo: the BookTrip saga, golden output, live run with injected fault.
6. Limits: code generation only; no durability claim; no replay claim.
7. Related work and future work (signals, timers, versioning, host-affinity queues).

## Threats to validity and limits

- The design says it plainly: "It does not provide durable execution, and it must not be sold as if it did" (`docs/design/253-temporal-target.md:424-426`). revl does not verify that a running cluster honored the shape, and cannot observe replay determinism.
- Claim 1 (no IO in workflow position) shows the source is clean, not that Temporal's replay will accept it (`docs/design/253-temporal-target.md:277-280`).
- The live evidence is one saga, one fault pattern, one SDK version, run through a dev server.
- Sequential awaits only; the design notes that concurrent emissions would need the dependency carried through (`docs/design/253-temporal-target.md:659-662`).
- `declared` idempotency is never promoted to a retry; that is conservative and can leave retryable operations at-most-once.
- `Int` emits as a TS bigint, which Temporal's default payload converter cannot serialize; the live saga uses `Str` everywhere for that reason (`tests/temporal_exit/saga.revl`, closing comment). A real user needs a custom data converter.
- Host-pinned inverses (`witnessed[fs]`) are refused, so file-local sagas are out of scope until host-affinity queues exist.
- The Python SDK target (slice 3) is not built.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 14 checked, 8 corrected.
