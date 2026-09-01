# syntax-2.0 acceptance benchmark — run summary (Opus 4.8 PILOT)

runner: `opus-4-8-inline` (no cline/claude CLI wired; the model-under-test
generated the .rvl directly in-agent) · model: `opus-4-8`
max iterations: 3
scope: PILOT — first 3 specs x {v1, v2, v2host} = 9 cells

Every cell was compiled against the live checker (`src/revl`, `compile_source`),
using the exact `compile_check` logic from `bench/run.py`. Every number below is
what actually happened against that checker.

## revl variants — compile-gated (residue refused at compile)

| variant | specs | first-pass compile | green <= max iters | mean iters-to-green |
|---|---|---|---|---|
| v1     | 3 | 3/3 (100%) | 3/3 | 1.00 |
| v2     | 3 | 3/3 (100%) | 3/3 | 1.00 |
| v2host | 3 | 3/3 (100%) | 3/3 | 1.00 |

Unsolved: none.

## Per-cell results

| spec | variant | first_pass | iters_to_green | final |
|---|---|---|---|---|
| 01-kv-provider | v1     | true | 1 | green |
| 01-kv-provider | v2     | true | 1 | green |
| 01-kv-provider | v2host | true | 1 | green |
| 02-pg-pool     | v1     | true | 1 | green |
| 02-pg-pool     | v2     | true | 1 | green |
| 02-pg-pool     | v2host | true | 1 | green |
| 03-user-cache  | v1     | true | 1 | green |
| 03-user-cache  | v2     | true | 1 | green |
| 03-user-cache  | v2host | true | 1 | green |

## Caveats (read before trusting these numbers)

- **Single-context run inflates first-pass.** All 9 cells were generated in ONE
  agent context, so there is cross-cell learning: seeing spec 01 pass informs
  spec 02/03, and seeing v1 pass informs v2/v2host of the same spec. A rigorous
  full run must use a FRESH context per cell (one subagent per cell) to remove
  this leakage.
- **Easy-spec sample.** Specs 01-03 are the three specs that map most directly
  onto the worked examples inside the variant prompts themselves (the v1/v2
  prompts literally show MemKv, PgDatabase, and UserCache). A 100% first-pass
  rate here is expected and is NOT representative of the full 30-spec matrix.
  The deepseek baseline over all 30 specs was v1 93% / v2 57% / v2host 43% first
  pass — the harder specs (host computation, multi-component cascades, saga
  ordering) are where variants separate.
- **No cost/token telemetry.** Because generation was in-agent rather than via
  cline, per-attempt `cost`, `duration_s`, and `output_tokens` are null. Only
  compile-rate and iterations-to-green are real measurements here.

## Read

On this 9-cell easy slice Opus 4.8 hits a first-pass compile ceiling (100%)
across all three variants, so the pilot does NOT discriminate v1 vs v2 vs
v2host. That is a property of the sample, not evidence they are equal. The
harness itself is validated: prompts load, generation lands, the live checker
scores, and the on-disk layout matches the existing `results/*` runs. To
actually measure whether 2.0 syntax helps Opus, scale to the full 30x3 with a
fresh context per cell.
