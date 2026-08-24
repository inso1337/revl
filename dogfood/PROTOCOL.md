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

### 5. Cost ledger (why the run cost what it cost)
Time-to-green says how long; this says *where it went and why*. List every
wasted cycle — a cycle that a better diagnostic, doc, tool, or missing feature
would have made unnecessary — one line each, classified by cause:
`diagnostic` (message didn't say how to fix) | `docs-gap` (had to read source
to learn a documented-feeling fact) | `env` (venv/setup/vintage drift) |
`tooling` (slow loop, missing fast path, hard-to-find code) |
`missing-feature` (worked around a language/stdlib gap) |
`spec-ambiguity` (brief, spec, and code disagreed).
End with the single change that would have cut the most cost. Do NOT
self-estimate tokens — the orchestrator records measured token count and
duration per run from harness telemetry into `dogfood/COSTS.md`; your job is
the causal story those numbers need.

## Rules of engagement
- Work in your own worktree off current `origin/devwip`; never touch other
  worktrees.
- Tests: the repo-root `.venv` python with `PYTHONPATH=<worktree>/src`;
  cordis-gated execution tests need a `backends/python/.venv` (run
  `sh backends/python/setup.sh`, or borrow a sibling worktree's).
- Differential-oracle rule: when porting a compiler phase, the reference in
  `src/revl/` is ground truth. Byte-identical output or identical verdicts on
  the corpus, or it does not merge.
- Conventional commits matching the repo's log style. NO attribution trailers
  of any kind (no Co-Authored-By). Push your own `agent/*` branch; never push
  `devwip` or merge — the orchestrator reviews on a merge preview and merges.
- On API 429: pause, retry, never abandon with work uncommitted.
