# residue-probe — a no-residue lifecycle linter for foreign Cordis plugins

`revl run` *proves* no-residue on exit for **revl-authored** compositions: after
teardown it asserts the Cordis runtime is back to the pre-load baseline in four
observable categories (`src/revl/run.py`, `_Driver._teardown`). This tool points
the **same contract** at plugins revl did **not** author — any existing Cordis
(TypeScript) plugin — by mounting and unmounting it *N* times on a real
`cordis` runtime and reporting what did not return to baseline.

The value is a lifecycle linter for the whole Cordis ecosystem **with zero
language adoption**: you don't write a line of revl to get the guarantee checked
against your plugin. It also generates the evidence a later bench
(roadmap item 20) and the paper need — *"N plugins × M cycles, X% leak"* — with
this probe as the pass/fail oracle.

## The contract it enforces (and how it maps to the py baseline proof)

`run.py` snapshots four things before load and asserts each returns to baseline
on teardown. This probe snapshots the same four off the live root `Context`,
using the exact field access the TS backend's own R4 oracle uses
(`backends/typescript/runtime.ts` `snapshotRuntime`):

| category   | run.py (py runtime)                         | probe (TS runtime)                 |
|------------|---------------------------------------------|------------------------------------|
| registry   | `root.registry.size == 0`                   | `ctx.registry.size`                |
| provisions | `root.reflect.store == {}`                  | `ctx.reflect.store` (impl names)   |
| effects    | `root.fiber._disposables.length == base`    | `ctx.fiber.getEffects().length`    |
| listeners  | `root.events._hooks == baseline`            | `ctx.events._hooks`                |

The py driver is the **model**, not code to copy: this is new TS work running
the same before/after contract against the real Cordis runtime, *N* cycles
instead of one. A category is reported as leaked when its post-cycle value
differs from the baseline snapshot; any leak makes the run exit non-zero.

**vs. the py proof — what the TS runtime exposes.** The correspondence is
essentially 1:1. `ctx.registry.size` / `ctx.reflect.store` / `ctx.events._hooks`
line up name-for-name with the py fields. The one rename is *effects*: py reads
`fiber._disposables.length` (a private field); the TS runtime exposes a public
`fiber.getEffects()` returning the effect list — so the probe counts that. Both
are "how many disposers is the root fiber still holding." The TS backend's
`snapshotRuntime` additionally tracks `liveHostResources` (open `Pool`/`Map`
handles); that is revl-host-stdlib-specific and a foreign plugin never touches
it, so the probe omits it and stays purely on the four Cordis categories.

## Usage

Prereq: the Cordis install the TS backend already ships. Once —

```sh
cd ../../backends/typescript && npm install   # pinned cordis 4.0.0-rc.8
```

The probe **reuses that single install** (it symlinks it in as its own
`node_modules` on first run; the link is gitignored). It does not add a second
`node_modules`. Node >= 23.6 (erasable-syntax TypeScript, no build step).

```sh
# probe any module that exports a Cordis plugin
node run.mjs <module> [export] [--config f.json | '{...}'] [--cycles N] [--warmup K] [--json]

# examples
node run.mjs fixtures/clean-plugin.ts              # -> no residue, exit 0
node run.mjs fixtures/leaky-plugin.ts --cycles 10  # -> RESIDUE LEFT, exit 1
node run.mjs ../../backends/typescript/golden/user_cache.ts UserCache
```

- `[export]` — the named export to probe (default `plugin`).
- `--config` — JSON file path or inline JSON passed as the plugin's config;
  falls back to the module's `config` export.
- `--cycles N` — measured mount/unmount cycles (default 5).
- `--warmup K` — un-measured cycles run **before** the baseline snapshot
  (default 0). `0` mirrors `run.py` exactly: a one-time first-mount cost counts
  as residue. Use `>0` to tolerate a fixed first-mount offset and flag only
  per-cycle **growth** (an unbounded leak).
- `--json` — emit the machine-readable `ProbeReport` (baseline, final,
  per-cycle snapshots, per-category leak set) for a bench to consume.

Exit code: **0** = no residue, **1** = something leaked, **2** = probe error.

## Fixtures

- `fixtures/clean-plugin.ts` — everything scoped to the plugin's own fiber
  (`ctx.on`, `ctx.provide` yielded from `ctx.effect`); returns to baseline in
  all four categories over any cycle count.
- `fixtures/leaky-plugin.ts` — real, ordinary Cordis mistakes: a listener bound
  to `ctx.root` and a whole plugin mounted via `ctx.root.plugin(...)`, both of
  which escape the plugin's own teardown. Leaves residue in **all four**
  categories (provisions, registry, effects fixed offsets; listeners + effects
  grow every cycle — an unbounded leak).

## Tests

```sh
npm test        # node test.mjs — no vitest, no extra install
```

Asserts the clean fixture reports **0 leaks**, the leaky fixture reports the
expected **{registry, provisions, effects, listeners}** leak set (with the
escaped `leaked-svc` provision named and the listener count shown growing per
cycle), and an in-tree revl-emitted plugin (`golden/user_cache.ts`) leaves no
residue under the same harness. Matches how `backends/typescript` runs its own
checks — a single `npm test` in this directory.

## How a later bench (item 20) consumes this

`probe(component, config, opts)` (in `probe.ts`) returns a `ProbeReport` with the
baseline, the per-cycle snapshots, and the per-category leak set; `run.mjs
--json` is the same report on stdout with a 0/1 exit code. A bench sweeps a
corpus of real Cordis plugins, calls the probe per plugin, and tallies the leak
set — turning the paper's *"N plugins × M cycles, X% leak"* line into a
reproducible measurement with this probe as the oracle for each cell.
