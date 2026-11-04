# Cross-tier cost of kernel-shaped work (issue #2709)

`bench/extern_call_cost.py` times one host extern call on the Python tier. This
harness answers the next question: what does **kernel-shaped work** cost on each
emitted tier, against the same work written by hand in native Rust as the
floor?

It runs seven shapes from `kernels.rvl` on four tiers:

| tier | what runs | artifact |
| --- | --- | --- |
| `py` | the emitted Python module on CPython | emitted module source |
| `ts` | the emitted TypeScript module on node's type stripping | emitted module source |
| `rust` | the emitted Rust module in one release crate | the release binary |
| `wasm` | the emitted pure-function WAT module on wasmtime | the `.wasm` module |

and against `native/`, the handwritten Rust reference for the same seven
shapes and the same checksums. The kernels are:

`schema_validate`, `regex_scan`, `vector_rerank`, `policy_eval`, `hash_bytes`,
`simulate`, `batch_transform`. A control, `loop_probe`, is a minimal
pinned-list scan; the tier's per-call cost over the native control is reported
as loop overhead. It does not cross a host or tier boundary.

Every tier's checksum is compared with the native reference at the same `n`
before a latency is reported. A disagreement fails that row rather than
becoming a number.

## Running it

```sh
python3 bench/cross_tier_kernels/harness.py            # print and verify
python3 bench/cross_tier_kernels/harness.py --write     # refresh the result
python3 bench/cross_tier_kernels/harness.py --only rust,wasm --keep /tmp/ct
```

Needs `cargo` and `rustc` for the `rust` and `native` tiers, `node` for `ts`,
and `wasm-tools` plus `wasmtime` for `wasm`. A missing toolchain is reported as
a skipped tier and its rows are not measured; nothing is inferred for it. The
native reference is not optional when an emitted tier is requested: the harness
builds it too (so `--only rust,wasm` still checks agreement), and it refuses to
report emitted rows if `cargo` is missing. `--write` is refused unless all five
tiers were measured and no row failed, so a partial run cannot overwrite the
committed result. The Python tier's own emitted module needs only `python3`,
but the native floor requires `cargo` for any run that reports a row.

The committed table is
[results/cross-tier-kernels.md](../results/cross-tier-kernels.md).

## What each column is

| column | how it is measured |
| --- | --- |
| `kernel`, `tier` | the row's shape and implementation |
| `latency ns/call` | process CPU ns per call, from the driver's own clock, divided by the calibrated `n` |
| `throughput calls/s` | `1e9 / latency` |
| `artifact bytes` | the deployed artifact: emitted module source, release binary, or `.wasm` |
| `cold start ms` | tier-level: minimum wall time of three warm `n=0` runs, so process start plus module load |
| `peak memory KB` | maximum resident set of the timed run, from `/usr/bin/time` |
| `loop overhead ns` | tier-level: the tier's control per call minus the native control's per call, floored at 0 |
| `capability overhead ns` | tier-level: the Python tier's witnessed crossing minus its body; n/a where the harness does not isolate a witnessed path |
| `n` | the calibrated repetition count for that row |

Latency is **process CPU time**, for the reason the first slice gives: a
machine running other work moves wall time and not CPU time. Wall time is read
only for cold start, where the question is startup; that column is the minimum
of three warm runs because a contended host inflates any one of them. Peak
memory is the `/usr/bin/time` maximum. `n` is calibrated per tier and kernel so one timed call
clears the CPU clock resolution; the wasm tier gets a longer target because
every call is a fresh wasmtime process and its startup is subtracted from the
CPU delta.

`artifact bytes` is not one kind of object across tiers: it is emitted module
source for `py` and `ts`, and a compiled release binary for `rust` and `native`,
and the `.wasm` module for wasm. Compare a row inside one tier, not 100 KB of
JavaScript against 5 KB of wasm as if they were the same thing.

`capability overhead` is a property of the tier's call path, not of the pure
kernel. The Python number is the witnessed crossing from
[results/extern-call-cost.md](../results/extern-call-cost.md); a pure kernel
call does not pay it, and the other tiers do not isolate a witnessed path here.

## Layout

| path | role |
| --- | --- |
| `kernels.rvl` | the seven shapes and the control, one pure function each |
| `native/` | the handwritten Rust reference and its driver, the floor |
| `drivers.py` | the driver source the harness writes for each tier |
| `harness.py` | emit, build, calibrate, measure, gate agreement, render |
| `../results/cross-tier-kernels.md` | the committed run |

## The decision rule

The rule the issue states, and that the numbers are read against: **native
work is only justified when the gain survives call overhead.** A native kernel
under 20% faster than the emitted path is not worth a tier boundary; a 5x to
10x gain is; anything between needs the call overhead subtracted before it
counts. The rule is also stated in [bench/README.md](../README.md).

This harness reports loop overhead, not call overhead, so applying the rule
needs the crossing number from
[results/extern-call-cost.md](../results/extern-call-cost.md).

The ratio is the emitted path against the floor, not a verdict that a native
kernel is the right repair. Where the gap is a shape the emitter writes badly,
the repair belongs in the emitter: the Rust tier lowers `Str.charAt` to
`chars().nth(i)`, so the string-scan kernels are quadratic there and their
ratio is an emitter cost, not a property of native code. The codegen benches
under `bench/codegen/` are where that belongs.
