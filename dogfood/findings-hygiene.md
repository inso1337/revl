# findings — hygiene (agent/hygiene)

Roadmap items 72 (tier-naming/doc drift) + 73(a)(b)(c) (golden policy:
snapshot, not freeze) + 74(c)(d) (errata decisions: cordis-rs A1 promotion,
wasm trap payloads). A tooling-and-docs run — the only `.rvl` touched is
existing fixtures, so the refusal log is mostly empty by construction.

## 1. Refusal log

No `revl compile` rejections hit: this task changed tooling (placement
conductor, `run` gating) and docs, not language. The rejections I *did* meet
were test-harness and pre-commit ones, logged under friction/cost where they
belong. Verdict on the whole class: the checker never entered this loop, so
this run is a fair sample of "everything outside the language" — which is
exactly what items 72–74 are about.

## 2. Friction log

- `[slow]` **The pre-commit hook needs >60s and my first two commits died
  on the tool-call timeout**, not on the hook: the full `pytest tests/`
  (~65s) plus vitest plus the conformance sweep crossed the 60s default, the
  commit was killed mid-hook, and I had to re-run it with a 240s timeout.
  The hook itself is fine (it even survived its own docs: worktree venv
  fallback worked); the harness default just doesn't know the suite's real
  duration.
- `[slow]` **The placement fake-`Popen` seam intercepts more than placement
  children.** The swap-conductor tests stub `subprocess.Popen` to script the
  children; my first `ts`-alias test then blew up with `KeyError: 'name'`
  because the *TS emitter build step* (`_emit_ts_module` → `subprocess.run`)
  also goes through `Popen`, and it reads an IR json, not a placement spec.
  Two debug cycles before I stubbed `_emit_ts_module` too. A conductor that
  routed build steps through a named mockable seam would have made this
  obvious from the first failure.
- `[slow]` **The placement spec file lives in the conductor's mkdtemp,
  which is removed in `finally`.** My first assertion read the child's spec
  json *after* `run_placement` returned — `FileNotFoundError`, one cycle,
  then I captured the spec at spawn time on the scripted proc. The `_ScriptedProc`
  stand-in is fine; the fact that the spec path is a dead pointer post-run
  is not documented anywhere near the harness.
- `[nit]` **`pytest backends/python/tests/... tests/...` in one invocation
  fails collection** (backends/python/tests/conftest.py conflicts with the
  root suite), while each alone is green. tests/conftest.py's docstring says
  the backend suites "keep owning their own loader paths" — true, but the
  constraint is not visible at the shell, and I hit it trying to verify the
  python emitter quickly.
- `[nit]` **The roadmap has a maintenance convention I almost violated.**
  "How this list is maintained" says implementing sessions flip `✅` and
  append terse `Landed:` lines, they do not rewrite entries. I read it while
  deciding how to mark 72/73/74 — one sentence in the wrong order would have
  rewritten an entry instead of annotating it.
- `[nit]` **stability.md and the roadmap header still carried the freeze
  promise** ("a change that alters emitted v1 output by a single byte fails
  the suite and does not land"; "their goldens must never regenerate") while
  item 73's decision had been written — the doc drift the item itself names,
  found only by grepping every `golden` mention after writing the policy.

## 3. What revl gave you

- **The repo's own precedent for 73(c).** `tests/test_frontend.py`'s golden
  tests exist because a merge once left the rust golden one character stale
  (cfd096b: "only the python and typescript goldens had frontend coverage").
  That commit's lesson *is* the roadmap's "recurring CI gap" — folding the
  wasm `functions.wat` and reference-IR goldens into `pytest tests/` was
  completing the exact fix the repo had already half-built.
- **The code was ahead of the docs, so it was the oracle.** tools/conformance.py
  already had `TIERS = (…, "go")` and 51 cases while conformance.md said
  "50 cases × 5 emitters"; placement.py's `KNOWN_BACKENDS` and run.py's were
  each internally consistent. The drift was invisible until code and doc
  were read side by side — which is the differential-oracle rule applied to
  prose.
- **The errata's race-loop assertion made 74(c) a five-minute decision.**
  `backends/rust/scenarios/scenarios.rs` already asserted torn-state freedom
  under a concurrent-divert race loop ("never a `do:` without its `undo:`"),
  and the errata entry already named it. The roadmap said "prefer the
  spec-promotion reading; make it official or make it moot" — the code had
  already made it official; I transcribed it into a dated decision line.
- **The snapshot policy's own test suite proved the collapse was safe.**
  After collapsing `_string` to one escape path, the regenerated rust golden
  was byte-identical — the "unreviewed" change detector working as designed,
  this time as a *reviewed* non-change.

## 4. Time-to-green

- Compile→refuse→fix cycles: 0 language cycles. Test-debug cycles: ~6
  (fake-Popen KeyError ×2, spec-file dead-pointer ×1, pre-commit timeout ×2,
  combined-pytest collection ×1).
- Longest single stall: the fake-`Popen` KeyError — the placement test
  seam's blast radius was wider than its name suggested. Would have been
  zero with a build-step seam named as such (see §5).
- Final: `tests/test_swap.py` 9/9 (incl. the new ts-alias test),
  `tests/test_run.py` 9 passed 1 skipped, `tests/test_goldens.py` 5/5,
  `tests/test_frontend.py` 98/98, `backends/rust/test_emit_rust.py`
  golden + string-literal tests green, full suite in §"Final suite".

## 5. Cost ledger

- `tooling` ×1: pre-commit hook killed twice by the 60s tool-call timeout —
  a fixed cost of ~2 minutes plus two re-commit loops, cause: harness
  default vs suite duration, remedy: longer timeouts for commits in this
  repo. The hook's own runtime is not the problem.
- `tooling` ×2: the placement fake-`Popen` seam and the post-run spec-file
  dead pointer — one debug cycle each, cause: the conductor's build steps
  and the child spawns share one `Popen` surface and the spec path is
  documented nowhere as transient.
- `docs-gap` ×1: combined pytest invocation failed collection with no hint
  that backend suites must run from their own dir (the conftest conflict is
  real but silent). Cost: one false start.
- `spec-ambiguity` ×1: item 72's "tier-naming" lumped two different facts —
  placement's manifest *alias* problem and run.py's *gating order* problem.
  Both needed `KNOWN_BACKENDS` changes but for different reasons; separating
  them in my head (alias at the manifest edge vs. gating after argparse)
  took one re-read of the item text.
- **The single change that would have cut the most cost:** a named
  build-step seam in the placement conductor (build subprocesses routed
  through `_emit_ts_module`-style functions already, but the test harness
  couldn't see that boundary) — it would have removed the two longest stalls
  and made the ts-alias test write itself in one shot.
