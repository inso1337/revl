# revl backend: cordis v4 (TypeScript)

The v0 TypeScript backend for revl, targeting [cordis](https://github.com/cordiverse/cordis)
4.0.0-rc.8 (the reference implementation of the spatiotemporal-composability
paradigm), pinned to revl's fork [`inso1337/cordis`](https://github.com/inso1337/cordis)
at `harden-assert-active` — the rc.8 source plus the `assertActive` lifecycle
fix (see `docs/upstream/cordis-ts-assertActive.md` and
`docs/contract-errata.md`). Implements the frozen contract in
`docs/backend-ir.md`.

## Setup + test (one command each)

```sh
npm install     # pinned deps (cordis fork c8b94b2, vitest)
npm test        # emits test fixtures, runs R1–R5 + emitter + upstream suites
```

Requirements: Node >= 23.6 (erasable-syntax TypeScript, used directly — no
build step) and `python3` on PATH (the emitter is pure-Python, stdlib only).

**What an emitted module may assume about its environment is a written
contract: `docs/ts-runtime-contract.md`.** `npm test` runs the suite under
vitest, which supplies more than the shipping runtime does (a CommonJS scope
with `require`/`__dirname`, vite's module resolution, full TypeScript). So
`revl test --backend ts` re-runs every emitted module under plain node through
`scripts/node-tier-runner.mjs` before it reports a pass; read the contract
before adding a construct — or a matcher — to `emit.py`.

Other commands:

```sh
npm run demo       # load PgDatabase + UserCache, hot-swap the provider,
                   # unload everything; prints the R2/R3 event log
npm run golden     # regenerate golden/user_cache.ts from the reference IR
npm run typecheck  # every .ts in the tier: the runtime + demo as one program
                   # (tsc --noEmit), then the emitted modules and the
                   # hand-written sources one program each
```

`npm run typecheck` must run after `npm test`, which is what writes the
gitignored `tests/generated/`; run cold it says so rather than checking
nothing. Three configs divide the tier between them, and
`scripts/typecheck-handwritten.mjs` fails if any `.ts` here is matched by none
of them:

| config | covers | checked by |
|---|---|---|
| `tsconfig.json` | `runtime.ts`, `demo.ts` | `tsc --noEmit`, one program |
| `tsconfig.generated.json` | `tests/generated/**`, `golden/**` | `scripts/typecheck-generated.mjs`, one program per module |
| `tsconfig.handwritten.json` | the vitest suites, `bridge.ts`, `revl_fs_ts.ts`, `placement_runner.ts`, `bridge_node.ts`, `revl_shell*.ts`, `scripts/`, `vitest.config.ts` | `scripts/typecheck-handwritten.mjs`, one program per file |

One program per module is not a performance choice. Every emitted module
declares its provisions by augmenting cordis' global `Context`, so two modules
that provide the same service name cannot share a program: TypeScript merges
the augmentations, reports TS2717, and resolves `ctx.<key>` to whichever won.
The same rule binds inside a file — one test file may not import two modules
that provide the same key (see the note atop `tests/semantics.test.ts`).

## Layout

| path | role |
|---|---|
| `emit.py` | the emitter: `emit(ir: dict) -> str`; CLI `python3 emit.py [--runtime <path>] <ir.json>` |
| `runtime.ts` | host-builtin stub stdlib (`Pool`, `Map`) + adapter glue (config defaults, R4 introspection) |
| `golden/user_cache.ts` | checked-in emitter output for `examples/user_cache.ir.json` (regenerated + diffed by `tests/emitter.test.ts`) |
| `demo.ts` | acceptance demo with event log (R1–R4 checks, exit code reflects them) |
| `tests/semantics.test.ts` | R1–R5, each test named for the requirement it covers |
| `tests/emitter.test.ts` | golden diff, determinism, contract rejections |
| `tests/upstream.test.ts` | pinned reproductions of two upstream cordis lifecycle gaps — finding 1 (top-level effects disposed concurrently) is current upstream behavior; finding 2 (effects registered during teardown) is FIXED in the pinned fork and pins the fixed behavior (see `REPORT.md`) |
| `tests/frame_teardown.test.ts` | `Frame` unit coverage — bracket / transactional / compensation on one LIFO stack, two-phase abort, residue records (item 243 Slice 2b) |
| `tests/witnessed_teardown.test.ts` | the three-entry-kind teardown loop end to end through a real cordis composition (item 243 Slice 2b) |
| `tests/method_witnessed.test.ts` | THE H1 GATE (item 318 → 324): a witnessed fs mutation fired from a PROVIDE-METHOD, per tool call — persists on clean unload, reverts on `frame.abort()`, residue enumerable (`Frame.transactionalMethod`) |
| `REPORT.md` | impedance mismatches, upstream bugs, IR contract notes, LOC, ship-first recommendation |

The reference IR is read from `../../examples/user_cache.ir.json` when this
directory sits inside the revl repo; a byte-identical vendored copy in
`tests/fixtures/` is used (and checked for sync) otherwise.

## The one lowering decision that matters

The entire component body is lowered into a **single** `ctx.effect(function* ...)`
generator, with `provide` steps yielding the runtime's own withdrawal disposer.
Cordis disposes *top-level* fiber effects concurrently but runs the disposers
of one effect strictly sequentially in LIFO order — so this shape is what makes
R1 (LIFO) and R3 (dependents fully deactivate before the provider reverts its
own effects) hold. `tests/upstream.test.ts` keeps a reproduction of what goes
wrong with the naive per-step lowering; `REPORT.md` has the full analysis.
