# Dogfood cost ledger — measured per run

Maintained by the orchestrator from harness telemetry (token count, wall
duration, tool calls) at each run's completion; the causal story behind each
number lives in that run's `findings-<slug>.md` §5. Numbers are the run's
final reported totals — a run killed and resumed reports segments, marked ~.

| Run | Task | Tokens | Duration | Tool calls | Outcome | Cost notes |
|---|---|---|---|---|---|---|
| rebuild-00015 | item 67: extern undo `result` binding | ~112k | ~16 min active (3 segments) | ~96 | landed bf306e7 | rebase silently dropped upstream-reverted commits (cherry-pick recovery); rust golden's cargo build was the long tail |
| rebuild-00016 | item 68: fault splice off-by-one | ~199k+ (final segment; earlier segment lost to session limit) | ~5 min final segment | 31 | landed 37d2d80 | diagnosis segment killed by session limit mid-run; port over ~54-commit devwip drift cost a merge + re-audit of every step-index site |

Wave-8 runs append below.
