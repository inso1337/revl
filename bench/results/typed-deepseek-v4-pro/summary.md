# syntax-2.0 acceptance benchmark — run summary

runner: `cline`
max iterations: 3

| variant | specs | first-pass compile | green ≤ max iters | mean iters-to-green | cost |
|---|---|---|---|---|---|
| v1 | 30 | 27/30 (90%) | 29/30 | 1.07 | $0.04 |
| v2 | 30 | 20/30 (67%) | 29/30 | 1.31 | $0.08 |
| v2host | 30 | 18/30 (60%) | 30/30 | 1.40 | $0.09 |

Unsolved: 25-checksum-log/v2, 26-log-rotator/v1
