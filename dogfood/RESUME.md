# Dogfood dispatcher — resume brief

**Why this file:** the dispatcher session (the "dogfood loop") hit its rate
limit on 2026-08-23 ~13:02 mid-review. This is the handoff so a fresh
dispatcher session continues from state, not from scratch. Delete or update
this file when the loop is current again.

## Where the loop stopped — the exact frame

Reviewing **run_00016** (fault-path residue asymmetry). The subagent's fix
was in review and is **defective**: it reads a host trace that nothing
feeds on the real execution path. The reviewer proved it by constructing
the leaky component from the original asymmetry repro and running it
through a fault test on the candidate tree — **the leaky component still
passes**. The loop died while "examining their implementation to find the
gap."

**First action of the resumed loop:** roadmap item 68. Turn the reviewer's
leaky-component construction into a regression test (red on the candidate
branch) *before* any more fixing. Do not merge run_00016's branch until
that test exists and goes green.

## Tier-3 dispatch board (as of loop death)

| Run | Task | Status | Now tracked as |
|---|---|---|---|
| run_00015 | Extern-level `undo <expr>` checking (arity, types, declared-callable, self-reference) + rejection corpus | dispatched, **never landed** | roadmap item 67 (soundness) |
| run_00016 | Fault-path residue asymmetry — which layer drops the accounting (lowering vs py adapter vs cordis-py) | fix in review, **counterexample open** | roadmap item 68 — priority |
| run_00017 | Map iteration/size/remove, spec-first, six tiers, order semantics | dispatched, **never landed** | item 52 (order decision gates it) |
| run_00018 | Record update `{r \| f = x}` + match-arm blocks, spec-first | dispatched, **never landed** | roadmap item 69 |

## What already landed (do not redo)

- Waves 5–6 merged (PRs #31–#33): selfhost checker slices 1–2 + oracle,
  uxprobe rounds, docs-gaps/diag-hints/porting-unblock batches,
  `Int.to_str` six tiers, conftest sys.path bootstrap, TS golden pinning,
  the `test_swap.py` `_stop` thread-flag fix.
- Independently since: A8 upstream fix confirmed (TCK pin retired,
  db240f6), string-unit probe run and decided — **code points** (677b572,
  item 51), de-risk probe verdicts recorded (2f7da1e), spawn phase 2 +
  wasm-to-zero claimed in-progress (b22269f), signals/queries mapping doc
  on a branch (b70c5e2).

## Standing protocol (unchanged)

- Findings protocol per docs/dogfood (findings files: refusals, friction,
  wins, time-to-green); triage tiers as in the wave-6 pattern.
- Review = diff + live repro + suite before merge; devwip → PR → CI.
- Roadmap conventions: see the "How this list is maintained" preamble in
  docs/v2.0-roadmap.md — the dispatcher flips ✅ and appends `Landed:`
  lines with citations; item text belongs to design sessions.
- No AI attribution trailers on commits.

## Session-transport note

The dead session's full transcript export:
`/private/tmp/rustprobe/1787370311519_ysn5v.json` (1253 messages). The two
stray `1787370311519_ysn5v.html` files (repo root, ~/Projects) are exports
of the same session — repo-root one is item 22 hygiene, delete it.
