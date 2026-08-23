# Admission latency

One honestly-measured number for revl's in-memory admission round-trip
(roadmap item 16). The gate runs per candidate component, per generation,
inside an agent loop, so this is the per-candidate cost of `compile_source(...,
manifest=running)`: parse + check/lower the candidate + link it against the
running composition + `refuse_admission`. No disk I/O, network, or toolchain is
in the timed section.

## Result

| measurement | median | p90 | p99 | min | samples |
|---|---|---|---|---|---|
| **compile + admit (round-trip)** | **0.165 ms** | 0.179 ms | 0.659 ms | 0.158 ms | 4000 |
| compile only (no gate, baseline) | 0.149 ms | 0.160 ms | 0.589 ms | 0.144 ms | 4000 |

**Admission costs ≈ 0.16 ms** (median) per candidate component on
the machine below. The gate itself — linking the candidate against the running
composition and refusing holes — adds ≈ 0.015 ms over compiling the
component alone (the "compile only" row is a standalone-valid twin with `Store`
inlined; it carries one extra service decl, so it slightly over-states compile
and under-states the gate share — treat the split as a guide, the round-trip as
the headline).

## Methodology

- **Machine:** Darwin 25.2.0 · Apple M1 Max · Python 3.14.3
- **Scenario:** running composition = `Store` provider `Kv` + consumer `App`;
  candidate = `CacheLayer` that *requires* the running `Store` and *provides* a
  new `Cache`. Requires/provides + G2/G3 across running and candidate — the real
  gate, not an empty component. Defined in `bench/admission_latency.py`.
- **Warm:** the running composition is compiled once outside the timer (an agent
  holds it across generations); 100 warm-up round-trips precede timing so
  one-time import cost is excluded.
- **Distribution:** 4000 timed iterations; median/p90/p99 reported, not a
  single lucky sample.
- **Determinism:** pure `compile_source` on in-memory strings — no disk I/O in
  the timed section, no network, no toolchain.

## Re-run

```
python3 bench/admission_latency.py            # print the figures
python3 bench/admission_latency.py --write    # rewrite this file
```

A guard test, `tests/test_admission_latency.py`, runs a small iteration count in
CI and asserts the round-trip completes and returns an admitted result (it does
*not* assert a hard wall-clock number — machines vary — only a generous
order-of-magnitude ceiling that a ~10x regression would trip).
