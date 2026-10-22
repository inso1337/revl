---
id: P18
title: "Time Travel over Typed Effects: Backwards Replay, Forward Replay and Execution Bisect"
theme: effects-replay
kind: full
maturity: partial
status: "Step-back, forward replay, bisect and the MCP verbs are shipped on the Python tier with 49 tests; no overhead or user-study data, and inverse correctness never reaches the bisect report."
fused_with: P19, P20, P21
---

## Pitch
A language that requires every effect to declare an inverse, or be classed as an emission, already holds the data for a reverse debugger. The checker verifies an inverse's presence and shape, not its correctness, and the honest version of that debugger makes one claim: the inverses ran newest first, and nothing about state being restored.

## Abstract
Reverse debuggers usually capture every memory write or require an invertible language. We observe that a runtime with inverse-or-emit effects already keeps, per live component, an ordered accumulator of registered inverses and a compiler-derived classification of which steps cannot be inverted. We build time travel on that accumulator in revl's Python reference tier: a captured timeline of effects, provisions, emissions, compensations and boundaries; step-back, which runs the registered inverses newest first down to a chosen step while leaving the component alive; forward replay, which re-invokes the provided-service calls that produced the tail; and bisect, a binary search over the timeline for the first step at which a predicate flips, in at most ceil(log2 N) + 2 evaluations (the test checks this on one 32-insert timeline). Each inverse is wrapped in a single-flight cell shared with the runtime's own teardown, so an early step-back and a later unload run each inverse once and unload still reports no residue. The system states one guarantee, a `guarantee` field carried in its step-back, timeline and bisect reports, and refuses by default to unwind across an uncompensated emission. We state what it cannot promise: an inverse is not an undo of the world, compensation is not inversion, forward replay re-runs rather than restores, and bisect trusts the registered inverses and labels its answer unverified. The engine has 49 tests, 13 of them against the real runtime. We discuss the design, the refusals, and the gap between a replay viewport and a state-restoration claim.

## Problem and claim
Stepping back normally costs a full trace or an invertible language. Debuggers that offer reverse execution rarely say what they do not restore, and tools that undo effects on a live system risk reporting success while a committed external write still stands.

The claim has two parts. First, a runtime whose effects register inverses in a LIFO accumulator (G4, G7 in the docs) gets partial unwinding, inspection of the unwound prefix, and a log-time search for free, without changing emitted code. Second, the useful contribution is the discipline: a two-sentence guarantee, a refusal that moves irreversibility from a footnote to a decision, and a bisect report that carries its own trust bound.

This is not a claim that stepping back restores program state, and not a claim about the other five tiers.

## Contributions
1. Step-back over the inverse accumulator newest first, leaving the component live, with a single-flight cell per inverse shared with teardown so unload after step-back remains residue-free (`docs/replay.md:61-64,82-91`; `test_real_cordis_step_back_then_unload_still_leaves_no_residue`, `tests/test_replay.py:929`).
2. A two-sentence guarantee, `replay.GUARANTEE`, returned in the `guarantee` field of reports, with a test that no step-back report key contains "restor" (`backends/python/replay.py:256-260`; `tests/test_replay.py:697`).
3. A refusal rule: step-back refuses by default when the range contains an uncompensated emission, refuses totally (nothing runs), and `force` is the explicit override; compensation is reported as its own step kind and appends a new emission step (`docs/replay.md:130-177`; `tests/test_replay.py:445,474,946,963`).
4. Forward replay that re-invokes provided-service calls and reports activation-body steps as `notReplayable` instead of faking them (`docs/replay.md:179-202`; `tests/test_replay.py:522,540`).
5. Execution bisect: a git-bisect-style search for the first step at which a predicate over the `inspect` view flips, with a report that carries a verified or unverified status for the effects on the path (`backends/python/replay.py:853-1000`; `tests/test_replay.py:585-683`; `docs/replay.md:297-349`).
6. Trace capture that the docs say leaves emitted output byte-identical (the golden files are cited, not opened here): a wrapper around each component's `apply` passes a delegating context (`docs/replay.md:215-232`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| rr (O'Callahan et al., USENIX ATC 2017) (citation to be checked) and Pernosco | Whole-process execution capture and replay at the machine level, with reverse execution | Does not capture execution; reuses the effect accumulator the runtime keeps for teardown. Reaches only declared effects, not arbitrary memory |
| GDB reverse debugging, omniscient debugging (Lewis, 2003) (citation to be checked) | Reverse stepping from a trace of state changes | No memory trace; the unit is a typed effect with a declared inverse, and the system states it does not know whether the inverse restores state |
| Elm time-travel debugger, Redux DevTools | Replays a pure message history over a pure update function | Effects are not pure; the engine refuses to cross irreversible emissions and separates compensation from inversion |
| git bisect | Binary search over commits for the first bad one, assuming monotone good-to-bad | Same search over a captured execution timeline, with the monotone assumption stated and an unverified label on the answer |
| Saga pattern and compensating transactions (Garcia-Molina and Salem, 1987) (citation to be checked) | Compensation as a second action offsetting a first | Keeps compensation distinct from inversion in the type of step and in the report, and appends it to the timeline as an emission |
| Algebraic effect handlers (Eff, Koka; see the [related-work table](../related-work.md)) | Control over effect operations, including resumption and rollback in some handlers | Not a handler library; the inverse-or-emit rule is enforced by the compiler across a composition, and the replay engine is an inspector over the runtime's accumulator |

## Evidence on main
Tree: revl main at commit 45bec431f.

| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Timeline, `inspect`, `step_back`, `replay_forward` operations | `docs/replay.md:54-66`; `backends/python/replay.py:825` (`inspect`), `backends/python/replay.py:1087` (`step_back`); `src/revl/mcp/session.py:9209` (`bisect`), `src/revl/mcp/session.py:9245` (`replay_forward`; `backends/python/replay.py` has no such method) | shipped | Python tier only |
| Two-sentence guarantee, no "restor" report key | `backends/python/replay.py:256-260`; `tests/test_replay.py:697` | shipped | Checks report keys, not values |
| LIFO order and once-only shared with teardown | `docs/replay.md:82-91`; `tests/test_replay.py:929` | shipped | The "sharpest result" per the doc (`docs/replay.md:380-383`); needs real cordis-py, so skipped without it |
| Refusal across uncompensated emission; forced override | `tests/test_replay.py:445,946` | shipped | |
| Compensation appends an emission, never reported as inverse | `tests/test_replay.py:474,963` | shipped | Classified by source adjacency of lines (`docs/replay.md:240-247`), a stated emitter-shape assumption |
| Forward replay re-runs service calls; activation-body steps `notReplayable` | `tests/test_replay.py:522,540,556,1003` | shipped | |
| Bisect in at most ceil(log2 N) + 2 evaluations | `test_bisect_finds_the_flip_in_log2_evaluations` (`tests/test_replay.py:585`) | shipped | One 32-insert timeline (`tests/test_replay.py:585-612`); the bound is a test on one N, not a general result |
| Bisect edge cases (never flips, flips at 0, empty, bad predicate, no mutation) | `tests/test_replay.py:648-683` | shipped | |
| MCP verbs `revl_timeline`, `revl_inspect_step`, `revl_step_back`, `revl_replay_forward`, `revl_replay_bisect` | `src/revl/mcp/server.py:5280-5372`; `docs/mcp-bridge.md:549-564`; `tests/test_replay.py:1043` | shipped | `revl_step_back` with no arguments is a separate session-undo (`docs/replay.md:270`) |
| CLI trace capture with `:timeline`, `:back`, `:bisect` | `docs/replay.md:284-295`; `src/revl/cli/parser.py:1587` (the capture flag) | shipped (docs) | The `:timeline`, `:back` and `:bisect` verbs were not opened |
| Capture leaves emitted output unchanged | `docs/replay.md:217-219` | documented | Golden files are the stated evidence; not opened |
| Timers are revertible coeffects (`every`/`after` acquire a schedule whose inverse is cancellation) | `docs/time-coeffect.md:30-69`; `test_unload_cancels_an_every_timer_no_residue_no_orphan_firing` (`tests/test_time_coeffect.py:219`) | shipped | Timers run on py, ts, go and rust; java and wasm refuse `timer` (`docs/time-coeffect.md:239-240` Scope, `:270-277` wasm) |
| Clock is a harness-advanced coeffect | `docs/time-coeffect.md:93-115` | shipped | `backends/python/replay.py` has no reference to the clock or timers, so replay and the clock are two mechanisms; their integration is not shown |
| Random as a declared coeffect for replay | none found | not found | Randomness in `src/revl/fault.py` is generator seeding for tests, not a replay coeffect |
| Bisect reports verified status from `verified effect` markers | `backends/python/replay.py:974-998` | partial | `backends/python/replay.py:874-875`, `:978-979` and `src/revl/mcp/server.py:5353-5354` say `verified effect` "is not built", which is stale: `src/revl/fault.py:1550-1600` and `run_roundtrip_units` (`src/revl/fault.py:2208`) build the round-trip runner (9 tests in `tests/test_verified_method_effect_1897.py`). The gap is that no marker reaches replay steps; `backends/python/replay.py` reads `verified` only at `:984`, so status is always `unverified` |
| Multi-generation replay across `revl_swap`; other tiers | `docs/replay.md:407-412` | not shipped | Stated as uncovered |

## Numbers that exist
- `tests/test_replay.py`: 49 test functions (`rg -c "^def test"`), 13 `@needs_cordis` decorators, each on a real-runtime test (`tests/test_replay.py:897-1113`). The 14th textual match is in the module docstring at line 13. This matches the doc's split of 36 stub tests and 13 real-runtime tests (`docs/replay.md:355-368`).
- Bisect bound: at most ceil(log2 N) + 2 evaluations, tested on a timeline built by `_notes_timeline(32)` (`tests/test_replay.py:585-612`). No larger N and no timing.
- `tests/test_time_coeffect.py`: 54 test functions. Not run.
- No measurements of trace-capture overhead, timeline size, or bisect speedup on real compositions.

## Paper outline
1. Introduction: reverse execution without a trace, and the cost of overclaiming restoration.
2. Background: inverse-or-emit effects, the LIFO accumulator, compensation (A5).
3. Model: step kinds, timeline, the four operations.
4. The guarantee and its refusals: the two sentences, force, compensation versus inversion.
5. Forward replay and what it cannot re-enter.
6. Bisect: predicate scope, complexity, monotonicity, unverified status.
7. Trace capture without changing emitted code; classification assumptions.
8. Evaluation: overhead, scale, case study or user study (to be run).
9. Related work.
10. Limits and the WAL-based, cross-process half (see P19).

## Threats to validity and limits
- The docs state the central bound: "stepping back to step `k` does not put the program in the state it was in at step `k`" (`docs/replay.md:107-111`). Under correct inverses the two coincide; the runtime never checks.
- Not caught: a wrong or partial `undo`, state reachable through a handed-out reference, anything outside the process, and values derived from a clock (`docs/replay.md:113-119`).
- Forward replay cannot re-enter activation-body steps; the remedy is reload (`docs/replay.md:186-202`).
- Trace capture is opt-in at load and not retroactive (`docs/replay.md:204-213`).
- Host `extern emission fn` calls do not appear as emission steps; their compensation shows up as an ordinary inverse (`docs/replay.md:253-258`).
- Compensation is identified by source adjacency of emitted lines, an assumption about emitter shape (`docs/replay.md:240-247`).
- Bisect assumes a monotone predicate and "still returns a flip boundary, but not provably the first" otherwise (`docs/replay.md:320-322`). It evaluates the predicate over `inspect(k)` reconstructed from the timeline (`backends/python/replay.py:869-880`), not over a re-executed program.
- Python tier only; the doc says nothing has been ported to the other five tiers (`docs/replay.md:410-412`).
- Doc statements this paper does not copy: `docs/replay.md:12` is headed "Why this exists here and nowhere else" (an unchecked comparative claim), and `docs/replay.md:299` says `step_back k` "restores any prefix", which contradicts `docs/replay.md:107-111`.
- The test named `test_real_cordis_step_back_restores_state_and_leaves_it_live` (`tests/test_replay.py:912`) checks one component after one step-back. Its name says "restores"; its assertions are cited, not its name, and it is not evidence of restoration in general.
- One cordis-py gap affects async bodies' failure state; the doc attributes it to the runtime, not to replay (`docs/replay.md:391-401`).
- The `revl replay` command (`src/revl/cli/parser.py:1924-1967`) reports replay-mode readiness over a durable write-ahead log and "runs nothing"; it is a different mechanism and is not claimed here.
- `backends/python/replay.py` also contains write-ahead log and recovery code beyond what `docs/replay.md` covers; this draft does not claim any of that.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 30 checked, 13 corrected.
