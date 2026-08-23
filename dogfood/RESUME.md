# Dogfood dispatcher — resume brief

**Status 2026-08-23 (second session):** the loop is running again. This file
now tracks the live board; delete it when the board below is empty.

## Board

| Run | Task | Status |
|---|---|---|
| run_00015 | Extern-level `undo <expr>` checking (item 67) | fix reviewed, **counterexample found, merge reverted** (ab239c5): the empty-scope rule breaks the landed WIT resource model. Design decision recorded in item 67 — `result: T` implicit binding. Rebuild dispatched on agent/extern-undo-check (`/Users/inso/revl-wt-undochk`). |
| run_00016 | Fault-path residue asymmetry (item 68) | reviewer's leaky construction is now a committed red regression test on agent/fault-res, eda5df3 (`/Users/inso/revl-wt-faultres`). Rebuild-the-fix dispatched against it. Second defect logged: b76224b flipped the §8 divergence lock. **Do not merge while either test is red/unreconciled.** |
| run_00017 | Map iteration/size/remove (item 52 implementation) | not re-dispatched — gated on item 52's order-semantics decision. |
| run_00018 | Record update `{r \| f = x}` + match-arm blocks (item 69) | not re-dispatched yet; spec-first per item 69. |

## Session-transport facts (from the dead session, kept until runs land)

- Dead session transcript: `/private/tmp/rustprobe/1787370311519_ysn5v.json`.
  The leaky counterexample is message 1246; run_00015's live verification
  frame is 1236. Both replayed and confirmed this session.
- The dead session's repo is `/Users/inso/revl-work` (devwip stale at
  e0fc683); canonical is `/Users/inso/Projects/revl` tracking origin/devwip.
  Worktrees `revl-wt-faultres` / `revl-wt-undochk` belong to revl-work.
- The two stray `1787370311519_ysn5v.html` exports were not found on disk —
  hygiene item moot.

## Standing protocol (unchanged)

- Findings protocol per docs/dogfood; triage tiers as in the wave-6 pattern.
- Review = diff + live repro + **suite on the merged tree** before push;
  devwip → PR → CI. (The run_00015 revert exists because the suite ran
  after the merge commit — run it on a merge preview first.)
- Roadmap conventions: dispatcher flips ✅ / appends `Landed:` and review
  notes with citations; item text belongs to design sessions.
- No AI attribution trailers on commits.
