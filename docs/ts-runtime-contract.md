# The ts tier runtime contract

What an emitted TypeScript module may assume about the environment it runs in,
and how that assumption is gated (issue #295). The browser target (issue #3294)
extends the same contract to a page: what it may assume there, which parts of
the runtime and cordis are portable, and the browser-engine test that holds it.

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
   `__filename`. Reach for `import.meta.url` / `import.meta.dirname`. A verbatim
   `@ts` extern body sits inside a function and cannot add a module-level
   `import`, so load a node builtin by calling `process.getBuiltinModule`
   synchronously with no import:
   `const fs = process.getBuiltinModule("node:fs")`. That works on plain node
   (>= 22.3) and under vitest alike. `createRequire` from `node:module` is the
   emitter-level machinery for a `@ts ref`, not something a body can reach:
   `_revl_createRequire` is emitted only when some extern carries `= @ts ref`
   (the `any(ext.get("refs", {}).get("ts") ...)` guard in `emit.py`), so in a
   plain `@ts` body it is not in scope. Reach for it there and you get a
   `ReferenceError`.
2. **Explicit specifiers.** Relative imports carry their real extension
   (`./runtime.ts`). No extensionless imports, no `.js`-naming-a-`.ts`, no
   directory imports. JSON imports carry `with { type: 'json' }`.
3. **Strip-only TypeScript.** Types are erased, never lowered. No `enum`, no
   `namespace`, no parameter properties, no `import x = require(...)`, no
   decorators. Everything type-level is fine.
4. **Node builtins and resolvable bare packages.** `node:*` is always there;
   a bare package must be resolvable from the module's own location. From a
   verbatim `@ts` body, load a builtin with
   `process.getBuiltinModule("node:...")` (rule 1) rather than a module-level
   `import` the body cannot write.
5. **No test-runner ambient state.** No `import.meta.env`, no
   `import.meta.vitest`, no `__vitest_worker__`, and no assumption about
   `NODE_ENV`, `VITEST` or `process.argv`.
6. **Node >= 22.18 (or >= 23.6 on the 23 line).** Below that, node does not
   strip types without a flag, so an emitted `.ts` module will not load.
7. **No ambient async context carried between emitted tests.** A module that
   carries `lifecycle test` blocks imports `AsyncLocalStorage` from
   `node:async_hooks`, snapshots the module's entry context once
   (`_revl_test_context`), and re-enters it around every driver. So a `@ts`
   extern that binds ambient context with `AsyncLocalStorage.enterWith` binds
   it for its own test and no other. This is the py reference tier's isolation
   (every py driver runs under its own `asyncio.run`, hence in a fresh copy of
   the module's context) on the ts tier, and it is version-independent by
   construction: node >= 24 confines an `enterWith` binding to the callback it
   ran in, node <= 23 does not, and without the snapshot the same emitted file
   is green on a node 26 dev machine and red on the node 22 CI pins. Issue
   #1112; the gate is `backends/typescript/test_lifecycle_async_isolation.py`,
   which reproduces the node <= 23 behaviour on a modern node with
   `--no-async-context-frame`. A document with no lifecycle test imports
   nothing new.

Rules 1 to 5 apply to everything the emitter writes **and to every verbatim
`@ts` extern body**, because an extern body is copied into the module
unchanged. Rule 1 is the one a `@ts` body gets wrong; see #278 for the
sanctioned door to a host module in the install tree.

One thing is done *to* a body rather than assumed by it: its result is coerced
to the extern's **declared return type** on the way out. `Int` is a `bigint` on
this tier, while a verbatim JS body naturally yields a `number`, most often
from `Math.floor(...)`. For a body declared `-> Int`, the emitter wraps it as
`return revlToBigInt((() => { ... })())`. The body keeps its shape and its own
`return` statements, and the value that leaves the function is a `bigint`. The coercion follows the three cases that `bridge.ts`'s
`toBigInt` uses at the wire seam (issue #1566), in the same order, so the two
sides of one boundary cannot disagree, and it is applied to the declared type
rather than to a guessed value
because nothing at the boundary says which numbers are `Int`s. `Int` is the
only return type that needs it (`Int32`/`Float` are already a `number`,
`Bool`/`Str`/`Bytes` are what a body naturally returns, `Unit` yields nothing),
and the helper is emitted only in a module that has such an extern. Issue #2147;
without it the emitted function's `bigint` annotation was false and the value
threw `Cannot mix BigInt and other types` at its first use in arithmetic.

The Temporal emission target (`emit(ir, target="temporal")`) obeys the same
rules and additionally imports `@temporalio/workflow`, which its own worker
provides. It is executed against a real dev server in the `temporal-exit` CI
job, so it is excluded by name from the golden load check below rather than
silently dropped.

## The browser target

A page can load an emitted module, the runtime it imports and cordis, with no
build tool at run time. The gate is
`backends/typescript/test_browser_target.py`, which runs an emitted module in
headless Chrome; this section is what it holds.

### What the page has to supply

A browser is not plain node with a different global object. Two things the node
tier gets from node are not in a page:

1. **JavaScript, not TypeScript.** A page has no type stripper. Emit the
   module, then strip it, and the runtime it imports, from
   `backends/typescript` after `npm ci`:

       node scripts/strip-types.mjs <out-dir> <module.ts> runtime.ts

   The script erases types with the TypeScript compiler the package already
   depends on, and rewrites every relative `.ts` specifier to the JavaScript
   file it resolves to. Outputs land flat in `<out-dir>`, so the default
   emission (`import ... from '../runtime.ts'`) becomes `./runtime.js`, beside
   the module. A relative `.ts` specifier that resolves to a file the step was
   not given fails the step, because the page could not load it either. Run the
   step once when the module is emitted; the page then loads plain ESM. This is
   the strip step the issue allowed in place of a second JavaScript rendering of
   the emitter: an emitted module is strip-only TypeScript by rule 3, so
   stripping is one mechanical step with one input and one output, while a
   second rendering would be a second emitter to keep in step with this one.

2. **`cordis` as a resolvable specifier.** The emitted module imports cordis
   for its `Context` type, and `runtime.ts` imports it for types. A page
   without a bundler resolves the bare specifier with an import map. These
   three entries are enough, with paths relative to `backends/typescript`; all
   three packages are already ESM with no node builtin imports:

   | specifier | path |
   |---|---|
   | `cordis` | `node_modules/cordis/lib/index.js` |
   | `cosmokit` | `node_modules/cosmokit/lib/index.mjs` |
   | `@standard-schema/spec` | `node_modules/@standard-schema/spec/dist/index.js` |

   Nothing else is needed for a document with no `lifecycle test` blocks and no
   `@ts ref` extern: after stripping, `runtime.ts` has no imports at all, and
   such an emitted module's only other import is its relative `./runtime.js`.

`Int` is a `bigint` on this tier (docs/arithmetic.md), so the page needs a
`BigInt` engine. Every engine the gate can find has one.

### What is portable

`runtime.ts` names no node builtin in an import, and its one node lookup is
optional:

    const getBuiltin = (globalThis as any).process?.getBuiltinModule
    const hooks = typeof getBuiltin === 'function' ? getBuiltin('node:async_hooks') : undefined

A page has no `process`, so `_uiStorage` returns null and the ui scope uses the
same stack fallback it uses wherever the builtin is absent. That fallback is the
supported path, not an error path.

The `host` object an emitted module resolves host calls against is portable in
exactly these members:

`Stream`, `Pool`, `Map`, `Job`, `scheduleEvery`, `scheduleAfter`,
`clockAdvance`, `clockReset`, `applyConfigDefaults`, `markSecret`,
`secretResult`.

The gate asserts the exact set, so a new host member is an edit to this list and
to the gate together. A member that reaches for the filesystem, a child process,
a socket or another node builtin does not belong in it.

### What is outside the target

The process transport is node-only and stays node-only: `bridge.ts`,
`bridge_node.ts`, `placement_runner.ts` and `estop.ts` import
`node:child_process`, `node:fs`, `node:net`, `node:tls` or `node:crypto`, so a
page cannot load them. `revl run --placement` and the host bridge are not
browser features; a page loads one emitted module directly.

The platform globals in "The same under both" split the same way. A page has
`structuredClone`, `fetch`, `performance`, `URL`, `TextDecoder`, `WeakRef` and
`import.meta.url`; it does not have `Buffer`, `setImmediate`, `process` or
`global`, and `import.meta.dirname` / `import.meta.filename` are node
extensions. An emitted module does not reach for any of them, but a verbatim
`@ts` body that does is outside the target.

A document that reaches for `fs` or `shell` imports a host module that names
node builtins (`revl_fs_ts.ts`, `revl_shell_ts.ts`), and is outside the target
for the same reason. A document that carries `lifecycle test` blocks is outside
it as well: those blocks make the emitted module import `AsyncLocalStorage` from
`node:async_hooks` (rule 7), which a page cannot load. So is a document with a
`@ts ref` extern: the reference machinery is emitted as module-level imports of
`node:module`, `node:url` and `node:path`. Documents that stay off all of these,
and whose host surface is the list above, are in it.

The playground wheel is unchanged: it still packages `backends/python` and not
the ts emitter, so a page compiles nothing. The third wanted item of issue
#3294 (the ts emitter in the wheel) is not part of this target.

### How the browser target is gated

`test_an_emitted_module_runs_in_a_browser_engine` emits a document with a
top-level `fn` and a component that provides a service, strips the module and
`runtime.ts`, serves them with the import map above, and runs the page in
headless Chrome. The page calls the function, applies the component through
`plug`, calls the service method and reports the `host` keys it sees; the test
asserts the function's value, the service's value and the host set (as a set:
the order of `host` is not part of the contract). A page whose module throws
while running reports `REVL-BROWSER-ERROR` with its stack. A static import that
cannot be resolved stops the module before the script runs, so the title stays
`PENDING`; the test fails on that title, which names what it saw either way.

The engine is found from `REVL_CHROME` (which names it exclusively when set),
then `google-chrome` and the other usual names, then the macOS application
paths. The `backend-typescript` job runs the
test with `REVL_BROWSER_REQUIRED=1` (`ubuntu-latest` carries `google-chrome`),
which turns a missing engine into a failure with the command to install one; a
local run without an engine skips with the same reason. A gate that skips in CI
would prove nothing, which is why the job sets the variable.

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

Both executions carry a wall-clock bound, because the deadline is on a process
whose runtime is a property of the HOST: on a loaded machine a run that is
still working is killed and reported as a failure (issue #2229). The bound is
`REVL_TS_TIMEOUT` seconds, default 180, read by `src/revl/test.py`; vitest's own
`testTimeout`/`hookTimeout` are derived from the same variable in
`backends/typescript/vitest.config.ts` at the ratio the two literals already had
(a third, so 60s by default), so vitest reports a named test first and the outer
bound is the backstop. When the outer bound fires, the verdict names the
composition, the bound and the variable to raise, instead of a raw
`subprocess.TimeoutExpired`; the timed-out child's whole process group is killed,
so no vitest fork worker outlives the run. `REVL_TS_TIMEOUT` must be a
positive integer of at most 86400 seconds; any other value (empty, zero,
negative, non-numeric, fractional, larger) fails the tier with a message naming
the variable and that rule.

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
