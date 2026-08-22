# Dogfood protocol — agents building in revl

Every agent working in this loop is also a sensor: its frictions are the
language's bug list, its refusals are the checker's field data. Findings are
first-class deliverables, equal in weight to the code.

## Every agent MUST commit `dogfood/findings-<slug>.md` on its branch

Append continuously while working, not retrospectively at the end — memory
of friction decays fast. Required sections:

### 1. Refusal log (every `revl compile` rejection you hit)
For each: the snippet refused, the diagnostic verbatim, and a verdict —
- `caught-bug` — it was wrong and the checker was right (the dream; quote it)
- `friction` — correct but the message/hint didn't tell you how to fix it
- `false-positive` — you believe the refusal was wrong (highest value; include
  why, and what you wrote instead)
- `gap` — the language cannot express what you needed

### 2. Friction log (everything that slowed you down)
Missing stdlib, docs that lied or were silent, diagnostics you had to decode,
grammar awkwardness, tooling pain (PYTHONPATH, venv, error surfacing), places
you wanted to reach for mutation/loops/pattern-matching and couldn't. One line
each, severity-tagged `[blocker]|[slow]|[nit]`.

### 3. What revl gave you
Concrete moments where a guarantee caught something, where the type system
found your mistake, or where hot-swap/LIFO/no-residue did work you'd otherwise
hand-roll. Be specific; skepticism is more credible than praise.

### 4. Time-to-green
Count your compile→refuse→fix cycles. Note the longest single debugging stall
and what would have shortened it.

## Rules of engagement
- Work in your own worktree off `devwip`; never touch other worktrees.
- Tests: `/Users/inso/revl-work/.venv/bin/python -m pytest tests/ -q`; CLI via
  `PYTHONPATH=<worktree>/src`.
- Differential-oracle rule: when porting a compiler phase, the reference in
  `src/revl/` is ground truth. Byte-identical output or identical verdicts on
  the corpus, or it does not merge.
- Conventional commits ending `Co-Authored-By: Claude Opus 4.8
  <noreply@anthropic.com>`. Do NOT push; the orchestrator reviews and merges.
- On API 429: pause, retry, never abandon with work uncommitted.
