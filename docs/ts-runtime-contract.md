# The ts tier runtime contract

What an emitted TypeScript module may assume about the environment it runs in,
and how that assumption is gated (issue #295).

## Why this document exists

`revl test --backend ts` executed emitted modules under **vitest**. Everything
that ships executes them under **plain node**: `revl run --backend ts` and
`revl run --placement` both spawn `["node", placement_runner.ts, spec]`
(`src/revl/placement.py`).

Those are different environments, and vitest is the larger one. So a green
`--backend ts` suite proved "this runs under vitest", not "this runs on the ts
tier", and no gate could report the difference. It cost real time twice:

* A downstream consumer's `@ts` extern bodies called bare `require(...)`. Their
  suite was green; on the emitted module it is a `ReferenceError`, which their
  `try`/`catch` reported as "the host is not installed" - a false cause.
* A pull request here was red in CI for a defect that reproduces only without
  `node_modules`, and passed locally because the author had symlinked one in.

The contract was never written down, which is why neither bug was anybody's
obvious mistake. It is written down here.

## The measurement

Measured by execution, not by reading docs: the same module body run under
`node <file>.ts` and under `vitest run <file>.test.ts`, in
`backends/typescript` (a `"type": "module"` package), with this repo's own
vitest 4.1.10 and node 26.7. Reproduce with
`backends/typescript/scripts/node-tier-runner.mjs` plus a one-line probe.

**Supplied by vitest, withheld by node.** Every row below is a live divergence.

| what | under `node` | under `vitest` |
|---|---|---|
| `require` | `undefined` | `function` (resolves builtins and bare deps) |
| `module` / `exports` | `undefined` | `object` |
| `__dirname` / `__filename` | `undefined` | `string` |
| `import './helper'` (no extension) | `ERR_MODULE_NOT_FOUND` | resolves |
| `import './helper.js'` naming `helper.ts` | `ERR_MODULE_NOT_FOUND` | resolves |
| `import './dir'` (directory index) | `ERR_UNSUPPORTED_DIR_IMPORT` | resolves |
| `import x from './x.json'` (no attribute) | `ERR_IMPORT_ATTRIBUTE_MISSING` | resolves |
| `enum` / `const enum` | `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` | works |
| `namespace` | `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` | works |
| parameter properties (`constructor(private x: T)`) | `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` | works |
| `import fs = require('node:fs')` | `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` | works |
| `import.meta.env` | `undefined` | `object` |
| `import.meta.vitest` | `undefined` | `object` |
| `globalThis.__vitest_worker__` | `undefined` | `object` |
| `process.env.NODE_ENV` | unset | `"test"` |
| `process.env.VITEST` | unset | `"true"` |
| `process.argv[1]` | the module | vitest's `forks.js` |

The syntax rows are the ones a reader tends to underweight. Node runs `.ts`
in *strip-only* mode: it deletes type annotations and refuses any TypeScript
construct that would need code generation. An emitter that started producing an
`enum` would pass every `--backend ts` test and fail to even parse under
`revl run`.

**The same under both.** Recording these matters as much: they are what an
emitted module is allowed to rely on.

* Platform globals: `Buffer`, `setImmediate`, `structuredClone`, `fetch`,
  `performance`, `process`, `global`, `URL`, `TextDecoder`, `WeakRef`.
* `import.meta.url`, `import.meta.dirname`, `import.meta.filename`,
  `import.meta.resolve`.
* `import fs from 'node:fs'` and the unprefixed `import fs from 'fs'`.
* Bare package specifiers resolved from `node_modules` (for example `cordis`).
* Relative imports written with an explicit `.ts` extension.
* JSON imports carrying `with { type: 'json' }`.
* Top-level `await`.
* Type-level TypeScript: `declare module` augmentation, `abstract`,
  `satisfies`, generics, `as` casts.
* Vitest injects no `expect` / `it` / `describe` / `vi` globals: the suite's
  config leaves `globals` off, so those are imports in both environments.
* `process.cwd()` is the same in both (`backends/typescript`).

Decorators are refused by both and are therefore not a divergence.

## The contract

An emitted ts module runs as an **ES module on plain node**, from inside
`backends/typescript/` (or an install tree with the same shape). It may assume:

1. **ESM only.** No `require`, no `module`, no `exports`, no `__dirname`, no
   `__filename`. Reach for `import.meta.url` / `import.meta.dirname`, or
   `createRequire` from `node:module` when a CommonJS host module is genuinely
   required (this is what `emit.py` already does for a `@ts ref`).
2. **Explicit specifiers.** Relative imports carry their real extension
   (`./runtime.ts`). No extensionless imports, no `.js`-naming-a-`.ts`, no
   directory imports. JSON imports carry `with { type: 'json' }`.
3. **Strip-only TypeScript.** Types are erased, never lowered. No `enum`, no
   `namespace`, no parameter properties, no `import x = require(...)`, no
   decorators. Everything type-level is fine.
4. **Node builtins and resolvable bare packages.** `node:*` is always there;
   a bare package must be resolvable from the module's own location.
5. **No test-runner ambient state.** No `import.meta.env`, no
   `import.meta.vitest`, no `__vitest_worker__`, and no assumption about
   `NODE_ENV`, `VITEST` or `process.argv`.
6. **Node >= 22.18 (or >= 23.6 on the 23 line).** Below that, node does not
   strip types without a flag, so an emitted `.ts` module will not load.

Rules 1 to 5 apply to everything the emitter writes **and to every verbatim
`@ts` extern body**, because an extern body is copied into the module
unchanged. Rule 1 is the one a `@ts` body gets wrong; see #278 for the
sanctioned door to a host module in the install tree.

The Temporal emission target (`emit(ir, target="temporal")`) obeys the same
rules and additionally imports `@temporalio/workflow`, which its own worker
provides. It is executed against a real dev server in the `temporal-exit` CI
job, so it is excluded by name from the golden load check below rather than
silently dropped.

## How it is gated

The divergence is kept on purpose. Vitest buys a legible failing assertion,
file isolation and speed, and the tier's own suite
(`backends/typescript/tests/`) is written against it. What changes is that
vitest's verdict is no longer allowed to stand alone.

`revl test --backend ts` (`src/revl/test.py::run_ts`) now runs the emitted
module twice:

1. under vitest, as before - it reports a failing assertion;
2. then, on the green path only, the **same generated bytes** under plain node
   via `backends/typescript/scripts/node-tier-runner.mjs`.

The runner registers one synchronous resolve hook (`module.registerHooks`)
mapping the bare specifier `vitest` to
`backends/typescript/scripts/vitest-shim.mjs`, which provides exactly the two
names `emit.py` emits (`it`, and `expect` with `toBe` / `toBeTruthy`) and
throws by name for anything else. Every other specifier, including
`../../runtime.ts` and `cordis`, resolves the way it does for
`placement_runner.ts`. The module therefore sees a plain node module scope.

A tier verdict is `pass` only when both agree. If node cannot run an emitted
module at all (too old), the tier reports **skip with the reason** rather than
passing on vitest's word.

Two gates in CI, one per shape.

**The test shape.** `tests/test_cross_tier_execution.py` drives `RUNNERS["ts"]`
in the `conformance` job, which is the one job that installs
`backends/typescript/node_modules`, so the double execution above runs there on
every commit. Cost is about 0.11s per tier verdict.

**The run shape.** `ci/placement_smoke.sh` boots a composition across real
`node` processes and was already the right place, but its five entries all ran
`examples/user_cache.rvl`, which contains no `@ts` at all: no hand-written
TypeScript had ever executed on a node process in CI, which is how the
divergence survived. A sixth entry, `ts-host-body`, runs
`examples/ts_host_body.rvl` over the same py to node seam. That document is
`user_cache.rvl` plus one verbatim `@ts` extern body and one method that calls
it, on the node-placed component. The probe `cache.module_scope()` executes
that body on the node process and reports the module scope it was given, and
the script requires `TS-MODULE-SCOPE ESM` **in the trace**.

The trace, not the exit code, on purpose: `revl run --placement --once` exits 0
even when every probe errors (that is the incident the script's own header
records), so a case that proved only that the process came `UP` would reproduce
this issue's failure in a new place.

## If you are extending the emitter

Emitting a new vitest matcher fails the node gate by name
(`matcher \`X\` is not implemented`) instead of passing silently. Add it to
`scripts/vitest-shim.mjs` in the same change. Emitting a construct from the
divergence table above will fail the gate too, which is the point.
