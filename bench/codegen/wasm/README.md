# wasm codegen bench (roadmap item 432)

Measures what the **wasm emitter produces**, not how fast the machine is.

Nothing here is timed. Every number is deterministic and reproducible on a
loaded box: wasmtime execution **fuel** (an exact count of executed wasm
operations), module **byte size**, **reachable vs defined** function counts,
static **allocation** and **memory.copy** counts per call. A wall clock is
deliberately never read, so results are comparable across machines and
across load.

## Run it

Needs `wasm-tools` and `wasmtime` on PATH, plus `revl` importable.

```sh
PYTHONPATH="$PWD/src" python bench/codegen/wasm/run.py \
    --json bench/codegen/wasm/results.json
PYTHONPATH="$PWD/src" python bench/codegen/wasm/probe_heap.py
```

`--quick` drops the wasmtime work and reports the static metrics only, so it
runs anywhere. `--only <program>` restricts to one program.

Executed against **wasmtime 47.0.3** and **wasm-tools** (Homebrew), through
the component model, on macOS arm64.

Fuel bisection is many short `wasmtime run --invoke` processes, so a full run
takes a few minutes. That is process startup, not measurement: the fuel
number a bisection converges on is exact and does not move.

## What is here

| file | role |
|---|---|
| `programs/*.revl` | the bench programs, one boundary shape each |
| `harness.py` | emit, static analysis, component build, fuel bisection, variants |
| `run.py` | the bench: baseline vs each variant, per program |
| `probe_heap.py` | executed proof that the emitted heap is one fixed page that never grows and never frees |
| `results.json` | the last recorded run |

## Variants

A **variant** is a textual rewrite of the *emitted* core WAT standing in for
a proposed emitter fix. The emitter is never touched, which keeps this an
audit, and each variant is what a competent wasm author would have written
for the same semantics.

`run.py` gates every variant on agreement: a variant must return exactly what
the baseline returns for the same argument before its numbers are reported. A
faster wrong module is not a finding.

| variant | stands in for | status |
|---|---|---|
| `zerocopy_lift` | `cabi_realloc` reserves the length header, so lifting a `Str` is one store instead of a copy | landed, 432(a) |
| `memcopy_lift` | the conservative version: `$__canon_lift_str` uses `memory.copy` instead of a byte loop | superseded by 432(a) |
| `list_bulk` | `List[Int]` crosses by `memory.copy`, and the lowered side aliases the already-canonical body | landed, 432(c) |
| `fused_concat` | one k-ary concat per template literal instead of a k-1 deep pairwise chain | landed, 432(d) |
| `prune_dead` | every function no export can reach is dropped from the module | landed, 432(b) |
| `const_fold` | a constant `+ - *` is evaluated instead of calling the checked helper | landed, 432(g) |
| `static_return_area` | one module-level canonical return area instead of one bump allocation per call | landed, 432(f) |
| `combined` | all of the above, which is how they would ship | |

Every finding is now in `backends/wasm/emit.py` / `canonical.py`, so the
**baseline** already is what the variants stood in for. Each rewrite
recognises the emitter's own marker comment (`;; item 432(a)` and friends) and
becomes a no-op rather than failing, so `run.py`'s agreement gate still covers
the same variant/program pairs and the rewrites still apply to a checkout of
the pre-fix emitter. Their `saved` rows therefore read 0 against a current
emitter: the win moved into the **baseline** row.

## Reading the numbers

* **`saved`** is baseline fuel minus variant fuel, per call. Exact: the
  `--invoke` instantiation charge is identical across variants of one
  program, so it cancels.
* **`fuel/byte`** is the slope across the argument sweep. Exact:
  instantiation does not scale with the argument. This is the honest
  per-operation number.
* A raw fuel **total** also contains instantiation. Measured, not assumed: a
  module carrying `(data ...)` segments pays about 16385 fuel to
  instantiate, so a ratio of totals understates the per-call ratio. Quote
  the slope or the delta.
