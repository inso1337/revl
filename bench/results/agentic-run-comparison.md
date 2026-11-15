# Comparing two runs of one cell: the withdraw determinism control

A benchmark arm is measured from one run per cell, so a per-cell difference
between two arms can mean the arms differ or that this particular run of this
particular cell differed. Both produce identical log output, and anything
built on the difference inherits that ambiguity.

This document records the comparison protocol that separates the two, and the
two measurements it was run for: a determinism pair (two fresh runs of one
index) and a position-matched K-rep sweep (index i against index i). The tools
are `bench/compare_runs.py` and `bench/compare_sweep.py`.

The headline the protocol refuses is "the determinism control passed". The
report form is:

    index N reproduced across M fresh runs on all K compared outcome fields

Agreement across runs of index N is evidence about index N. It does not
generalise to the other indices, and it is not a pass for the arms.

## Field set

The pair and fresh records carry 41 fields; the older original batch carries
36. It predates `run_seq`, `proc_id`, `started_at`, `since_proc_start_s` and
`revl_tree`, so comparing the whole record across the two generations would
report a difference that is a schema change rather than a benchmark result.
The tools use these groups, taken from the records on disk:

| group | fields | role |
|---|---|---|
| identity | `model`, `revl_sha`, `arm_variant`, `rep` | must match, or the runs are not one cell and no agreement claim is made |
| advisory | `revl_tree` | a directory name, not content; checked only when both runs carry it |
| outcome | 22 fields, listed below | the agreement test |
| tokens | `tokens_in`, `tokens_new_prefill`, `tokens_out`, `write_tokens` | reported separately, never folded into the verdict |
| call sequence | `call_log` | compared as a sequence and included in the verdict |
| excluded | `run_seq`, `proc_id`, `started_at`, `since_proc_start_s`, `wall_total_s`, `wall_s`, `transcript`, `final_text`, `call_detail` | per-run metadata and bulk text |

The 22 outcome fields:

    withdrew  break_set_exact  named_breaks  named_cause  survivors_intact
    survivors_present  compiles  compile_err  exhausted  turns  tool_calls
    mutating_calls  edit_calls  load_calls  hatch_calls  scaffold_calls
    query_withdraw_calls  refusals  max_refusal_run  preflight_before_edit
    first_preflight_idx  first_mutation_idx

Three rules make the comparison mean what it says:

- **Empty in every run is excluded, not counted as agreeing.** Two empty
  fields agree perfectly and measure nothing. The tools print the excluded
  fields and the compared count, so the comparison cannot inflate its own
  field set. A comparison with no populated field is refused rather than
  reported as a vacuous reproduction.
- **Tokens never decide the verdict.** At temperature 0 the action sequence
  can reproduce exactly while the model writes a different amount of prose
  around it. A token difference is reported as a secondary observation; it is
  not evidence that two runs differed.
- **The action sequence does decide the verdict.** A different call order is a
  behavioural difference even when every counter matches. When the sequence is
  identical and only `turns` moved, the same calls were packed into a
  different number of model responses, and the tool says so rather than
  leaving it as a behaviour change.

## Freshness

The pair tool refuses the same file twice, two names for one byte-identical
file, and two records with the same recorded run stamp (`proc_id` and
`started_at`), checked pairwise among the runs that carry both fields. It
prints each run's digest and stamp, so the report shows what distinguished the
runs.

The sweep tool performs the same file and byte checks, checks the run stamps
across the two files, and prints the file digests and the process ids each file
records. The original sweep file predates the stamp fields, so the sweep can
show the fresh file's process ids but cannot compare them against the
original's; it says so.

Neither tool can prove from a record that two processes never overlapped. The
operator states that separately.

## Measurement 1: the determinism pair

Two fresh runs of index 0, each in its own output file, in separate processes
about eighteen minutes apart (19:40:29 and 19:58:46). Same model
`Qwen3.8-27B-4bit-MLX`, revl sha `cca549ac4ba7`, arm `warm`, rep 0. The run
stamps and file digests differ.

| field group | result |
|---|---|
| identity | all four fields equal |
| outcome | 21 of 21 compared fields reproduced |
| excluded | `compile_err` empty in both runs |
| call sequence | identical, 8 calls |
| tokens | `tokens_new_prefill` moved 23358 to 3281; `tokens_in`, `tokens_out` and `write_tokens` identical |
| wall time (excluded) | 752.4 s against 285.1 s |

Report: **index 0 reproduced across two fresh runs on all 21 compared outcome
fields and an identical call sequence**. The token move and the 2.6x wall-time
difference are reported beside the verdict, not in it. Wall time alone would
have called these two runs different.

## Measurement 2: the position-matched K-rep sweep

The original eight-rep sweep (2026-10-17) against a fresh eight-rep sweep run
two days later, compared index to index. The fresh sweep ran from 20:04 to
22:14 in two processes, the second resuming after the first. Same model, sha
and arm.

| measure | result |
|---|---|
| positions present in both sweeps | 0 through 7 |
| call sequence | identical at 8 of 8 positions |
| `tool_calls` | 8 at 8 of 8 positions, both sweeps |
| `break_set_exact` | true at 8 of 8 positions, both sweeps |
| compared outcome fields | reproduced at 6 of 8 positions |
| positions 4 and 7 | differ on `turns` only, 8 against 7, with identical call sequences |
| each position | `compile_err` empty in both, excluded from the 22-field set |
| tokens | moved at every position, reported separately |

Report: **6 of 8 positions reproduced across the two sweeps on all 21 compared
outcome fields**, and separately **8 of 8 positions reproduce on the call
sequence**, `tool_calls` and `break_set_exact`. The two positions that moved
differ only in how many model responses carried the same eight calls: one
response carried two calls instead of one. That is a packing difference, not a
behavioural one.

The sweep exits 1 for this result, not 0. A bare "6 of 8" is not the whole
count: the same data is 8 of 8 on the sequence and 6 of 8 on the full field
set, and only the second can be called the control passing. It did not.

## What this does not show

- One cell, one model, one sha. Agreement here says nothing about another
  cell, another arm or another model.
- Two runs of index 0 are evidence about index 0 only. The sweep is the part
  that covers the other positions.
- No field in the records identifies the system prompt. The identity check
  therefore establishes the model, compiler sha, arm and rep, not the prompt.
  The prompt was confirmed separately at run time; a record that carries a
  prompt digest would remove this assumption.
- The source records are not committed here. This page is a receipt for runs
  whose bytes are identified below, not a re-runnable corpus.
- Freshness is checked against files and recorded stamps, not against the
  machine. The original sweep file records no run stamps, and an overlapping
  process that wrote neither is invisible to both tools.

## Source records

| record | sha256 |
|---|---|
| `pair_qwen3_8_27b_4bit_mlx_0_a.jsonl` | `c7a80e2a11cd8f5580141bfd1a41f40489dac2e13c1edc8c5587c18ee7c8ef6a` |
| `pair_qwen3_8_27b_4bit_mlx_0_b.jsonl` | `a3bb44dc99fdd7b4005e0a56c0423e4e3f3620a9d0d037430628ccddd1aaeda7` |
| `warm_k8x_qwen3_8_27b_4bit_mlx_cap6.jsonl` | `907f9a51c6cd29414695f42c8da3abd35aaa54b9088c7625df9ece2ef5e0d352` |
| `sweep_qwen_8.jsonl` | `23e99b07c03e85ba4100be6ddde7d97f5f9117d14f03d08d2fac419f3a97c9a9` |

## Re-running the comparison

The pair:

    python3 bench/compare_runs.py --index 0 \
        --runs pair_a.jsonl pair_b.jsonl

The sweep:

    python3 bench/compare_sweep.py \
        --orig warm_original.jsonl --fresh sweep_fresh.jsonl

Both exit 0 only when every compared field or position reproduced. Exit 1
means something moved, and the per-field or per-position lines say what. Exit
2 means the input cannot support a claim: the same file or identical bytes
twice, a repeated run stamp, an identity mismatch or absent identity field, an
ambiguous rep, a field set with nothing populated, or a sweep with positions
present in only one file.

To measure a fresh pair, run one index twice into two new output files. The
work directory is keyed on the rep index alone, so the first run's directory
must be moved aside before the second starts; otherwise the second run
overwrites the run it is being compared against. Run them sequentially, and
never point both arguments at one path.
