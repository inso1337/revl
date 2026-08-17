# revl

A research language for **spatiotemporal composability**: components that can
be loaded, unloaded, and hot-swapped in a running system, where "unloading
leaves no residue" and "dependencies stay coherent" are **compile-time
guarantees**, not runtime discipline.

revl is the language-level realization of the paradigm formalized in
[*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper)
and implemented as a library by [Cordis](https://github.com/cordiverse/cordis).
The one-line pitch: **Cordis has revertible effects as a discipline; revl makes
them a type system** — the same jump C++ RAII made to become Rust's ownership.
What this is *for* — and the honest scope of the "future of programming" claim
— is [docs/vision.md](docs/vision.md).

```revl
component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
    }
  }
}
```

- Undeclared access won't compile. Mutations without an inverse (or an
  explicit `emit` admission of irreversibility) won't compile. Dependency
  cycles and provision conflicts are rejected at link time. Teardown cannot
  register effects, by construction.
- **Type-safe and null-safe.** Bidirectional checking, sound where declared:
  service and function call sites are checked against their signatures,
  provide-methods inherit the service's types (surface names, service
  signature), and returns are verified. There is no `null` in the type
  system — absence is `Opt[T]`; `T` flows into `Opt[T]` but never silently
  back out, and the diagnostic says to unwrap with `match` or `??`. The
  unchecked remainder is enumerated, not implied: host-valued objects and
  the extern boundary, both on the G8 audit surface.
- Backends: [cordis-py](https://github.com/geohotstan/cordis-py) (reference),
  [cordis](https://github.com/cordiverse/cordis) (TypeScript), the
  cordis-wasm substrate, plus first [cordis-rs](https://docs.rs/cordis-rs)
  (Rust) and [cordis4j](https://github.com/1na-ko/cordis4j) (Java) spikes —
  one language, five tiers ([docs/vision.md](docs/vision.md)). See
  [DESIGN.md](DESIGN.md) for the full design, the checked-guarantees table,
  and why raw native codegen is deliberately a non-goal.

**Status: v1.** The pipeline runs end-to-end on **five backends** — three
runnable (**cordis-py**, **cordis** (TS), and the **cordis-wasm substrate**,
[backends/wasm/](backends/wasm/)) plus first **cordis-rs** (Rust) and
**cordis4j** (Java) spikes ([docs/v2.0-roadmap.md](docs/v2.0-roadmap.md)) —
`python -m revl compile` takes `.rvl` sources through parse → check →
link → IR ([docs/backend-ir-v1.md](docs/backend-ir-v1.md)), and the
emitters produce runnable components. On the wasm substrate, confinement is
enforced by the sandbox and `effect/undo` compiles to a state machine with
physical partial rollback. Divert-during-`await` semantics (A1) are verified
on all three runnable backends. `python -m revl audit` prints a
composition's manifest and G8 boundary surface; `compile_files(...,
manifest=running)` is the runtime-admission gate. The rejection suite in
[examples/rejections/](examples/rejections/) is the checker's executable
spec, and [demo/](demo/) is a live file-watching hot-swap loop — edit a
`.rvl`, watch the running system recompile and swap it. The 2.0 language
below builds on this frozen core.

**Status: 2.0.** The full language of
[docs/syntax-2.0.md](docs/syntax-2.0.md) is implemented on top of the v1
core: a TypeScript-subset stratum of pure functions (`fn`, `var`/`while`/
`for-of` local mutation, arrow lambdas with by-value capture), types and
ADTs with exhaustiveness-checked `match`, modules (`use`/`pub`), template
strings (replacing 1.x `$name` interpolation — the compiler rejects the old
form with a migration hint), `extern` host blocks with typed boundaries on
the G8 audit surface, in-file `test` blocks, realms & interception
(`isolate`/`intercept`, [docs/design-v2-realms.md](docs/design-v2-realms.md)),
and a specified [stdlib surface](docs/stdlib-2.0.md) — unknown methods are
compile errors, never host pass-throughs. The strata compose: components
call functions at every expression position, and the audit surfaces host
code transitively. The expression layer is **type-safe and null-safe**
(see above); the remaining typing frontier and everything else in flight
is tracked in the [2.0 roadmap](docs/v2.0-roadmap.md).

### Turing-complete, demonstrated by execution

2.0's pure stratum is Turing-complete (`var` + `while` + recursion), and the
claim is checked by running the emitted code, not by argument: the test
suite compiles revl sources for `fib` (loop form) and the Collatz
step-counter, executes the **emitted Python**, and asserts `fib(10) = 55`
and `collatz(27) = 111`; the same sources lower through the TypeScript
emitter. Suites at this commit: 189 passing frontend tests (incl. the
17-test sound-typing group, 7-test strata-composition, 6-test stdlib
groups, and the new §3.2 group covering `??` / `?.` / `${a.b}`),
21 python-backend, 24 ts-backend, plus the wasm demo, live hot-swap demo,
and the cordisc cross-check.

### The acceptance benchmark (syntax-2.0 §10)

The 2.0 syntax ships only if models actually write it better — the
prescription is 30 component specs × {1.x, 2.0, 2.0+host-blocks} × models,
measuring first-pass compile rate and iterations-to-green against the real
checker. The harness lives in [bench/](bench/) (`python3 bench/run.py`),
generations and summaries are committed under `bench/results/`.

<!-- BENCH-RESULTS:BEGIN -->
Two full 30×3 runs with DeepSeek V4 Pro (3-iteration error-feedback loop,
~$0.25 total): one scored against the **typing-enforced** checker
(`37bed37`, the shipping compiler), one against the pre-typing checker
(`9a8c670`) as a control.

Typed checker (`bench/results/typed-deepseek-v4-pro`):

| variant | first-pass compile | green ≤ 3 iters | mean iters-to-green |
|---|---|---|---|
| v1 (1.x syntax) | 27/30 (90%) | 29/30 | 1.07 |
| v2 (2.0 syntax) | 20/30 (67%) | 29/30 | 1.31 |
| v2host (2.0 + host blocks) | 18/30 (60%) | 30/30 | 1.40 |

Pre-typing control: v1 93% / v2 57% / v2host 43% first-pass — i.e. **sound
typing costs models nothing** (typed first-pass is equal-or-better, within
run-to-run variance on n=30).

What the gap was: one grammar friction dominated these runs. Models write the
full provide-method signature (`fn query(sql: Str) -> Int = ...`) exactly as
the 2.0 `fn` stratum teaches them to, and the component grammar rejected the
parameter and return-type annotations — most of each run's v2/v2host
first-pass failures were this single parse error. **That friction is now
fixed:** optional provide-method parameter *and* return-type annotations are
accepted and checked against the service signature (A6). Re-compiling the
committed first-pass generations against the fixed compiler (same
generations, new compiler — not a fresh model run) measures the effect:

| variant | first-pass, as-run | first-pass, fixed compiler |
|---|---|---|
| v1 | 27/30 | 29/30 |
| v2 | 20/30 | 29/30 |
| v2host | 18/30 | 29/30 |

All three variants reach **full parity** — the annotation friction is gone,
and `${}` templates now take arbitrary expressions (the one v2 case that used
a function call in a template). The single remaining failure is `29-mesh`, a
genuine model error (a malformed statement) *identical across all three
variants* — the irreducible floor, not syntax friction. A fresh model run
(which would also benefit from the new diagnostics) is future work.
Diagnostics did their job meanwhile: 176/180 cells compiled within 3
iterations, mean iterations-to-green ≤ 1.4 everywhere.
<!-- BENCH-RESULTS:END -->

```bash
uv venv && uv pip install -e ".[test]" && .venv/bin/pytest tests/
```
