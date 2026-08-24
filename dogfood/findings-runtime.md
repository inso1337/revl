# Dogfood findings — runtime errata harvest (74a, 74b)

Agent: runtime-errata (agent/runtime-errata). Scope: two open upstream
runtime threads — cordis (TS) `assertActive` residue (74a) and cordis-py
dict-plugin `Config` (74b). No revl source was written: the work was
fork-and-pin of two runtimes plus an emitter simplification, so the refusal
log below is empty by construction.

## 1. Refusal log

None. No `revl compile` was ever invoked — this task lives entirely on the
runtime side (fork fixes, pins, one emitter simplification, docs). The
checker's role here was already settled: G5 is by-construction in revl
source (undo bodies have no syntactic slot for effects), which is exactly
why the runtime-side residue had to be fixed in the library instead. The
one refusal-shaped fact worth recording: revl's *own* suite refused to let
the emitter simplification land silently — see the replay regression in §2
(the checker couldn't catch it because it is a runtime behavior, but the
execution tests did).

## 2. Friction log

- `[slow]` **npm rewrites git dependencies to `git+ssh` in the lockfile.**
  Pinning the cordis fork via `github:...`/`git+https:...` both produced
  `"resolved": "git+ssh://git@github.com/..."` — pacote's hosted-git rule
  ("store ssh in resolved when no auth"). CI runs anonymous `npm ci`, so an
  ssh URL would break a fresh checkout. Had to read pacote's git.js to
  confirm, then switch the pin to the GitHub codeload tarball URL
  (`archive/<sha>.tar.gz`) — plain https, still SHA-pinned. A doc line in
  npm's docs would have saved the detour.
- `[slow]` **The pre-commit hook outlived my first commit attempt.** The
  hook runs the full `pytest tests/ -q` (~3.5 min). My first `git commit`
  used a 60 s tool timeout; the SIGTERM killed pytest mid-run and left a
  stray `tests/generated/revl_test_*.test.ts` (a `revl test --backend ts`
  temp file whose cleanup never ran) that read like a mystery artifact from
  my vitest runs. Cost: ~10 min of provenance archaeology before realizing
  the hook's own cleanup contract (`test_backend_ts_runs_vitest` asserts it)
  was violated by the kill, not by my changes.
- `[slow]` **Moving config resolution out of `apply` broke the replay
  harness — an integration point nothing announced.** After the 74b emitter
  change, `tests/test_replay.py` failed 9 tests with `KeyError: 'pool_size'`:
  `replay.py` calls the emitted `apply` directly (bypassing cordis-py's
  `resolve_config`), so the body suddenly received raw config without
  defaults. Found only by running the full suite; the backend's own 58 tests
  were green. A `grep` of direct `apply` callers should have been on the
  checklist for "resolution moved out of apply".
- `[slow]` **The `_pending_config` parking hazard.** Moving validation from
  "inside apply" to "at plugin() setup time" widens the global
  parking→Frame-flush window. Verifying it was safe required tracing the
  py fork's activation order (`_start_reload` awaits `asyncio.sleep(0)`
  before apply runs) and auditing every revl consumer (emitted lifecycle
  steps, demo.py, mcp session, live.py) for back-to-back config-carrying
  loads. All loads are separated by settle/flush awaits, so it holds — but
  the design is one global away from misattribution; a future "two
  config-carrying components plugged before flushing" host would silently
  swap `<name>.config` trace attribution.
- `[nit]` **Sandbox denied writes under `/tmp`** (`spawn sandbox-exec
  ENOENT`) — built the fork repo under the workspace instead.
- `[nit]` **`inso1337/cordis` did not exist** while the playbook's
  `inso1337/cordis-py` did; creating the fork repo was a one-liner
  (`gh repo create`) but the asymmetry wasn't documented anywhere.
- `[nit]` The py fork ships compiled `lib/` only in the npm sense: the TS
  fork needed lib+src kept in sync by hand (one-line fix in both); no
  tooling checks them against each other.

## 3. What revl gave you

- **The errata's A8 resolution entry was a complete playbook.** "fixed in
  the pinned runtime, commit X, folded into upstream PR #1" told me the
  exact shape 74a needed (fork → fix → pin → red-on-fix test flips to a pin
  of the fixed behavior) before I read a line of cordis source.
- **The pinned repro tests were precise enough to act on without re-deriving
  the bugs.** `upstream.test.ts` finding 2 made the TS residue reproducible
  in one paste; the same repro, run against the fixed lib, flipped
  `leaked: true → false` — the whole 74a verification.
- **The execution-test network caught a real regression the unit layer
  missed.** The 9 replay failures were an integration break in a harness
  that calls emitted code directly; only the full suite (1927 execution
  tests) surfaced it. That is the "the emitter did not raise never implied
  the code is right" lesson paying rent.

## 4. Time-to-green

No revl compile→refuse cycles (no revl source written). Cycles were
test→fix on the runtime side:

- 74a: baseline green (95 TS) → fork built → repro-check green (1 standalone
  run) → flipped test green → commit. One cycle, ~0 red runs after the
  flip; the flip itself was validated in both directions (old test green on
  old runtime from baseline; new test green on new runtime).
- 74b: fork probe green (1 run) → backend 58 green → full suite RED (9
  replay) → replay fix → full suite green. Longest stall: the replay
  regression + the parking-hazard audit (~25 min combined); the parking
  audit was the only genuinely slow *thinking* step — everything else was
  mechanical.

## 5. Cost ledger

- `tooling` — npm's ssh-rewrite of git deps and the codeload workaround
  (~15 min): a doc sentence ("npm records ssh for hosted git deps; use a
  tarball URL for anonymous CI") would have removed it.
- `tooling` — pre-commit timeout + stray-file archaeology (~10 min): my own
  timeout mistake; a longer default for `git commit` in this repo (the hook
  is documented as "~20s" but the pytest step is ~3.5 min) would have
  removed it.
- `diagnostic` — the 9 replay failures each printed `KeyError: 'pool_size'`
  with no hint that the harness bypasses the runtime config path (~10 min
  to locate and understand): the replay harness's direct-`apply` call is
  exactly the kind of hidden caller a `grep` checklist for "resolution moved
  out of apply" would have found up front.
- `docs-gap` — inso1337/cordis's nonexistence vs cordis-py's existence
  (5 min).
- `spec-ambiguity` — the `_pending_config` parking design relies on a
  load-ordering convention no comment states ("all revl consumers settle
  between loads"); documenting it at the `_pending_config` definition would
  have saved the audit.

Single change that would have cut the most cost: **a doc line at the
`_pending_config` definition stating the load-ordering invariant it depends
on, plus a grep-able marker in emit.py when a resolution moves out of an
emitted function** — together they remove the replay regression and the
parking audit, roughly half the total time.
