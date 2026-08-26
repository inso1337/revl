# syntax-2.0 acceptance benchmark — run summary

runner: `local` · model: `openai/gpt-oss-20b`
max iterations: 1

## revl variants — compile-gated (residue refused at compile)

| variant | specs | first-pass compile | green ≤ max iters | mean iters-to-green | mean tokens-to-green | cost |
|---|---|---|---|---|---|---|
| v1 | 30 | 24/30 (80%) | 24/30 | 1.00 | 334 | $0.00 |
| v2 | 30 | 24/30 (80%) | 24/30 | 1.00 | 364 | $0.00 |
| v2host | 30 | 18/30 (60%) | 18/30 | 1.00 | 452 | $0.00 |

Unsolved: 02-pg-pool/v1, 02-pg-pool/v2, 02-pg-pool/v2host, 04-migrator/v1, 10-metrics/v2host, 11-outbox/v2, 13-provider-consumer-pair/v2host, 14-health-check/v2host, 15-search-index/v2host, 16-notifier/v2host, 19-lock-manager/v1, 21-two-stage-boot/v1, 22-cascade/v2, 22-cascade/v2host, 24-normalizing-cache/v1, 24-normalizing-cache/v2, 24-normalizing-cache/v2host, 25-checksum-log/v2, 26-log-rotator/v2host, 27-inventory/v2host, 29-mesh/v1, 29-mesh/v2host, 30-saga-transfer/v2, 30-saga-transfer/v2host
