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
- `tokens.py` — the **token economy's measurement** (item 50): records
  *tokens-to-green* — output tokens spent per admitted component — beside the
  iterations-to-green everything else tracks. Recomputes green against the
  current checker (like `rescore.py`) and sums the output tokens the model
  emitted across every attempt up to the admitted one. Free — reads committed
  data, no model. Committed number: `results/tokens-to-green.md`; the
  protocol-side companion analysis: `results/token-surface-audit.md`.
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

## The token economy: tokens-to-green (measured, not assumed)

Iterations-to-green counts *turns*; it does not count *spend*. A two-iteration
component that emitted 900 output tokens cost more than a three-iteration one
that emitted 300. Item 50 makes revl the cheapest language for an agent to
write, and its house rule is *measured, not assumed* — so before any
optimization can claim it pays, there has to be a number it moves. That number
is **output tokens spent per admitted component**, across *all* attempts up to
admission (retries are not free):

```bash
python3 bench/tokens.py                    # both model runs
python3 bench/tokens.py --run typed-deepseek-v4-pro
python3 bench/tokens.py --json tokens.json # machine-readable
```

It is recorded in two places, and is explicit about which is real:

- **In the committed corpora (free re-score):** `tokens.py` tokenises the
  committed generation files (`attempt-N.rvl`) with a deterministic, dependency-
  free **BPE-proxy** (`count_tokens`) and sums to green. The *artefacts* are
  real; the *tokeniser* is a proxy, because the model's own output-token count
  was never recorded for these corpora. Alongside it surfaces the **real as-run
  dollar cost** from the committed `cost_total`. Green is recomputed against the
  current checker, so the number tracks the language exactly as first-pass rate
  does. Committed snapshot: `results/tokens-to-green.md`.
- **In future funded runs (exact):** `run.py` now captures cline's
  `output_tokens` per attempt into `results.jsonl`, and writes a
  `tokens_to_green` summary row + a *mean tokens-to-green* column into each
  run's `summary.md`. Where a committed cell carries a recorded `output_tokens`,
  `tokens.py` uses it verbatim instead of the proxy — so the next paid run
  upgrades the number from proxy to exact with no code change.

The committed figure today: **mean ~149 est. output tokens-to-green** across 136
admitted components (real as-run cost $0.29). The costliest cells and the
per-variant breakdown are in `results/tokens-to-green.md`.

This is only the *generation* half of the token economy. The larger,
protocol-side half — tokens an agent burns re-sending source and running chatty
MCP verb sequences — is ranked in `results/token-surface-audit.md`, an
analysis-only audit cross-referenced with the item-38 demand ranking (demand =
what agents retry; tokens = what retries cost). That audit is the map of *where
the spend goes*; the optimizations item 50 will weigh against this metric (a
compound one-intent verb, a terser authoring wire-form, `revl_edit` structured
patches) each have to move tokens-to-green here before they ship — no trick
ships on taste. Where the paying tricks eventually land is the "How you're
measured" contract in `../docs/guide-ai-agents.md`; this metric is the scale.

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

## Scaffold-with-holes vs whole-component (design note, item 32)

`tokens.py` measures **tokens-to-green** for the whole-component loop:
generate a plausible file, let the checker refuse, regenerate. Typed holes
(docs/holes.md) enable a second loop worth measuring against it — **scaffold
then fill**: write the skeleton with a `hole` at every unknown expression, then
close them one at a time, each fill constrained by the hole's *fill spec*
(docs/holes.md §8: expected type, emission bound, in-scope bindings, reachable
service signatures).

The comparison this note proposes — not yet a wired variant, so `demand.py`
and `tokens.py` are untouched — is a fair-billing A/B on the existing `specs`
corpus:

- **Whole-component (baseline):** today's `tokens.py` path — output tokens
  summed across every regenerate iteration until the file admits.
- **Scaffold-then-fill:** bill the skeleton once, then bill each fill turn.
  Every turn is prompted with only that hole's fill spec, not the whole file,
  so the accounting question is whether the specs' constraint (most wrong
  fills are unrepresentable) buys back the per-hole prompt overhead.

Two quantities decide it, both already derivable from committed data plus a
fill-loop harness that reuses `run.py`'s scoring:

1. **tokens-to-green**, summed the same way `tokens.py` sums it, so the two
   loops are billed identically and the numbers are directly comparable; and
2. **first-fill validity** — the share of fills that admit on the first try,
   the fill-loop analogue of first-pass compile-rate, which is where a spec
   that makes wrong answers unrepresentable should show up.

The prediction under test: scaffold-then-fill trades more turns for far fewer
wasted output tokens per turn, so tokens-to-green drops on the specs with
several independent unknowns and is roughly flat on single-expression bodies
where there is nothing to scaffold. Wiring it is a `run.py` variant plus a
fill-loop billing pass; it is out of scope here, where the deliverable is the
fill spec itself.
