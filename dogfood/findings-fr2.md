# findings-fr2 — wiring `revl run --backend ts` (roadmap 77(b), FEATURE-REQUESTS FR-2)

Branch `agent/fr2-run-ts` off devwip @ b460a63. The ts tier emitted
(`backends/typescript/`) but had no run driver: `revl run --backend ts`
answered `error: not wired yet`. DSH is a TS/Cordis system and the roadmap's
lighthouse entry targets ts, so the harness needs the tier to *run*. This
slices off the driver exactly as the rust tier's (src/revl/run_rust.py): emit
the cordis-ts module → node boot → the `--once` boot → LIFO teardown →
no-residue round-trip, gated by `tests/test_run_ts.py`.

## 1. Refusal log

Zero `revl compile` rejections this run: no `.rvl` authoring was needed —
`examples/outcome.rvl` compiled and emitted on the first attempt, and the
driver's job is plumbing around the emit, not changing the language surface.
The one refusal-shaped behavior encountered was a *test* refusal, not a
checker one: `tests/test_run.py::test_run_refuses_a_backend_that_is_not_wired_yet`
pinned ts as the flat-`not wired yet` example; wiring ts made it wrong, so it
was rewritten to assert ts now reaches the config preflight (rc 1) instead of
short-circuiting on the wiring refusal (rc 2). Verdict: `caught-bug`-adjacent
— the old test was correct *about the old state*; the tripwire is generic
tests coupling to a specific tier's unwired-ness (next unwired tier will hit
it again; see §5).

## 2. Friction log

- `[slow]` **The pre-commit hook runs the full `pytest tests/ -q` (45s+) but
  the hook's own header says "Kept under ~20s on purpose".** My first commit
  was killed at the default 60s bash timeout mid-hook — the commit had not
  happened, the tree was staged, and it took a `git status` + reading the hook
  to be sure nothing was half-written. The hook comment is stale vs the suite
  it runs.
- `[nit]` **Running the ts vitest suite leaves scratch in the checkout**: a
  `tests/generated/revl_test_<pid>.test.ts` (untracked, not gitignored) appears
  after `npx vitest run` — the emitter test spawns vitest over a temp-emitted
  test that lands in the tracked `tests/generated/` dir and is never cleaned.
  I deleted it before committing; it would otherwise have dirtied every ts
  commit's `git status`.
- `[slow]` **Finding the node emit path required reading placement.py source.**
  The driver reuses `_placement._emit_ts_module` (emit → `backends/typescript/
  _gen/mod_<tmp>.ts` so `../runtime.ts`/`cordis` resolve), but that function is
  named `_private` and documented only by its call site; roadmap 77(c) itself
  flags the missing repo-map line pointing at `backends/*/emit.py`. A one-line
  pointer in the contributor notes would have saved the grep.
- `[nit]` **Two node gates now exist and disagree on the minimum version.**
  `revl run --placement`'s preflight checks node presence only (message says
  ">= 22, for --experimental-strip-types"); the new run driver requires
  >= 23.6 (native type stripping, since neither path passes the flag). A node
  22.6–23.5 install would pass placement's gate and fail the run gate. Honest
  either way, but the two messages should agree.
- `[nit]` A run leaves an empty `backends/typescript/_gen/` dir behind
  (gitignored, so harmless; the placement conductor leaves it too). The emitted
  module file itself is removed by the driver's `finally`.

## 3. What revl gave you

- **The first boot was green.** `outcome.rvl` (records + ADT + Result, two
  components) emitted TS that node ran correctly on the first attempt: both
  fibers reached ACTIVE, providers first, `[run] UP` printed. No FR-7
  (`revlSlice` union) bite — `outcome.rvl` uses no string slicing, so the
  helper isn't even emitted, and node's type stripping means the run path
  never consults `tsc` anyway. FR-7 stays entirely with its own agent; the
  driver deliberately has no tsc build gate (matching the placement node
  path), and the module docstring says so.
- **The no-residue proof is the tier's own R4 machinery, for free.**
  `snapshotRuntime`/`assertNoResidue` (backends/typescript/runtime.ts) diff the
  full pre-load snapshot — registry, reflect store, root effects, event hooks,
  *and host resources*. The `Map.new()` effects in DirSvc/DirUser (with their
  `undo drop()`) came up as `0 live plugin(s)` / `0 service(s) provided` /
  NO-RESIDUE, which is the cordis-ts mirror of the py driver's
  `registry.size`/`reflect.store` check and the rust runner's
  `registry().len()`/`reflect().services().len()` check — the guarantee the
  harness's core claim rests on, now asserted on the tier DSH actually runs.
- **LIFO came out without me writing an order.** The driver disposes fibers in
  reverse load order (consumers before providers), exactly the py driver's
  `_dispose_all` contract; the `swap` log lines show DirUser (consumer) down
  before DirSvc (provider), and the test pins that ordering. The emitted
  inject graph (`DirUser` requires `dir`) means cordis itself would have
  deactivated the consumer first on a provider withdrawal anyway — the
  driver's order and the runtime's reactive order agree.

## 4. Time-to-green

No compile→refuse→fix cycles (no revl authoring). The driver's first smoke
test booted green; the whole implementation was ~3 edit/write rounds before
the first passing `pytest tests/test_run_ts.py`. The single longest stall was
not language work at all: the 60s-timeout commit kill (~2 minutes of "did the
commit happen?" confusion) — a tooling cost, not a language cost.

## 5. Cost ledger

- `tooling` — one wasted cycle: commit killed at the 60s tool timeout while
  the pre-commit hook ran the full suite; needed a 900s timeout. Fix: hook's
  "under 20s" comment is stale, or a frontend-fast-path flag.
- `tooling` — vitest run dirtied the checkout with `tests/generated/
  revl_test_<pid>.test.ts`; manual delete before commit.
- `docs-gap` — `_emit_ts_module` found only by reading placement.py; the
  repo-map line roadmap 77(c) asks for would have removed the grep.
- `spec-ambiguity` — `test_run.py`'s generic "not wired yet" test pinned ts
  specifically; wiring ts invalidated it (expected), but a future tier
  (go) will trip the same test-shaped coupling again.
- `env` — the worktree has no `.venv`; the pre-commit hook falls back to the
  primary checkout's venv via git-common-dir (worked, but the error hints in
  run.py assume a local `backends/python/.venv`).

The single change that would have cut the most cost: a `pytest tests/ -q` that
finishes in the hook's claimed ~20s (or a hook that says what it actually
takes) — the commit-timeout confusion was the whole stall.

## Verification

- `tests/test_run_ts.py`: 5 passed (runnable-backend, no-flat-refusal, plan,
  once round-trip, no emitted-module residue).
- TS tier: `cd backends/typescript && npx vitest run` — 15 files, 95 tests
  passed (placement_runner change is additive: `once` is never set by
  placement specs).
- Full `pytest tests/ -q`: green via the pre-commit hook; final counts
  recorded at push time.
