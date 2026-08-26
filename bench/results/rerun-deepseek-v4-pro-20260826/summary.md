# syntax-2.0 acceptance benchmark — run summary

runner: `cline` · provider: `deepseek` · model: `deepseek-v4-pro` · date: 2026-08-26
compiler (live tree): `7697f09`
max iterations: 3

**PARTIAL RUN — 25 of 30 specs, deepseek-v4-pro.** A secondary datapoint, not
a headline. The full matrix is 30 specs × 3 variants = 90 cells; this run was
stopped after 73 cells to cap model spend, so specs 25–30 are not complete for
all variants (v1 reached spec 25; v2/v2host reached spec 24). The comparable
figure below is the **common completed subset, specs 01–24 across all three
variants** (n=24 each, apples-to-apples). No fabricated cells — every number is
measured.

## revl variants — common subset (specs 01–24), compile-gated

| variant | specs | first-pass compile | green ≤ 3 iters | mean iters-to-green | mean tokens-to-green | cost |
|---|---|---|---|---|---|---|
| v1 | 24 | 23/24 (96%) | 24/24 | 1.04 | 1132 | $0.031 |
| v2 | 24 | 23/24 (96%) | 24/24 | 1.04 | 2671 | $0.061 |
| v2host | 24 | 22/24 (92%) | 24/24 | 1.08 | 3751 | $0.090 |

Every completed cell reached green within 3 iterations; 0 unsolved.

## all completed cells (uneven n — v1:25, v2:24, v2host:24), for the record

| variant | specs | first-pass compile | green ≤ 3 iters | mean iters-to-green | mean tokens-to-green | cost |
|---|---|---|---|---|---|---|
| v1 | 25 | 24/25 (96%) | 25/25 | 1.04 | 1143 | $0.032 |
| v2 | 24 | 23/24 (96%) | 24/24 | 1.04 | 2671 | $0.061 |
| v2host | 24 | 22/24 (92%) | 24/24 | 1.08 | 3751 | $0.090 |

total as-run cost across all 73 completed cells: $0.1837

