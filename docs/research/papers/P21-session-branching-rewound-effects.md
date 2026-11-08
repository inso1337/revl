---
id: P21
title: "Branching Agent Sessions With Rewound Side Effects: Scope-Gated Rewind and a Frozen Parent"
theme: effects-replay
kind: full
maturity: partial
status: A serial, single-workspace fork with offline lineage tooling is shipped on the Python runtime with unit and integration tests; parallel copy-on-write branches, live branch re-execution and the language fork form are not built, and no fork evaluation exists; the committed AgentDojo pilot exercises the admitted-turn path, not branching.
fused_with: P18, P19, P20
---

## Pitch

Forking an agent session at step k puts the host-confined state back at step k, refuses to fire any inverse that could leave the machine, freezes the parent, and prints what the log shows already escaped, so exploring a branch leaves no external residue beyond what the fork names.

## Abstract

Agent frameworks offer "checkpoint and resume" or "backtrack", but a rewind that replays compensations is itself a side effect: undoing a sent email sends a second one. We describe session branching in revl, a language and runtime whose effects are logged on a step-indexed timeline with declared inverses. A fork is a two-step, hash-bound operation. Step one walks every step above k into a partition that is total over the seven logged step kinds (restored, dropped before crossing, crossed and uncompensated, crossed and compensated, would cross on rewind, unrestored, provably empty) and returns a hash. Step two re-derives the hash, runs a rewind that executes an inverse only when its logged capability scope is host-confined, drops the parent's held outbound sends, freezes the parent, and mints a branch with a fresh session id, a fresh write-ahead log and no inherited approvals. The decisive design point came from an adversarial review: gating the rewind on step kind was unsound, because a declared-non-emitting inverse is an opaque host body, so the gate was moved to the logged scope, and an absent scope now reads as crossing. An offline reader (`revl branch`, `revl compare`) reproduces the live partition bucket for bucket from the WAL alone. The shipped fork is serial: one live branch over one workspace. The repository carries 17 tests in the fork test module (partition, rewind and fork protocol), 32 offline lineage tests and a golden for the report shape. We state what the fork does not preserve (seeds, clock, provider versions) instead of claiming it.

## Problem and claim

Tree-search agents, human "what if I had answered differently" workflows and incident post-mortems all want to resume from an earlier point of a session. Existing mechanisms treat the earlier point as a state snapshot. That is sound for a pure in-memory state and wrong for a session whose steps touched files, services and outboxes: the snapshot does not know which of the later steps can be undone, and a naive rewind that runs every declared undo will fire compensations (a refund, a cancellation, an offsetting call), which are new boundary crossings made with no commit and no prompt. The design doc names this the critical defect of its own first draft (`docs/design/250-session-branching.md:14-30`, `docs/design/250-session-branching.md:548-568`).

The claim of the paper is narrow and checkable. A fork at step k may say exactly one thing: the host-confined witnessed state and the provisions above step k are back at their step-k content; everything else is printed, not pretended away (`docs/design/250-session-branching.md:72-85`). To keep that sentence true, the rewind is gated on the capability scope the runtime logs per step, not on the step kind, the partition covers every kind including ones added later, and the parent is frozen because the rewind mutates a workspace the parent would otherwise keep writing to.

We do not claim a general time machine. The shipped primitive is serial (one live branch at a time), python-runtime only, and refuses forks it cannot make honest: an opaque step in the tail, a non-idempotent inverse in the span, a boundary already flushed below k, a second concurrent fork (`docs/design/250-session-branching.md:635-680`).

## Contributions

1. A fork protocol with enumerate-then-confirm semantics, hash-bound to the rewound span, the would-cross set and the live composition; a stale hash returns a fresh report, not a rewind (`Session.fork`, `Session.fork_confirm`, `src/revl/mcp/session.py:4182`, `src/revl/mcp/session.py:4345`; `test_fork_confirm_refuses_a_stale_hash`).
2. A scope-gated, non-emitting rewind mode (`step_back(compensate=False)`) that runs an inverse only if its logged capability scope is provably host-confined, treats an absent or unknown scope as crossing, and never fires a compensation (`backends/python/replay.py:1087-1110`; `docs/design/250-session-branching.md:207-273`).
3. A partition of the tail that is total over the seven step kinds, with an `unrestored` fall-through bucket so a future kind cannot silently drop out, pinned by a test that walks every kind into exactly one bucket (`test_every_kind_lands_in_exactly_one_bucket_or_empty`, `tests/test_session_fork.py:191`).
4. Branch isolation: parent frozen and non-callable, fresh deferral queue, fresh session id so a standing approval does not carry, distinct WAL, and a durable `fork-begin`/`fork-complete`/`fork-frozen` bracket written before the rewind and after the branch snapshot (`docs/design/250-session-branching.md:410-497`; `src/revl/mcp/session.py:1099-1107`, `src/revl/mcp/session.py:4388-4467`). The crash rule (a mid-fork crash collapses to the parent rolled back) is designed in Decision 5, but no test completes that rollback; the offline reader only reports the window (`tests/test_250_branch_slice2.py:296`).
5. An offline lineage and comparison surface over durable WALs (`revl branch`, `revl compare`) whose classifier reproduces the live partition, with the edges it cannot close named as orphans instead of guessed (`src/revl/branch.py`; `test_the_offline_partition_reproduces_the_live_one`, `tests/test_250_branch_slice2.py:119`).
6. An explicit list of what a branch does not preserve, written into the branch WAL's first entry (`_branch_provenance`, `src/revl/mcp/session.py:4279-4309`).

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Git branches and worktrees | Branch a file tree cheaply; nothing about external effects | The thing branched is a running session; the fork reports which effects already left the machine |
| Database MVCC and snapshot isolation | Branch readers over versioned data; writes are in-engine and rollback-able | Effects are arbitrary host calls; only those with a host-confined logged scope are rewound |
| Sagas and compensating transactions (Garcia-Molina and Salem 1987) (citation to be checked) | Undo a long transaction by running compensations | A fork refuses to run compensations during the rewind; it lists them as "would cross on rewind" and says compensation is not inversion (`docs/design/250-session-branching.md:312-317`) |
| CRIU, VM and container snapshots | Restore whole process state | Restores nothing opaque; a step that cannot be restored either blocks the fork or lands in `unrestored` |
| Copy-on-write filesystems (Btrfs, APFS clonefile) | Cheap divergent file trees | The shipped fork rewinds one shared workspace in place and freezes the parent; CoW branches are explicitly deferred (`docs/design/250-session-branching.md:825-828`) |
| Tree-of-Thoughts, LATS and other agent backtracking (citation to be checked) | Search over model outputs, usually with side-effect-free tools | Addresses tool calls with side effects, which those search methods assume away |
| Replit and Cursor style checkpoints (citation to be checked, product docs) | Restore files to a prior checkpoint | Also covers provisions and held sends, reports crossed sends, and binds the confirm to a hash |
| Time-travel debuggers (rr, Pernosco) (citation to be checked) | Replay an execution deterministically | A branch is a new live continuation, not a replay; seeds, clock and model decisions are not preserved and are listed as such |

## Evidence on main

| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Two-step fork exposed as MCP verbs | `src/revl/mcp/server.py:4707`, `src/revl/mcp/server.py:4734`; `docs/mcp-reference.md:1561-1600` | shipped | python runtime only |
| Fork enumerates, confirm performs and re-derives hash | `src/revl/mcp/session.py:4182`, `src/revl/mcp/session.py:4345`; `tests/test_session_fork.py:412` | shipped | |
| Rewind gated on logged scope; compensations never fired | `backends/python/replay.py:1087-1110`; `tests/test_session_fork.py:88`, `tests/test_session_fork.py:115`, `tests/test_session_fork.py:129`, `tests/test_session_fork.py:269` | shipped | absent scope reads as crossing since item 872 (`docs/design/250-session-branching.md:243-262`) |
| Partition total over seven kinds | `docs/design/250-session-branching.md:275-357`; `tests/test_session_fork.py:152`, `tests/test_session_fork.py:164`, `tests/test_session_fork.py:175`, `tests/test_session_fork.py:191` | shipped | |
| Pinned report shape | `test_compensate_false_report_matches_the_golden`, `tests/test_session_fork.py:239` | shipped | |
| Parent frozen, non-callable | `src/revl/mcp/session.py:1099-1107`; `tests/test_session_fork.py:341` | shipped | |
| Fresh session id, no approval carry; spend and review bind carried | `src/revl/mcp/session.py:4395-4435` | shipped | carrying spent budget is a deliberate narrowing, see code comment |
| Deferred sends dropped, not flushed | `tests/test_session_fork.py:370` | shipped | uses the earlier commit-deferral machinery |
| Refusals: opaque tail, non-idempotent span | `tests/test_session_fork.py:428`, `tests/test_session_fork.py:447` | shipped | boundary-flushed-below-k and concurrent-fork refusals are in code and docs; no named test found for them in `tests/test_session_fork.py` |
| Durable lineage, offline `revl branch` / `revl compare` | `src/revl/branch.py`; `tests/test_250_branch_slice2.py` (32 tests) | shipped | includes refusal to compare unrelated WALs (line 374) |
| Offline partition equals live partition | `tests/test_250_branch_slice2.py:119` | shipped | |
| Model decision made durable per completion | `tests/test_250_model_decision_wal.py` (17 tests) | shipped | keeps model, usage, latency, attempts; no prompt text, no digest |
| Offline replay-mode readiness | `tests/test_250_replay_modes.py` (13 tests); `docs/design/250-slice3b-replay-modes.md` | partial | no mode is executable offline; the live executor is deferred |
| Parallel CoW branches | `docs/design/250-session-branching.md:825-828` | designed | refused today |
| Language `fork` form | `docs/design/250-session-branching.md:829-835` | designed | deferred by the doc |
| Roadmap text "branch preserves seeds, clock, inverse witnesses" | `docs/v2.0-roadmap.md:1407` (inside item 250, whose header at `docs/v2.0-roadmap.md:1390` reads "SLICE 1 LANDED", not "proposed") | contradicted | the branch lineage lists seeds and clock, provider versions and model decisions as NOT preserved (`src/revl/mcp/session.py:4279-4309`) |

## Numbers that exist

- 17 tests in `tests/test_session_fork.py`, 32 in `tests/test_250_branch_slice2.py`, 17 in `tests/test_250_model_decision_wal.py`, 13 in `tests/test_250_replay_modes.py` (counted by `rg -c "^def test"` on the tree; not run for this draft).
- Seven logged step kinds in the partition (`docs/design/250-session-branching.md:275-282`).
- Four findings from the adversarial review of the first draft: one critical, two high, one medium (`docs/design/250-session-branching.md:14-17`).
- No latency, size or fork-cost numbers exist. No user study. No agent-benchmark result using forks. The AgentDojo pilot in `bench/results/agentdojo/pilot-banking/` exercises admission only, not branching.

## Paper outline

1. Introduction: why rewinding side effects is not free; the sent-email example; claim and non-claims.
2. Background: the step-indexed timeline, declared inverses, deferred sends and the commit boundary.
3. Fork protocol: enumerate, hash, confirm; refusals.
4. Scope-gated rewind: why kind-gating was unsound, the review story, fail-closed on absent scope.
5. A total honesty partition: the seven kinds and the report shape.
6. Isolation and recovery: frozen parent, fresh queue and approvals, WAL bracket, crash mid-fork.
7. Offline lineage and comparison: `revl branch`, `revl compare`, live/offline agreement.
8. Evaluation: partition agreement against an independent oracle, a naive-fork baseline, an agent case study.
9. Limits: serial only, python only, what is not preserved.
10. Related work and conclusion.

## Threats to validity and limits

- The rewind's soundness rests on the declared capability scope. The design doc states that revl cannot see what an opaque host body does at runtime, and that the guarantee makes no claim that the earlier non-emission analysis guarantees runtime non-emission (`docs/design/250-session-branching.md:267-273`).
- A branch preserves composition, generation, IR and source digests, capability surface and WAL position. It does not preserve provider versions, seeds and clock, or model decisions, and the model-decision entry excludes prompt and response text (`docs/design/250-session-branching.md:742-812`; `src/revl/mcp/session.py:4279-4309`). The roadmap text that says otherwise is out of date.
- The fork freezes the parent. This is a design limitation, not a feature: the doc defers copy-on-write precisely because the shared workspace has one owner.
- Exploring N branches is serial; the "zero residue until one is chosen" claim is about external sends held in each branch's own queue, not about divergent file state.
- Non-idempotent rewound spans are refused, and that refusal is what makes a mid-fork crash safe (Decision 5). The doc lists non-idempotent spans as an open item (`docs/design/250-session-branching.md:496`, `docs/design/250-session-branching.md:844`). The mid-fork recovery itself has no end-to-end test.
- Evidence so far is unit and integration tests written by the project team; there is no independent oracle and no external user.
- The completeness of the crossed set is inherited from the exhaustiveness argument of an earlier roadmap item (245); the fork does not prove it (`docs/design/250-session-branching.md:609-623`).

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 16 checked, 8 corrected.
