# raw-ts paradigm bench — hand corpus

*Does revl earn its keep?* The v1/v2/v2host variants measure which **revl
syntax** models write best. This variant measures whether **revl itself** earns
its keep: the same specs authored as **raw Cordis (TypeScript) plugins**, scored
not on compile-rate — raw TS always "compiles" — but on **lifecycle
correctness**, using the item-18 residue probe (`tools/residue-probe/`) as the
oracle. The probe mounts/unmounts each plugin N cycles on a real cordis runtime
and reports which of the four no-residue categories (registry / provisions /
effects / listeners) did not return to baseline.

The revl variants are **compile-gated**: a component that would leave residue on
unload never reaches a corpus — the revl compiler *refuses it at compile time*
(G4 + the no-residue proof, `src/revl/run.py`). The raw-ts column below is
exactly what that gate would have caught.

## Headline (measured on this hand corpus)

**4/10 (40%) of raw-TS attempts carry residue that revl would have refused at
compile time.**

- clean (no residue — what revl compiles): 6/10 (60%)
- leaked (>=1 category — revl would refuse): 4/10
- failed to mount (bad plugin — also refused): 0/10

Probe: 6 mount/unmount cycles per plugin. Reproduce (free — no model, no cost):

```sh
python3 bench/score_raw_ts.py --run hand-corpus --cycles 6
```

### which categories leaked

| leaked category | count | contract it breaks (run.py) |
|---|---|---|
| registry   | 2 | `root.registry.size == 0` |
| provisions | 3 | `root.reflect.store == {}` |
| effects    | 4 | `root.fiber._disposables == base` |
| listeners  | 1 | `root.events._hooks == base` |

### the residue-carrying cells (and the ordinary Cordis mistake behind each)

| spec | leak set | the mistake |
|---|---|---|
| `03-user-cache`    | registry, provisions, effects | audit sink mounted with `ctx.root.plugin(...)` — a sibling of the root that survives teardown |
| `05-rate-limiter`  | effects, listeners            | sweep listener bound with `ctx.root.on(...)` — its removal is collected by the never-disposed root fiber |
| `07-session-store` | provisions, effects           | service published with `ctx.root.provide(...)` — the provision lingers in the root reflect store |
| `29-mesh`          | registry, provisions, effects | the `kv` dependency brought up with `ctx.root.plugin(MemKv)` — escapes the mesh's teardown |

Each is a real, ordinary mistake — attaching work to a context that *outlives*
the plugin's own fiber — not a contrived one. The 6 clean plugins fiber-scope
every acquisition, provision, and listener (the shape revl emits,
`backends/typescript/golden/user_cache.ts`) and return to baseline over any
cycle count.

## What this corpus is, and what it is not

This is a **hand-authored** corpus (10 plugins: 6 clean, 4 leaky, spanning all
four leak categories), committed so the raw-ts scoring path is exercised
end-to-end and the 40% above is a *real probe measurement*, not a stub. It
proves the pipeline: prompt -> generation -> residue probe -> leak set ->
headline.

It is **not** the population number. The pitch line — *"Z% of raw-TS attempts
carry residue that revl would have refused at compile time"* across a full model
run of all 30 specs — requires a funded `cline` run:

```sh
python3 bench/run.py --runner cline --variants raw-ts          # all 30 specs
python3 bench/run.py --runner cline --variants v1,v2,raw-ts    # side-by-side
```

That run authors each spec with a real model against `bench/prompts/raw-ts.md`,
saves `attempt-1.ts` per spec, and scores each with this same probe. The harness
is ready to produce Z; no paid API call was made to produce this hand-corpus
figure. Once a `cline` corpus is committed, re-score it for free at any cycle
count with `python3 bench/score_raw_ts.py --run <label>`.

## Method note (honesty)

- The probe measures the four Cordis lifecycle categories the revl runtime
  proves clean. It does **not** measure host-resource leaks (an unreleased
  `Pool`/`Map` handle) — those are revl-host-stdlib-specific and out of the
  foreign-plugin contract — nor functional correctness of the plugin. A plugin
  can be probe-clean and still miss the brief; spot-check generations.
- A generation that cannot even be mounted (bad TS, throws on import) is counted
  as residue-carrying ("failed to mount"), because revl's gate would equally
  never admit it. This corpus has none.
- "revl would have refused" is the load-bearing claim: every leak category here
  maps 1:1 to a baseline the revl no-residue proof asserts on teardown, so a
  revl component reaching any of these states fails to compile/admit.
