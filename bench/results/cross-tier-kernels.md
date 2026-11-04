# Cross-tier cost of kernel-shaped work (issue #2709)

From `python3 bench/cross_tier_kernels/harness.py`; the committed copy
is refreshed with `--write`.

Toolchain: python 3.14.7 on Darwin-25.2.0-arm64-arm-64bit-Mach-O; node v26.7.0; rustc 1.85.1 (4eb161250 2025-03-15) (Homebrew); wasmtime 47.0.3 (5554cc1a6 2026-07-31); wasm-tools 1.257.1.

Seven kernel shapes from `kernels.rvl` on each emitted tier, against the
handwritten native Rust reference in `native/` as the floor. Every tier's
checksum is compared with the native reference before a latency is
reported; a disagreement fails the row rather than becoming a number.

Latency is process CPU time per call, so a machine running other work
moves wall time and not this column. `n` is calibrated per tier and
kernel, given in the last column: the checksum is still compared with
the native reference at the same `n`, so the per-call figures compare.

Calibration: one timed call takes about 10 ms on the
self-timed tiers and 100 ms on wasm (one wasmtime
process per invocation); 5 samples,
median. Cold start is the
minimum of three warm n=0 runs per tier; peak memory comes from
`/usr/bin/time` around the timed run.

The host was not idle when this ran. Latency is CPU time and so is mostly
load-independent, but CPU frequency still moves it: treat one row's
absolute nanoseconds as a snapshot, read the tier ratios, and rerun on an
idle host before acting on a single row.

| kernel | tier | latency ns/call | throughput calls/s | artifact bytes | cold start ms | peak memory KB | loop overhead ns | capability overhead ns | n |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| schema_validate | native | 126.1 | 7,928,151 | 488,064 | 3.9 | 1,472 | 0.0 | 0.0 | 128,000 |
| schema_validate | py | 15,064.0 | 66,383 | 10,043 | 31.5 | 17,088 | 308.5 | 1,824.0 | 1,000 |
| schema_validate | ts | 23,644.0 | 42,294 | 12,871 | 98.4 | 99,872 | 352.5 | n/a | 1,000 |
| schema_validate | rust | 7,868.5 | 127,089 | 488,704 | 3.9 | 1,456 | 0.0 | n/a | 2,000 |
| schema_validate | wasm | 7,844.7 | 127,475 | 5,819 | 7.9 | 16,912 | 3.8 | n/a | 16,000 |
| regex_scan | native | 69.3 | 14,433,920 | 488,064 | 3.9 | 1,472 | 0.0 | 0.0 | 256,000 |
| regex_scan | py | 22,884.0 | 43,699 | 10,043 | 31.5 | 17,120 | 308.5 | 1,824.0 | 1,000 |
| regex_scan | ts | 37,857.0 | 26,415 | 12,871 | 98.4 | 101,808 | 352.5 | n/a | 1,000 |
| regex_scan | rust | 11,574.0 | 86,401 | 488,704 | 3.9 | 1,472 | 0.0 | n/a | 1,000 |
| regex_scan | wasm | 8,142.1 | 122,818 | 5,819 | 7.9 | 23,424 | 3.8 | n/a | 16,000 |
| vector_rerank | native | 29.2 | 34,293,369 | 488,064 | 3.9 | 1,456 | 0.0 | 0.0 | 512,000 |
| vector_rerank | py | 21,536.0 | 46,434 | 10,043 | 31.5 | 17,296 | 308.5 | 1,824.0 | 1,000 |
| vector_rerank | ts | 28,015.0 | 35,695 | 12,871 | 98.4 | 100,160 | 352.5 | n/a | 1,000 |
| vector_rerank | rust | 455.4 | 2,195,842 | 488,704 | 3.9 | 1,472 | 0.0 | n/a | 32,000 |
| vector_rerank | wasm | 533.4 | 1,874,812 | 5,819 | 7.9 | 7,920 | 3.8 | n/a | 256,000 |
| policy_eval | native | 4.5 | 222,814,557 | 488,064 | 3.9 | 1,456 | 0.0 | 0.0 | 4,096,000 |
| policy_eval | py | 4,272.8 | 234,041 | 10,043 | 31.5 | 17,072 | 308.5 | 1,824.0 | 4,000 |
| policy_eval | ts | 10,058.0 | 99,423 | 12,871 | 98.4 | 99,808 | 352.5 | n/a | 1,000 |
| policy_eval | rust | 11.8 | 84,691,093 | 488,704 | 3.9 | 1,440 | 0.0 | n/a | 1,024,000 |
| policy_eval | wasm | 64.2 | 15,566,687 | 5,819 | 7.9 | 7,840 | 3.8 | n/a | 2,048,000 |
| hash_bytes | native | 150.4 | 6,648,660 | 488,064 | 3.9 | 1,472 | 0.0 | 0.0 | 128,000 |
| hash_bytes | py | 54,197.0 | 18,451 | 10,043 | 31.5 | 17,168 | 308.5 | 1,824.0 | 1,000 |
| hash_bytes | ts | 49,373.0 | 20,254 | 12,871 | 98.4 | 100,032 | 352.5 | n/a | 1,000 |
| hash_bytes | rust | 2,573.5 | 388,576 | 488,704 | 3.9 | 1,440 | 0.0 | n/a | 8,000 |
| hash_bytes | wasm | 3,210.3 | 311,493 | 5,819 | 7.9 | 7,824 | 3.8 | n/a | 32,000 |
| simulate | native | 471.4 | 2,121,313 | 488,064 | 3.9 | 1,456 | 0.0 | 0.0 | 32,000 |
| simulate | py | 55,524.0 | 18,010 | 10,043 | 31.5 | 17,168 | 308.5 | 1,824.0 | 1,000 |
| simulate | ts | 29,731.0 | 33,635 | 12,871 | 98.4 | 98,944 | 352.5 | n/a | 1,000 |
| simulate | rust | 433.3 | 2,307,637 | 488,704 | 3.9 | 1,440 | 0.0 | n/a | 32,000 |
| simulate | wasm | 630.9 | 1,585,002 | 5,819 | 7.9 | 7,888 | 3.8 | n/a | 256,000 |
| batch_transform | native | 21.8 | 45,804,258 | 488,064 | 3.9 | 1,456 | 0.0 | 0.0 | 512,000 |
| batch_transform | py | 11,054.0 | 90,465 | 10,043 | 31.5 | 17,232 | 308.5 | 1,824.0 | 1,000 |
| batch_transform | ts | 21,347.0 | 46,845 | 12,871 | 98.4 | 99,856 | 352.5 | n/a | 1,000 |
| batch_transform | rust | 40.1 | 24,934,255 | 488,704 | 3.9 | 1,440 | 0.0 | n/a | 256,000 |
| batch_transform | wasm | 235.4 | 4,247,905 | 5,819 | 7.9 | 7,856 | 3.8 | n/a | 512,000 |
| loop_probe (control) | native | 0.8 | 1,211,655,081 | 488,064 | 3.9 | 1,456 | 0.0 | 0.0 | 16,384,000 |
| loop_probe (control) | py | 309.4 | 3,232,486 | 10,043 | 31.5 | 17,120 | 308.5 | 1,824.0 | 64,000 |
| loop_probe (control) | ts | 353.3 | 2,830,105 | 12,871 | 98.4 | 98,560 | 352.5 | n/a | 32,000 |
| loop_probe (control) | rust | 0.5 | 1,850,985,709 | 488,704 | 3.9 | 1,440 | 0.0 | n/a | 32,768,000 |
| loop_probe (control) | wasm | 4.6 | 218,352,891 | 5,819 | 7.9 | 7,920 | 3.8 | n/a | 32,768,000 |

## Reading the table

`artifact bytes` is the deployed artifact, which is not the same kind of
object across tiers: emitted module source for py and ts, the release
binary (driver included) for rust and native, and the .wasm module for
wasm. Compare it within a tier's row pair, not across languages.

`loop overhead ns` is tier-level: the tier's control (`loop_probe`) per
call minus the native control's per call, floored at 0. It is the emitted
loop and list-index cost over the native loop, not a claim about one
kernel.

Call overhead across a host, tier or capability boundary is not isolated
by this harness. The control crosses no boundary; `bench/extern_call_cost.py`
measures the Python tier's extern crossing, and a witnessed call path on
the other tiers would need its own runtime harness.

`capability overhead ns` is tier-level too. The Python tier's number is
the witnessed extern crossing minus its host body, from
[results/extern-call-cost.md](extern-call-cost.md); a pure kernel call
does not pay it. The other emitted tiers run pure kernels here and do not
isolate a witnessed call path, so their cell says n/a rather than
borrowing the Python number.

The decision rule these numbers serve, from [bench/README.md](../README.md):
native work is only justified when the gain survives call overhead. Under
20% over the emitted path is not worth a tier boundary; 5x to 10x is;
anything between needs the call overhead subtracted before it counts.

The ratio is the emitted path against the floor, not a verdict that a
native kernel is the right repair. Where the gap is a shape the emitter
writes badly, the repair belongs in the emitter: the Rust tier lowers
`Str.charAt` to `chars().nth(i)`, so the string-scan kernels are quadratic
there and their ratio is an emitter cost, not a property of native code.
The codegen benches under `bench/codegen/` are where that belongs.

## Findings

- py: median 360.3x the native floor across the seven kernels (range 117.8x to 952.0x).
- ts: median 546.4x the native floor across the seven kernels (range 63.1x to 2241.1x).
- rust: median 15.6x the native floor across the seven kernels (range 0.9x to 167.1x).
- wasm: median 18.3x the native floor across the seven kernels (range 1.3x to 117.5x).
- control `loop_probe` per call: native 0.8 ns, py 309.4 ns, ts 353.3 ns, rust 0.5 ns, wasm 4.6 ns.
