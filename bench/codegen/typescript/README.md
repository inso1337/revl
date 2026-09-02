# TypeScript codegen benchmarks

What the revl TypeScript backend emits, measured against what a competent
TypeScript developer writes by hand for the same revl program. The gap is
emitter waste.

This harness reports **counts, not durations**. Microtask turns, Promise
allocations, code points materialised, array elements copied. Counts are
deterministic: they are the same on an idle laptop and on a box running a dozen
agents, which is why they are safe to act on. The default output contains no
timing at all. `--timing` adds an interleaved A/B ratio and prints a banner
saying it is meaningful only on an idle machine.

## Running it

Needs node >= 22.18 for native TypeScript type stripping (checked at 26.7.0),
and `cordis` resolvable from the repository root for the two component cases.

```
npm --prefix backends/typescript install          # once, for cordis
ln -s backends/typescript/node_modules node_modules   # so bench/ resolves it

node --expose-gc bench/codegen/typescript/run.mjs
node --expose-gc bench/codegen/typescript/run.mjs --case match-sync-arms-in-async-fn
node bench/codegen/typescript/cpuprof.mjs
```

On an idle machine, add durations:

```
node --expose-gc bench/codegen/typescript/run.mjs --timing
```

Regenerate the emitted code after an emitter change:

```
PYTHONPATH=$PWD/src python3 bench/codegen/typescript/emit.py
```

## Layout

| path | what it is |
| --- | --- |
| `src/*.rvl` | the benchmark programs, in revl |
| `emit.py` | runs `revl emit --backend typescript` over `src/` into `emitted/` |
| `emitted/*.ts` | machine output, checked in so a shape change is visible in a diff |
| `cases/*.ts` | one case per shape: imports the emitted function, defines the hand-written equivalent, declares provenance |
| `lib/measure.mjs` | the counting primitives |
| `run.mjs` | the driver |
| `cpuprof.mjs` | CPU-profile sample-count attribution |
| `results/` | captured output from the last run |

## Why the cases import from `emitted/`

Every case imports the emitted function directly and executes it. Nothing is
transcribed, so a case cannot silently measure a shape the compiler stopped
producing. On top of that each case declares a `provenance` list of fragments
that must still appear in its emitted file; `run.mjs` checks them before
measuring and reports `STALE` rather than producing a number if the emitter has
moved on.

The hand-written side keeps every leaf the emitted side uses (the same
`decode`, the same `revlI64` overflow guard, the same helpers) and changes only
the one shape under test, so the delta is attributable.

## The measures

**Microtask turns.** A spinner reschedules itself on the microtask queue for as
long as the workload runs. The queue is FIFO and single-threaded, so the number
of spinner turns is the number of microtask turns the workload occupied. No
timer, no I/O, no dependence on machine load. This is the measure that catches
an `await` the program never asked for.

**Promise allocations.** `async_hooks` fires `init` for every Promise the VM
creates, including the implicit ones an `async` function and an `await`
allocate. An exact count.

**Code points materialised.** `Array.from` is intercepted and the lengths of
its results are totalled. This turns "the string helpers are quadratic" into a
number that grows visibly with the input.

**Array elements copied.** The array iterator is intercepted, which forces
spread onto its slow path and makes every copied element observable. Counting,
not timing, so the deopt does not distort the result.

**CPU-profile sample counts.** `cpuprof.mjs` reports the SHARE of self-samples
each function received. A share is a ratio inside one profile, so if the
process is descheduled every function loses samples together and the shares are
unchanged. Both arms of a comparison run the same iteration count, so the
sample totals are comparable to each other.

## Adding a case

1. Write the revl program under `src/`.
2. `PYTHONPATH=$PWD/src python3 bench/codegen/typescript/emit.py`.
3. Read `emitted/<name>.ts` and decide what a competent TS developer writes.
4. Add `cases/<name>.ts` exporting `name`, `summary`, `provenance`, `emitted`,
   `hand`, and either `check` (asserting the two agree) or nothing if the case
   is shape-only. Optional: `opsPerRun` to normalise per logical operation,
   `shape` for a source-read closure/await count, `emittedHot`/`handHot` for the
   `--timing` pass.
