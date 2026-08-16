# revl backend: cordis v4 (TypeScript)

The v0 TypeScript backend for revl, targeting [cordis](https://github.com/cordiverse/cordis)
4.0.0-rc.8 (the reference implementation of the spatiotemporal-composability
paradigm). Implements the frozen contract in `docs/backend-ir.md`.

## Setup + test (one command each)

```sh
npm install     # pinned deps (cordis 4.0.0-rc.8, vitest)
npm test        # emits test fixtures, runs R1–R5 + emitter + upstream suites
```

Requirements: Node >= 23.6 (erasable-syntax TypeScript, used directly — no
build step) and `python3` on PATH (the emitter is pure-Python, stdlib only).

Other commands:

```sh
npm run demo       # load PgDatabase + UserCache, hot-swap the provider,
                   # unload everything; prints the R2/R3 event log
npm run golden     # regenerate golden/user_cache.ts from the reference IR
npm run typecheck  # tsc --noEmit over runtime.ts, demo.ts, golden/
```

## Layout

| path | role |
|---|---|
| `emit.py` | the emitter: `emit(ir: dict) -> str`; CLI `python3 emit.py [--runtime <path>] <ir.json>` |
| `runtime.ts` | host-builtin stub stdlib (`Pool`, `Map`) + adapter glue (config defaults, R4 introspection) |
| `golden/user_cache.ts` | checked-in emitter output for `examples/user_cache.ir.json` (regenerated + diffed by `tests/emitter.test.ts`) |
| `demo.ts` | acceptance demo with event log (R1–R4 checks, exit code reflects them) |
| `tests/semantics.test.ts` | R1–R5, each test named for the requirement it covers |
| `tests/emitter.test.ts` | golden diff, determinism, contract rejections |
| `tests/upstream.test.ts` | pinned reproductions of two upstream cordis lifecycle gaps (see `REPORT.md`) |
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
