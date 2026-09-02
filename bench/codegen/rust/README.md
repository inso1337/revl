# Codegen performance harness — rust backend

What does `backends/rust/emit.py` emit, and how far is it from the rust a
competent developer would write by hand for the same semantics? That gap is
emitter waste. This harness measures it.

## Running it

```
PYTHONPATH=$PWD/src python3 bench/codegen/rust/run.py                 # everything
PYTHONPATH=$PWD/src python3 bench/codegen/rust/run.py str_append      # one
PYTHONPATH=$PWD/src python3 bench/codegen/rust/run.py --json out.json --keep
```

Needs `cargo` and a resolvable `cordis-rs` (the emitted module imports it).
When either is missing the harness says so and measures nothing; it never
reports analysis as if it were measurement. The first benchmark pays a cold
`cordis-rs` + `serde` build (about a minute); the rest share one target
directory and build in a few seconds each.

## No timing by default, on purpose

**`run.py` takes no wall-clock measurement unless you pass `--timing`.**

A duration measured on a machine that is running other work is not evidence,
and a ratio of two durations sampled at different moments is not evidence
either: the two arms see different load, so the ratio carries the noise rather
than cancelling it. A number that looks measured is worse than no number,
because someone will act on it.

What the harness reports instead is load-independent, meaning it reads the
same on an idle laptop and on a host running a dozen jobs:

- **Heap allocations and heap bytes**, counted by a counting global allocator
  installed in the driver. This is the primary evidence. The driver counts one
  call of each side and subtracts the cost of evaluating the argument
  expressions alone, so the figure belongs to the function and not to the
  harness building its input.
- **Generated-code shape**: counts of `.clone()`, `String::from(`,
  `.to_string()`, `format!(`, `.collect()` and `.chars()` in the emitted
  function against the hand-written one. A property of the text.
- **Agreement**: every benchmark asserts the two sides return the same value
  before anything else is reported. A comparator that computes something else
  is not a comparator.
- **Complexity**, argued in the benchmark's own comments and checked against
  how the allocation count scales. Where a gap is a complexity class rather
  than a constant, the argument does not depend on the machine at all, which
  makes it stronger than any single timing.

To add a timing pass on a QUIET, otherwise-idle host:

```
PYTHONPATH=$PWD/src python3 bench/codegen/rust/run.py --timing --rounds 25
```

That prints a per-benchmark median emitted/hand-written ratio with its range.
Say where it was run when quoting it. Absolute milliseconds stay in `--json`.

## Layout

```
programs/      benchmark programs, in revl
handwritten/   the hand-written rust comparator for each, same semantics
run.py         emit, assemble, build, run, report
```

`run.py` builds ONE cargo crate per benchmark holding two modules, `emitted`
and `handwritten`, plus a generated driver. Both modules are compiled with the
same profile in the same crate, so the comparison is not confounded by build
settings.

## Adding a benchmark

1. `programs/<name>.rvl` — a `pub fn <name>` with the shape under test.
2. `handwritten/<name>.rs` — `pub fn <name>` with the SAME signature and the
   same observable semantics, written the way a competent rust developer would
   write it. Say in the doc comment WHY that is what they would write.
3. An entry in `BENCHES` in `run.py`: `setup` (rust that builds the inputs),
   `args` (one rust expression per parameter), `iters` (used only under
   `--timing`).

Keep the two signatures identical. The moment the comparator takes different
arguments it stops measuring the emitter's choices and starts measuring two
different APIs.

Preserving semantics is the whole job. revl `Int` traps on overflow, so the
comparator keeps `checked_add(..).expect(..)`; revl `Str.length` is a codepoint
count, so the comparator counts codepoints; revl `Str.indexOf` returns a
codepoint index, so the comparator converts a byte offset rather than
pretending the string is ASCII. A comparator that quietly drops a guarantee
would report emitter waste that is really a correctness difference. Where a
clone in the emitted code is genuinely required by the program (a binding read
after the loop), keep it in the comparator too, rather than winning on a
difference that is not the emitter's fault.

## Related measurements

- `tools/bench_selfhost_rust.py` — the whole self-host compiler on the native
  tier against the CPython baseline. Bigger, coarser, one number per stage,
  and wall-clock based.
- `docs/bench-selfhost.md` — the standing record of those numbers, including
  the item 277 / 282 / 283 / 284 sequence. Item 277 in particular records a
  plausible rust perf change here that REGRESSED, which is why this harness
  leads with allocation counts rather than with intuition about what should be
  faster.

This harness is deliberately narrower than either: one emitter decision per
benchmark, so a finding points at a line of `backends/rust/emit.py`.
