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
- `prompts/raw-ts.md` — the **paradigm baseline**: author each spec as a raw
  Cordis (TypeScript) plugin, no revl. Same briefs, same pinned service
  interfaces. Scored on lifecycle correctness, not compile-rate (see below).
- `run.py` — orchestrator: prompt → model → extract code → compile with the
  real checker → on error, feed the compiler message back and retry (up to
  `--max-iters`, default 3). **Costs real money.** The `raw-ts` variant skips
  the compile/retry loop and is probe-scored instead (`score_raw_ts.py`).
- `rescore.py` — recompiles the *already committed* revl generations against
  the current checker. No model, no provider, no cost. This is how a language
  change is measured against a fixed corpus.
- `demand.py` — refusal telemetry. Extends `rescore.py`'s failure taxonomy
  into a **ranked demand table**: what the models keep reaching for and being
  refused (unknown stdlib methods, invented syntax forms, missing types),
  ranked by frequency across the committed corpora. Orders the stdlib/syntax
  roadmap by *measured* demand. Free — reads committed data, no model.
- `score_raw_ts.py` — the raw-ts scoring + re-score path. Mounts/unmounts each
  committed `raw-ts/attempt-1.ts` N cycles via the item-18 residue probe
  (`tools/residue-probe/`) and reports the leak set. Free — the probe calls no
  model. Imported by `run.py` to score fresh generations, and runnable
  standalone to re-score a committed corpus.

### The paradigm variant — bench the paradigm, not just the syntax

v1/v2/v2host ask *which revl syntax do models write best* (compile-rate). The
`raw-ts` variant asks the prior question: *does revl earn its keep at all?* The
same specs are authored as raw Cordis plugins and scored on **lifecycle
correctness**, because raw TS always "compiles" — the question is what it
*leaks* on unload. The revl variants are compile-gated (a residue-carrying
component is refused at compile, G4 + the no-residue proof), so the raw-ts leak
rate is precisely what that gate catches:

```sh
python3 bench/run.py --runner cline --variants raw-ts          # 30 specs, real model
python3 bench/score_raw_ts.py --run <label> --cycles 6         # free re-score
```

The headline it produces: *"Z% of raw-TS attempts carry residue that revl would
have refused at compile time."* A committed hand corpus
(`results/hand-corpus/`, 6 clean + 4 leaky plugins) exercises the whole path
and yields a real 40% on that corpus; the population Z needs a funded `cline`
run. Prereq: `cd backends/typescript && npm install` once (the probe reuses that
one cordis install).
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

## Turning refusals into a feature-request queue

The failure taxonomy answers *what broke*. `demand.py` answers the next
question — *what do the models keep reaching for that revl won't give them* —
and ranks it, so the stdlib/syntax roadmap is ordered by measured demand
rather than by guess (the same "measured, not assumed" move the `Int32` entry
made). Every refused attempt in `results/` is one data point of demand.

```bash
python3 bench/demand.py                       # rank across both model runs
python3 bench/demand.py --attempts all        # every attempt (the default)
python3 bench/demand.py --kind stdlib         # only stdlib-method demand
python3 bench/demand.py --json demand.json    # machine-readable ranking
```

Two things separate the demand table from the taxonomy:

- **It sub-classifies below `(code, category)`.** The taxonomy lumps the
  refusal `no builtin method take` and the parse error `found 'kv'` into one
  `(G6, guarantee)` bucket; the miner splits them into a *stdlib-method* demand
  for `take` and a *syntax-form* demand for `kv`, because they order two
  different roadmaps.
- **It mines the reached-for symbol** out of each message — the method, the
  syntax token, the type name — so a row reads "`take` was reached for N
  times", the thing you would add, not "a G6 fired".

Each row is one reached-for symbol: its *kind* (`stdlib-method`,
`syntax-form`, `host-method`, `missing-type`, or a `guarantee:<code>` bucket
for refusals with no symbol to mine), a refusal count, the contributing
diagnostic codes, and a representative example. Rows are ranked by descending
count with a total-order tie-break, so the ordering is deterministic. The
`Roadmap-actionable rows` block at the bottom filters to the stdlib / syntax /
host / type kinds — the ones an author can act on directly. Like `rescore.py`,
the header names the compiler sha the demand was measured at.

### Opt-in second source: the live MCP session

The committed corpus is the always-on source. A live MCP session emits the
same structured diagnostics (`revl_check`/`revl_load` return them), and an
operator can capture that stream and fold it in as a **second** demand source:

```bash
python3 bench/demand.py --mcp-diagnostics session.jsonl
```

It is **off by default and read-only** — it never touches the live session,
only a capture file (JSONL or a JSON array; it accepts a bare diagnostic, a
`report()` `{diagnostics: […]}` document, or a restore-error `{diagnostic: …}`
shape). Refusals that came from the MCP source are tagged `(mcp)` in the table
and carry a `sources` breakdown in the JSON, so corpus demand and live demand
stay distinguishable.

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
