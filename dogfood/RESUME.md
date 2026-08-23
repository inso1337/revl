# Dogfood dispatcher — resume brief

**Status 2026-08-23 (third pass, post limit-reset):** the loop is running.
While the dispatcher was rate-limited, parallel sessions moved origin/devwip
~54 commits (PRs #34–#38): they merged agent/extern-undo-check (bf306e7),
agent/fault-res (b0db71c — including the red gate test), run_00017's map
iteration (bc7cbc7, order decided: canonical-order), and run_00018's record
update (041ed18, item 69 flipped). Delete this file when the board is empty.

## Board

| Run | Task | Status |
|---|---|---|
| run_00016 | Fault-path residue asymmetry (item 68) | **origin/devwip's fault suite is RED** — the gate test `test_a_non_inverse_undo_fails_under_an_injected_fault` (eda5df3) was merged via PR #36 together with the defective b76224b, and it correctly fails there. Root cause found: off-by-one in `_inject`'s splice (src/revl/fault.py) — `fail at step N` replaced step N instead of following it, so at step 1 nothing ran and `assert no residue` was vacuous. Fix exists in `/Users/inso/revl-wt-faultres` (agent porting it onto the moved origin/devwip, incl. checking the fault-sweep path for the same off-by-one). Merge when the gate is green and the §8 divergence lock is reconciled. |
| run_00015 | Extern undo checking (item 67) | **LANDED** on origin via bf306e7 (the rebuilt `result: T` binding version, 6ad91f5). Item 67 flipped ✅. |
| run_00017 / run_00018 | Map iteration / record update | landed independently of this loop (bc7cbc7 / 041ed18); no dispatcher action left. |

## Session-transport facts

- Original dead-session transcript: `/private/tmp/rustprobe/1787370311519_ysn5v.json`
  (leaky counterexample = message 1246). Replayed and locked in as the gate test.
- Worktrees `revl-wt-faultres` / `revl-wt-undochk` belong to the
  `/Users/inso/revl-work` clone; canonical repo is `/Users/inso/Projects/revl`.

## Standing protocol (unchanged)

- Findings protocol per docs/dogfood; triage tiers as in the wave-6 pattern.
- Review = diff + live repro + **suite on a merge preview** before the merge
  commit; devwip → PR → CI.
- Roadmap conventions: dispatcher flips ✅ / appends `Landed:` and review
  notes with citations; item text belongs to design sessions.
- No AI attribution trailers on commits.
