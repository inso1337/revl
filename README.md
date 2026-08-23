<div align="center">

<img src="assets/banner.svg" alt="revl — the agent-first programming language" width="820">

<p>
  <a href="https://github.com/inso1337/revl/actions/workflows/ci.yml"><img src="https://github.com/inso1337/revl/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/runtimes-6-2dd4bf" alt="6 runtimes">
  <img src="https://img.shields.io/badge/emitted%20code-validated%20by%20real%20compilers-2dd4bf" alt="validated by real compilers">
  <img src="https://img.shields.io/badge/self_hosted-in_progress-yellow">
  <img src="https://img.shields.io/badge/agent--native-MCP-a78bfa" alt="MCP native">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT">
</p>

<p>
  <b><a href="#quickstart">Quickstart</a></b> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="DESIGN.md">Design</a> ·
  <a href="docs/vision.md">Vision</a> ·
  <a href="docs/guide-ai-agents.md">For agents</a>
</p>

</div>

---

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
service Database {
  emission fn execute(sql: Str) -> Int    // crosses the system boundary
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)   // its body emits, so the interface says so
}

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

## Architecture

<div align="center">

<img src="assets/architecture.svg" alt="one checked front-end, six hardened runtimes" width="880">

</div>

One front-end parses, checks and links `.rvl` source into a single IR, enforcing
guarantees **G1–G8** before any code is emitted. Six emitters lower that one IR
to six runtimes — and the same compiler runs behind an MCP server, so an AI
agent proposing a component talks to the *admission gate*, not the filesystem.
The claim that all six tiers agree is not asserted; it is
[measured](docs/conformance.md), by handing each emitter's output to that tier's
real compiler.

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
- **Agent-native.** `revl mcp serve` runs the compiler as an MCP server, so
  an agent proposing a component gets `revl_check` / `revl_admit` (the
  admission gate) instead of filesystem access, and rejections come back as
  structured diagnostics (code, guarantee, expected/actual, fix hint) —
  `revl compile --json-diagnostics` for humans and CI. `revl mcp schema`
  projects a composition's services to MCP tools whose `readOnlyHint` /
  `destructiveHint` are **derived from the compiler** — including from the
  method body, so a tool cannot describe itself as harmless when it emits.
  See [docs/mcp-bridge.md](docs/mcp-bridge.md).
- **Conformance is measured, not asserted.** `tools/conformance.py` emits 50
  language constructs through all six backends and reports what each does;
  `docs/conformance.md` records the result and separates a *deliberate* tier
  limit (an extern with no body for that backend; `Str` on the i32-only wasm
  tier) from a real gap — every gap in *emitting* is closed. `--validate`
  asks the second question — it hands each tier's output to that tier's real
  compiler (`tsc`, `cargo check`, `javac`, `wasmtime`, and a scope walk for
  python), because "the emitter did not raise" never implied "the code
  compiles": that gap hid a rust bug for months, a TypeScript twin of it, and
  16 cases that emit code their own compiler rejects (13 java, 3 rust).
  python, typescript and wasm validate clean; the rest are baselined in
  `tests/test_conformance_validate.py`, which fails on new breakage *and* on
  a baselined case that starts passing, so the list can only shrink. Each
  tier's validator skips loudly when its toolchain is missing, and two of
  them are missing in CI: the job that runs this suite installs neither
  `backends/typescript/node_modules` nor `wasmtime`, so the *typescript* and
  *wasm* halves of "validates clean" are reproducible locally
  (`pytest tests/test_conformance_validate.py -q -rs`, with `npm ci` and
  `wasmtime` present) but are **not** gated on every push.
- Backends: [cordis-py](https://github.com/geohotstan/cordis-py) (reference),
  [cordis](https://github.com/cordiverse/cordis) (TypeScript), the
  cordis-wasm substrate, [cordis-rs](https://docs.rs/cordis-rs)
  (Rust), [cordis4j](https://github.com/1na-ko/cordis4j) (Java), and
  [cordis-go](https://github.com/0xdenny218/stc-go) (Go, targeting the
  third-party stc-go runtime) —
  one language, six tiers ([docs/vision.md](docs/vision.md)). See
  [DESIGN.md](DESIGN.md) for the full design, the checked-guarantees table,
  and why raw native codegen is deliberately a non-goal.

**Status: v1.** The pipeline runs end-to-end on **six backends** —
**cordis-py** (reference), **cordis** (TS), the **cordis-wasm substrate**,
[backends/wasm/](backends/wasm/), **cordis-rs** (Rust), **cordis4j** (Java),
and **cordis-go** (Go) ([docs/v2.0-roadmap.md](docs/v2.0-roadmap.md)) —
`python -m revl compile` takes `.rvl` sources through parse → check →
link → IR ([docs/backend-ir-v1.md](docs/backend-ir-v1.md)), and the
emitters produce runnable components. On the wasm substrate, confinement is
enforced by the sandbox and `effect/undo` compiles to a state machine with
physical partial rollback. Divert-during-`await` (A1) is *executed* on
cordis-py
(`backends/python/tests/test_v1_semantics.py::test_a1_divert_during_await_skips_emission`)
and checked *structurally* on wasm — the golden asserts the post-`await`
effect is a separate segment a divert can skip
(`tests/test_wasm_backend.py::test_pulse_await_lowering`). The TypeScript
tier's runtime has the in-flight window for it but **no divert test yet**;
that is a gap, not a guarantee. `python -m revl audit` prints a
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

## The toolchain is the developer surface

revl treats its author — increasingly an AI agent — as a first-class user, so
the compiler exposes far more than *compile / don't compile*. Each of these is
reachable from the CLI and, where it makes sense, as an MCP tool.

| capability | what it answers | docs |
|---|---|---|
| **`revl plan`** | *before* a hot-swap: which provisions appear/withdraw, which running components get diverted, teardown order, how the emission surface changes | [plan.md](docs/plan.md) |
| **`revl query`** | who emits to `X`? what breaks if I withdraw `C`? what does a component reach? — each result says whether it is exact or a conservative over-approximation | [queries.md](docs/queries.md) |
| **Typed holes** | `hole[T]` type-checks so the rest of a draft still checks, is reported as an obligation, and can never be admitted or emitted | [holes.md](docs/holes.md) |
| **Backwards replay** | step an activation back over its own accumulator; R4 no-residue survives the round-trip. Only the revertible-effect model makes this cheap | [replay.md](docs/replay.md) |
| **Capability-scoped emissions** | `emission[db]` bounds *which* boundary a provider may cross, not just whether it emits — a tighter contract for agent-authored code | [capabilities.md](docs/capabilities.md) |
| **Why-traces** | a G2/G3/G4 rejection ships the derivation, not just the verdict (`put → writeThrough → audit.log`); `revl explain <code>` prints the fix | [why-traces.md](docs/why-traces.md) |
| **Fault tests** | `fault test "…" for Component { fail at step 2; assert no residue }` — the paradigm's L-Raise guarantee as a declarable assertion | [fault-tests.md](docs/fault-tests.md) |
| **Lifecycle tests** | `lifecycle test` — assert no-residue over a *live* composition from inside the language | [syntax-2.0.md](docs/syntax-2.0.md) §7.1 |
| **Function types** | `(Int, Str) -> Bool` — arrows leave the unchecked frontier where a type is known | [function-types.md](docs/function-types.md) |
| **Importers** | `revl import openapi` / `revl import wit` — an external contract becomes a typed service | [import-openapi.md](docs/import-openapi.md) · [import-wit.md](docs/import-wit.md) |
| **MCP server** | `revl mcp serve` — the whole compiler as an agent admission gate; `revl mcp schema` derives tool safety hints from the *body* | [mcp-bridge.md](docs/mcp-bridge.md) |

## Interoperability

revl components built for *different* runtimes compose in **one running
system**, across process boundaries — a Python component can require a service
a Rust component provides, and neither shares an address space. `revl run
--placement` spreads a composition over per-tier processes and wires each seam
with a generated proxy/stub pair.

- **Five of the six tiers are symmetric and reactive on the bridge.** `py ↔
  node (TypeScript) ↔ rust ↔ java ↔ go` compose in one lifecycle: every tier
  both *consumes* and *serves*
  across a seam, and every tier turns a provider's death into a reactive
  withdrawal (R2/R3) rather than an exception. Verified end-to-end with `revl
  run --placement … --once` — including a Go↔Python round-trip whose values are
  byte-compatible on the wire, and a Go consumer that withdraws reactively when
  its provider is killed (`tests/test_placement_go.py`). (Java is reactive on a
  real JDK 21 runtime, a stub otherwise. **wasm** is the one tier still outside
  the bridge — it is sandboxed, with its own confinement model.)
- **Values cross by construction, not by a wire schema.** A revl value is a
  record / ADT / `Opt[T]` — value semantics, no object identity, no cycles — so
  it serializes across a seam without a marshalling spec. A service that would
  need shared memory (an address-space-bound host object) is a **checked**
  verdict: `revl audit` (G8) refuses it *at the seam* rather than silently
  marshalling something that cannot cross.

See [docs/interop-bridge.md](docs/interop-bridge.md) for the transport tier,
the trust model, and the distributability audit.

### Turing-complete, demonstrated by execution

2.0's pure stratum is Turing-complete (`var` + `while` + recursion), and the
claim is checked by running the emitted code, not by argument. The gate is
`backends/wasm/test_v3_emit.py::test_v3_loops_run_on_wasmtime`: it compiles
revl sources for `fib` (loop form) and the Collatz step-counter, hands the
emitted module to **real wasmtime**, and asserts `fib(10) = 55`,
`fib(20) = 6765` and `collatz(27) = 111`.

```bash
pytest backends/wasm/test_v3_emit.py -q      # needs wasmtime on PATH; CI pins v47.0.3
```

Recursion executes on the same substrate
(`tests/test_wasm_backend.py`, recursive `fib(10) = 55`). Loops, `for-of`,
mutation and destructuring execute through the **emitted Python** in
`tests/test_v2_emit.py::test_loops_mutation_and_destructuring_emit_and_execute`
— but no test runs *these two programs* through the python or typescript
emitters, so treat wasmtime as the one that proves it.

**Suites** — each with the command that counts it, because a number with no
command behind it is the failure mode this project keeps hitting:

| suite | command | collected |
|---|---|---|
| frontend (typing, strata, stdlib, MCP-session, self-evolution, cross-tier, emitted-code validation, self-hosted lexer/parser, plan/queries/holes/replay/capabilities/why-traces/fault-tests/lifecycle) | `pytest tests/ -q` | 1146 |
| wasm tier | `pytest tests/test_wasm_backend.py backends/wasm/test_v3_emit.py -q` | 42 |
| java tier | `pytest backends/java/test_emit_java.py -q` | 29 |
| rust tier | `pytest backends/rust/test_emit_rust.py -q` | 26 |
| python tier | `sh backends/python/setup.sh && cd backends/python && .venv/bin/pytest -q` | 21 |
| typescript tier | `cd backends/typescript && npm ci && npx vitest run` | — |

Plus the live hot-swap demo, the self-evolution demo and the cordisc
cross-check. **Read the skips.** Most suites skip rather than fail when a
toolchain is absent, and a skip is not a pass: of the 42 wasm-tier tests, 20
execute on real `wasmtime` and skip without it; the java tier skips 8 without
a JDK; the rust cargo tests need crates.io reachable; the cordis-py runtime
tests skip without `backends/python/setup.sh`. The python tier is the
exception — without its own venv it *errors* at collection rather than
skipping, so run it the way the table says.

Three things in that list are **local-only checks, not CI gates**, because
the job that runs `tests/` installs neither cordis-py nor
`backends/typescript/node_modules`: the self-evolution demo
(`tests/test_self_evolution.py`), the live MCP session
(`tests/test_mcp_session.py`), and the cordisc cross-check
(`tests/test_cordisc_crosscheck.py`, which needs cordisc checked out beside
this repo). They pass on a fully-provisioned machine; they skip on every
push. `pytest tests/ -q -rs` prints exactly which.

### The acceptance benchmark (syntax-2.0 §10)

The 2.0 syntax ships only if models actually write it better — the
prescription is 30 component specs × {1.x, 2.0, 2.0+host-blocks} × models,
measuring first-pass compile rate and iterations-to-green against the real
checker. The harness lives in [bench/](bench/) (`python3 bench/run.py`),
generations and summaries are committed under `bench/results/`.

<!-- BENCH-RESULTS:BEGIN -->
Two full 30×3 runs with DeepSeek V4 Pro (3-iteration error-feedback loop,
~$0.25 total): one scored against the **typing-enforced** checker
(`37bed37`), one against the pre-typing checker (`9a8c670`) as a control.
Those as-run numbers are frozen in each run's `summary.md`:

| variant | v1 | v2 | v2host |
|---|---|---|---|
| typed (`bench/results/typed-deepseek-v4-pro`) | 27/30 | 20/30 | 18/30 |
| pre-typing control (`bench/results/baseline-deepseek-v4-pro`) | 28/30 | 17/30 | 13/30 |

Sound typing cost models nothing (typed first-pass is equal-or-better, within
run-to-run variance on n=30). One grammar friction dominated the v2/v2host
gap: models write the full provide-method signature
(`fn query(sql: Str) -> Int = ...`) exactly as the `fn` stratum teaches them
to, and the component grammar rejected those annotations. That friction is
fixed (A6: optional provide-method parameter and return-type annotations,
checked against the service signature).

**Re-score against the current compiler.** `bench/rescore.py` recompiles the
committed `attempt-1.rvl` files — *the same generations, a newer compiler; not
a fresh model run, and it calls no provider* — and reports a failure taxonomy:

```bash
python3 bench/rescore.py --run all      # measured at compiler d71c689
```

| variant | as-run | re-score @ `d71c689` |
|---|---|---|
| v1 | 27/30 | 22/30 |
| v2 | 20/30 | 23/30 |
| v2host | 18/30 | 23/30 |

**The re-score is lower than the as-run number, and that is the honest
result.** The A6 fix alone took all three variants to 28/29/29 (measurable:
`python3 bench/rescore.py --run all --compiler-root <export of 056539f>`).
Landing G4's *upper-bound* direction — a service operation declared plain `fn`
may not be implemented by a body that reaches an emission — then took 18 of
the 90 cells red. Those 18 are not model errors: six specs pinned an interface
declaring `fn put(...)` plain while the brief instructed the model to emit
inside it, so under the new rule the spec was unsatisfiable and the prompts
never taught the rule anyway. Both are fixed for the next run (the seven
affected `specs.json` interfaces now say `emission fn`; all three prompts now
state the upper-bound direction), but fixing them cannot retroactively change
generations produced before the rule existed.

The remaining 4 failures are genuine model errors: an invented stdlib method
(`take` on `Str`) and `29-mesh`, where every variant wrote a bare `kv.put(k, v)`
in statement position — legal only as `let _ = kv.put(k, v)`, a form the
prompts did not mention and now do.

**A fresh model run is what would move these numbers, and it has not been
done since the rule landed.** Treat the re-score as a regression measurement
of the corpus, not as a current model capability figure.
<!-- BENCH-RESULTS:END -->

## Documentation

Start with **[DESIGN.md](DESIGN.md)** for the guarantees and the checked table,
or **[docs/vision.md](docs/vision.md)** for what this is *for*.

**Language**
- [syntax-2.0.md](docs/syntax-2.0.md) — the full 2.0 language reference
- [stdlib-2.0.md](docs/stdlib-2.0.md) — the specified stdlib surface
- [function-types.md](docs/function-types.md) · [holes.md](docs/holes.md) · [capabilities.md](docs/capabilities.md) — newer type-system surface
- [generics.md](docs/generics.md) — implicit and explicit `[T]` type parameters
- [design-v2-realms.md](docs/design-v2-realms.md) · [design-v2-instances.md](docs/design-v2-instances.md) — realms, interception, instances

**Testing & guarantees**
- [conformance.md](docs/conformance.md) — every construct against every tier, and how emitted code is validated
- [fault-tests.md](docs/fault-tests.md) — L-Raise / no-residue as a language form
- [replay.md](docs/replay.md) — backwards replay over the accumulator
- [contract-errata.md](docs/contract-errata.md) — known runtime divergences, per tier
- [selfhost-findings.md](docs/selfhost-findings.md) — the self-hosted front end as a differential oracle: what it found, and what writing revl in revl actually cost

**Working in a live system**
- [plan.md](docs/plan.md) — a dry run for admission
- [queries.md](docs/queries.md) — ask the composition questions
- [why-traces.md](docs/why-traces.md) — derivations behind a rejection

**Agents & interop**
- [guide-ai-agents.md](docs/guide-ai-agents.md) — the agent-facing guide
- [mcp-bridge.md](docs/mcp-bridge.md) — the compiler as an MCP server
- [import-openapi.md](docs/import-openapi.md) · [import-wit.md](docs/import-wit.md) — importers
- [interop-bridge.md](docs/interop-bridge.md) — cross-tier interop

**Internals**
- [backend-ir-v1.md](docs/backend-ir-v1.md) · [backend-ir-v3.md](docs/backend-ir-v3.md) — the IR contract
- [backend-go-v3.md](docs/backend-go-v3.md) — the Go tier's IR v3 implementation plan
- [v2.0-roadmap.md](docs/v2.0-roadmap.md) — what's done and what's in flight

**Project**
- [docs/stability.md](docs/stability.md) — what a version number promises: v1 IR frozen and byte-identical, what is versioned by `ir_version`, what may break without notice
- [CONTRIBUTING.md](CONTRIBUTING.md) — the wave/worktree workflow, the pre-commit contract, and the "every rejection joins the executable spec" rule — for human and agent contributors
- [SECURITY.md](SECURITY.md) — reporting a soundness escape (a program the checker accepts that breaks a guarantee)

## Quickstart

```bash
uv venv && uv pip install -e ".[test]" && .venv/bin/pytest tests/
```

```bash
python -m revl compile examples/user_cache.rvl   # source → checked IR → emitted component
python -m revl audit    examples/user_cache.rvl   # the G8 boundary surface
python -m revl mcp serve                          # the compiler as an agent admission gate
```


## Acknowledgments

revl stands on open-source work by others, with gratitude.

**The paradigm.** revl is the language-level realization of Cordis — the
design, the guarantees, and every runtime it targets come from that ecosystem.

- [*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper) — the paper revl formalizes as a type system
- [Cordis](https://github.com/cordiverse/cordis) — the reference library and the TypeScript runtime (npm [`cordis`](https://www.npmjs.com/package/cordis))

**The runtime targets.** One revl source lowers to community ports of the
same spatiotemporal-composability paradigm; revl exists because these do. Each
runs on its language's platform, cited alongside it.

- [cordis-py](https://github.com/geohotstan/cordis-py) — the Python reference runtime, on [CPython](https://github.com/python/cpython)
- [Cordis](https://github.com/cordiverse/cordis) (TypeScript) — the portability tier, on [Node.js](https://github.com/nodejs/node)
- [cordis-rs](https://docs.rs/cordis-rs) ([source](https://github.com/dshbox/cordis-rs)) — the native tier, on [Rust](https://github.com/rust-lang/rust)
- [cordis4j](https://github.com/1na-ko/cordis4j) — the JVM tier, on [OpenJDK](https://github.com/openjdk/jdk)
- [cordis-wasm](https://github.com/inso1337/cordis-wasm) — the wasm substrate (first-party), on [Wasmtime](https://github.com/bytecodealliance/wasmtime) (Bytecode Alliance)
- [stc-go](https://github.com/0xdenny218/stc-go) — the newest tier, on [Go](https://github.com/golang/go) (runs under `go test`, not yet in the conformance matrix)

**Toolchains & libraries** used to build, emit, and test.

- [Hatchling](https://github.com/pypa/hatch) — Python build backend · [pytest](https://github.com/pytest-dev/pytest) — the frontend and cross-tier suites
- [TypeScript](https://github.com/microsoft/TypeScript) — the TS tier's typechecker (validation gate) · [Vitest](https://github.com/vitest-dev/vitest) — the TS backend suite
- [Serde](https://github.com/serde-rs/serde) / [serde_json](https://github.com/serde-rs/json) — cross-tier value marshalling on the Rust tier

## License

[MIT](LICENSE).
