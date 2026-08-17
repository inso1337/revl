# syntax-2.0 acceptance benchmark — run summary

runner: `cline`
max iterations: 3

| variant | specs | first-pass compile | green ≤ max iters | mean iters-to-green | cost |
|---|---|---|---|---|---|
| v1 | 30 | 28/30 (93%) | 29/30 | 1.03 | $0.05 |
| v2 | 30 | 17/30 (57%) | 29/30 | 1.41 | $0.11 |
| v2host | 30 | 13/30 (43%) | 28/30 | 1.57 | $0.10 |

Unsolved: 22-cascade/v2host, 26-log-rotator/v1, 29-mesh/v2, 29-mesh/v2host
