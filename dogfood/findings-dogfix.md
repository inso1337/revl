# findings — agent/dogfood-fixes (wave-1 follow-through)

Branch: agent/dogfood-fixes off devwip. Task: turn three wave-1 dogfood findings into fixes — `revl test`
py-tier runtime preflight (uxprobe), TS generated-golden staleness pin
(map), backend emitter module collision (map addendum). One commit each.

## 1. Refusal log

No `revl compile` rejections this round — the work was on revl's own
Python tooling and vitest suites, so the refusal log is about TEST
failures instead:

1. First combined pytest run after migrating test_v2_emit.py:
   `ModuleNotFoundError: No module named 'runtime'` ×3 — my migration
   removed the `sys.path.insert(backends/python)` that exec'd emitted
   modules still needed for their own `from runtime import ...`. Verdict:
   **caught-bug** (my fix broke a second, invisible contract of that
   sys.path entry: it feeds TWO mouths — the canonical `emit` import AND
   the exec'd modules' `runtime` import). Fix keeps the path entry for
   runtime and moves only the emit load to the unique-name helper.
2. Perturbed-fixture check of the new TS staleness gate initially showed
   **12 passed where 1 failure was required**: vitest's globalSetup had
   already re-written every generated module from the (perturbed)
   fixtures before my in-test comparison ran. Verdict: **false-green**
   (the worst kind). The gate now compares fresh emit against git HEAD,
   not the working tree — a stale golden can only go green again once
   the regenerated outputs are actually committed. Re-verified: perturb
   → exactly 1 failed with the regen remedy; restore → 12 passed.
3. A first perturbation attempt (renaming a service method) crashed
   globalSetup outright (`EmitError: method 'lookup' is not declared by
   service 'Store'`) — invalid IR kills setup before any test runs.
   Verdict: **friction** with myself; perturbations for negative-testing
   this gate must keep the IR *valid* but output-*changing* (a return
   type swap works).

## 2. Friction log

- [nit] Each linked worktree has its own empty backends/typescript;
  vitest needs node_modules, so every agent either runs `npm ci` or
  symlinks a sibling worktree's. A one-line note in the TS README would
  save the detour.
- [slow] The pytest tool timeout (~30s) is shorter than a full root suite
  run (~25s+) and much shorter than cargo-involving runs; backgrounded
  processes are killed with the shell. Workaround: split suites into
  slices. Tooling friction, not repo friction — noting it because it
  shaped how these fixes were verified.
- [nit] `revl run` already printed the perfect missing-cordis remedy
  (run.py) while `revl test` stack-shaped the same situation; two
  commands, one environment failure, two UX philosophies. Fixed here,
  but the general lesson stands: remedies should live in ONE shared
  constant per environment problem.

## 3. What revl gave you (meta-dogfood: fixing revl's tooling BY revl's means)

The remarkable part of this round is that all three fixes were made
*using the project's own instruments*, and they held:

- The preflight fix is a direct transplant: `revl run` already had the
  exact diagnostic (run.py's "the cordis-py runtime is not installed …
  set it up: sh backends/python/setup.sh"), so `revl test` now quotes
  the same remedy rather than inventing a second dialect for the same
  failure. One environment problem, one message — consistency came free
  because the reference text existed.
- The TS staleness gate is a straight port of go's
  test_checked_in_generated_is_current, and porting it surfaced something
  neither language's version knew: on the TS tier the comparison target
  cannot be the working tree, because globalSetup rewrites it under you.
  The go test's *idea* survived the port; its *implementation* correctly
  did not. That is what a good test convention buys — the second copy
  teaches you something the first couldn't.
- The emitter-collision helper consolidates four ad-hoc copies of the
  same importlib dance (test_cross_tier, test_frontend, rust's loader,
  realm-conformance's loader) into one documented seam — and writing its
  docstring forced the honest exception into the open: test_replay.py
  MUST keep the canonical import, because module-object identity is load-
  bearing there. That constraint was never written down before; now it
  is, next to the code that must respect it.
- And the meta-point the protocol asks for: none of these three fixes
  touch the compiler, yet every one was located from agents' findings
  files alone — refusals and stalls logged by earlier sessions pointed
  at exact files and exact symptoms. The dogfood corpus is doing what
  production error reports never do: preserving the *experience* of the
  tool, not just its stack traces.

## 4. Time-to-green

Compile→refuse→fix cycles: ~4 total across the three fixes (1: the
runtime-import regression above; 2: the false-green gate redesign; 1:
adjusting the fixture perturbation to stay IR-valid). Longest single
stall: not a stall but a lesson — the false-green staleness gate, ~2
cycles, and it would have shipped as a test that cannot fail. What would
have shortened it: nothing in-repo; this one needed the perturb-and-
expect-failure discipline the task brief itself mandated. Full suite:
green from this worktree (slices below); combined cross-backend pytest
processes verified explicitly (85 + 41 passed).
