# Codegen performance harness, cordis-py backend

What the revl python backend EMITS, measured against what a competent Python
developer writes by hand for the same semantics. The subject is
`backends/python/emit.py`, not the runtime library and not the compiler's own
speed.

Findings from the first run are roadmap item 436 in `docs/v2.0-roadmap.md`.

## Run it

    PYTHONPATH=src python bench/codegen/python/run.py
    PYTHONPATH=src python bench/codegen/python/micro.py

Nothing has to be installed beyond what compiles revl: the harness compiles
each `programs/*.rvl` in-process, emits Python through the backend, execs the
result, and calls it next to the matching function in `handwritten.py`. The
cordis-py runtime is NOT required, because every benchmark is a pure `fn`
surface with no component activation.

## Files

| file | what it is |
| --- | --- |
| `programs/*.rvl` | the benchmark programs, in revl |
| `handwritten.py` | the yardstick: the same semantics, written by hand |
| `run.py` | whole-program metrics, emitted vs hand-written |
| `micro.py` | per-construct cost of one emitted spelling vs one hand-written |
| `copycount.py` | the deterministic element-copy counter `run.py` uses |

## The metrics, and why they are these

The audit ran on a machine with a dozen other agents on it. **No timings were
taken.** Under that contention even an interleaved A/B ratio is noise: the two
arms sample different load. Everything reported by default is a property of the
program rather than of the machine, and comes out identical on every run:

- **ops**: executed bytecode instructions, counted with `sys.settrace` and
  `f_trace_opcodes`. Deterministic.
- **calls**: Python frames entered, counted with `sys.setprofile`. This is
  what prices a per-evaluation helper call or a lambda that is built and
  immediately applied.
- **copies**: container elements moved, counted by `copycount.py`, which
  re-executes the module with its AST rewritten so that every copying
  construct reports how many elements it moved. `ops` is blind here: `out +
  [x]` is ONE bytecode instruction that copies `len(out)` pointers, so an
  accumulation loop that is quadratic and one that is linear have nearly the
  same opcode count. This metric is what separates them, and it is reported at
  n and 2n so the growth is visible.
- **bytes**: `tracemalloc` peak, as corroboration on live-set growth.

`copycount.verify()` asserts that the instrumented module returns exactly what
the plain one returns, so a copy count can never be bought with a behaviour
change. `micro.py` likewise asserts that each emitted and hand-written spelling
agree on a sample of inputs before reporting their costs.

## The timing pass, later, on a quiet machine

Three duration modes exist and were deliberately not run. They are the only
place in this harness where a clock appears. Run them where nothing else is
competing for CPU:

    python bench/codegen/python/run.py --time    # interleaved A/B ratio
    python bench/codegen/python/run.py --scale   # growth exponent, log2
    python bench/codegen/python/run.py --curve   # E/H at a series of sizes

`--curve` is the most useful of the three: a ratio that RISES with n is the
signature of a complexity gap, and it reads correctly even when the absolute
numbers do not.

## Adding a case

1. Write `programs/<name>.rvl`.
2. Write the hand-written equivalent in `handwritten.py`, under the same
   function name, keeping revl's semantics exactly (bounded `Int` traps,
   truncated `%`, value semantics for `List` and `Map`).
3. Add the program to `PROGRAM_ORDER` and a row to the `cases()` table.
4. If the case is about accumulation, add it to `COPY_PROBES` too.

## Reading a result

`E/H` above `1.00x` means the emitted program costs more than the hand-written
one. A per-call constant factor is a normal codegen tax and is worth fixing
only where the construct is hot. A `copies` column that quadruples when n
doubles, next to a hand-written column that merely doubles, is a different
thing entirely: it is a complexity class the emitter introduced, and no
constant-factor work anywhere else can pay it back.
