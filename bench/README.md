# The syntax-2.0 acceptance benchmark

The experiment prescribed by [docs/syntax-2.0.md §10](../docs/syntax-2.0.md):

> 30 component specs × {1.x syntax, 2.0 syntax, 2.0+host-blocks} × several
> models; measure first-pass compile rate and iterations-to-green. The syntax
> ships only if the benchmark agrees it should.

## Layout

- `specs.json` — 30 component specs, each expressible in all three variants.
  A spec gives the model a brief plus service interfaces to use verbatim;
  scoring is compile-rate, so interfaces are pinned but implementations are
  the model's.
- `prompts/v1.md` — the 1.x grammar as a system prompt (components, effects,
  emissions, minimal expressions).
- `prompts/v2.md` — the full 2.0 grammar (fns, types, match, stdlib, tests).
- `prompts/v2host.md` — addendum concatenated onto v2 for the `v2host`
  variant (extern host blocks).
- `run.py` — orchestrator: prompt → model → extract code → compile with the
  real checker → on error, feed the compiler message back and retry (up to
  `--max-iters`, default 3). **Costs real money.**
- `rescore.py` — recompiles the *already committed* generations against the
  current checker. No model, no provider, no cost. This is how a language
  change is measured against a fixed corpus.
- `results/<label>/` — per-attempt `.rvl` files, `results.jsonl`,
  `summary.md`. Committed runs are the record; the directory is not
  gitignored on purpose.

## Running it

Models are driven through the [`cline`](https://cline.bot) CLI
non-interactively, using whatever provider it is configured with:

```bash
python3 bench/run.py --runner mock                     # pipeline self-check, no model
python3 bench/run.py --runner cline --specs 3          # cheap pilot
python3 bench/run.py --runner cline                    # full 30 × 3 matrix
python3 bench/run.py --runner cline -m some-model      # another model (cline -m)
```

Useful flags:

- `--compiler-root DIR` — score against `DIR/src/revl` instead of the live
  tree (export a pinned commit with `git archive <sha> src | tar -x -C DIR`).
  Use this whenever the working tree is mid-edit, and record the sha with
  the run.
- `--variants v1,v2,v2host`, `--specs all|N|id,id`, `--max-iters`,
  `--timeout`, `--label`.
- `--inline-system` — if a provider misbehaves with cline's `-s` system
  override, merge the grammar into the user prompt instead.

Cost: with DeepSeek V4 Pro the full matrix (~90–150 calls) lands around
**$0.50**. Runtime is dominated by model latency, roughly 10–30 minutes.

## Re-scoring without spending anything

```bash
python3 bench/rescore.py                       # typed run, attempt-1
python3 bench/rescore.py --run all             # both model runs
python3 bench/rescore.py --compiler-root DIR   # score a pinned export instead
```

It prints, per run: per-variant first-pass compile counts, a failure taxonomy
bucketed by the compiler's own diagnostic code/category, and the list of
failing cells. Every table names the compiler sha it was measured at, so a
published number can be traced back to a commit. `--json` writes per-cell
records for further analysis.

The headline is `attempt-1` only — the file the model produced before it ever
saw a compiler message. Later attempts are the error-feedback loop.

Note what a re-score can and cannot say. It measures **the corpus against a
compiler**, so it is the right tool for "did this language change break
previously-accepted code?". It is *not* a current model-capability figure: the
generations were produced against older prompts and an older checker, and no
prompt fix can retroactively improve them. Only a fresh `run.py` does that.

## Reading the results

`summary.md` per run reports, per variant: first-pass compile rate,
green-within-max-iters, mean iterations-to-green, and cost. The §10 question
is comparative: does 2.0 syntax (the TS-subset strata) compile *better or no
worse* than the 1.x surface in models' hands, and do host blocks help or
hurt? First-pass rate is the headline; iterations-to-green measures how well
the compiler's diagnostics convert misses into one-shot corrections.

Caveats worth keeping in mind:

- Compile-rate is necessary, not sufficient — a compiling file can still miss
  the brief's intent. Spot-check generations (they are all saved).
- The v1 variant runs against the same 2.0 compiler (the 1.x surface is a
  subset), so v1 numbers measure the *syntax* under identical checking, which
  is exactly the controlled comparison §10 wants.
- One model ≠ "several models". Re-run with `-m`/`-P` per model cline can
  reach and compare summaries.
- A spec is only meaningful if it is *satisfiable* under the current checker.
  Seven specs (`03`, `15`, `16`, `17`, `22`, `27`, `29`) pinned an operation as
  plain `fn` while the brief instructed the model to emit inside it; when G4
  gained its upper-bound direction (a service declaration bounds what its
  providers may do) those specs became impossible to satisfy without
  contradicting the "use these interfaces verbatim" instruction. They now
  declare `emission fn`. When a language rule changes, re-check the specs
  against it — a benchmark that cannot be passed measures nothing.
