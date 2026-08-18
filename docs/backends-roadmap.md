# Backends roadmap

The organizing fact: **the five tiers are disjoint by directory** (`backends/{python,typescript,rust,java,wasm}`), so one agent per tier is the natural wave shape — parallel, no shared-file contention. The only cross-tier files are the conformance gate (`tests/test_realm_conformance.py`, `tools/conformance.py`) and the IR schema; work touching those is sequenced, not parallelized.

## Current state

| tier | ownership | runtime coverage | headline gap |
|---|---|---|---|
| cordis-py | user fork (carries revl fixes) | reference — 29 test files, executed | none blocking; the place instance-parametric work lands |
| cordis-ts | upstream | 10 test files, `tsc` + executed | thinner realm runtime coverage than py |
| cordis-rs | upstream crate (0.3.0) | 1 scenario file, `cargo check`/`cargo test` | reactive isolate-linking hangs `Pending`; realm labels just fixed |
| cordis4j | upstream (github/1na-ko) | 1 scenario file, real-jar scenarios | global-realm divergence (errata'd, xfail) |
| cordis-wasm | **user wrote it** (first-party) | 1 test file, wasmtime exec | *named* realms conform (gate green); lacks *local/identity* realms (instance-parametric) |

The through-line across every past wave: **a claim with no gate behind it** — emitters marked correct with nothing executing their output (the `Map.new()` that shipped in a golden; realm semantics asserted at runtime nowhere). The runtime-truth theme below exists to close that class for good.

Ownership dictates fix path: **wasm and py are first-party** (change directly); **rs/ts/java are upstream** (fork → fix → PR, with sign-off before any external push). See the runtime-ownership memory.

---

## Wave A — Runtime truth (READY NOW, one agent per tier)

Each tier's agent owns only its own directory. No design gate. Goal: every guarantee the tier claims is asserted by *executing* emitted code on the real runtime, not by compile-gating or golden text.

- **A0 · realm conformance gate** — `tests/test_realm_conformance.py`. ✅ **Landed** (active wave). Cross-tier; runs the named-realm contract ("equal strings = same realm", G2 same-realm conflict) on all five runtimes by executing emitted code. Result: **py / ts / rs / wasm PASS, java `strict xfail`** (the errata'd divergence). This is the scaffold the rest of Wave A hangs scenarios onto. Note: the gate asserts *provider-side* separation; *consumer-side* reactive separation is deliberately out of scope here — it's exactly what A1 fixes.
- **A1 · rust reactive isolate-linking** — an isolated consumer's `requires kv in realm("t")` never activates (stays `Pending`) because the `Inject` gate reads the un-isolated root context. **Decide the layer first** (chip already spawned): if py/ts link isolated consumers reactively and rust alone hangs → cordis-rs PR; if all three place isolation the same way → revl emitter fix. Then a runtime scenario proving activation.
- **A2 · rust/java/wasm executed-scenario parity** — bring per-tier executed coverage toward py/ts. Each already has one scenario harness (`backends/rust/scenarios/`, `backends/java/scenarios/RunRealScenarios.java`, wasm wasmtime tests) — extend them to cover the guarantees currently only compile-gated on that tier. Disjoint by tier.

Wave A is fully parallel: **rust agent** (A1 + A2-rust), **java agent** (A2-java), **wasm agent** (A2-wasm), **ts agent** (A2-ts consumer-side realm coverage). A0 is already in.

> **Correction (gate result):** an earlier draft listed "wasm local realms" as a contained Wave-A win that would flip a wasm xfail. Wrong on both counts — wasm has no xfail; its *named* realms already conform at runtime. What wasm actually lacks is *local/identity* realms for runtime-created instances, which is design-gated Wave-B work (B3 below), not Wave A.

## Wave B — Instance-parametric foundation (DESIGN-GATED — hold)

Blocked on the `docs/design-v2-instances.md` addressing decision (question 2). Do not spawn until accepted. When unblocked, most of it lands in the two first-party tiers:

- **B1 · sub-component teardown scopes** *(first-party: py runtime + emitter)* — item zero from the static audit. Today effects created in a provide-method are adopted into the component-level `Frame` and live until the component tears down; a request-scoped instance needs a nested scope. `backends/python/runtime.py` `Frame`.
- **B2 · hierarchical realm resolution in the checker** — `src/revl/lower.py:3047` `_realm` is flat; the runtime walks the parent chain. Diverges the instant a child is plugged onto its spawner's context. Prerequisite for `spawn`, opens before per-instance realms. *(compiler, not a backend — noted here because it gates the backend work.)*
- **B3 · wasm local/identity realms** *(first-party)* — the gate proved wasm handles *named* realms, but instance-parametric components need *local* realms (each runtime instance in its own realm without a distinct label string). wasm's provider table is flat and realms are compile-time mangling, so a runtime-determined instance count can't be expressed today. Add a realm prefix applied at resolve / publish / conflict-check (`backends/wasm/runtime.py` ~`:293`, `:332`, `:131-137`). This is the ~10-line first-party change — but it only matters once instance-parametric is accepted, hence Wave B not A.
- **B4 · spawn lowering across tiers** — once py proves the shape, one agent per remaining tier, disjoint.

## Wave C — New Cordis runtimes (INDEPENDENT — parallel anytime)

"C/C++/Go/Zig in progress." The backend contract is small (install effect with inverse; provide/read keys; reactive refresh). Each new tier is a fresh directory = one agent, fully disjoint from every other wave. Can run concurrently with Wave A. Sequence by demand, not dependency.

## Wave D — Emitter structural cleanups (SEQUENCED — do not naively parallelize)

These touch shared emit structure/IR semantics, so they conflict if run in parallel. One at a time, integrated between:

- **D1 · single expression renderer per backend** — each backend currently renders expressions in scattered spots; consolidate to one renderer (the §1d structural fix).
- **D2 · uniform component IR dialect** — reduce per-backend divergence in how components lower.
- **D3 · `call` node disambiguation** across dialects.

## Deferred / tracked, not scheduled

- **Java global-realm fix** — errata'd (`docs/contract-errata.md`). The real fix routes emission through cordis4j `Loader` + `ComponentSpec`, replacing the `plugin`/`inject` composition that G7/A8/Theorem-63 are verified against — out of proportion to a multi-tenancy detail. A characterization test fails loudly if the behavior ever changes. Revisit only if global realms on Java become load-bearing.
- **wasm deliberate boundaries** — `config` blocks, host builtins (`Map`/`Pool`), non-`Int` service types, compound interpolation, escaping arrows. Each is a precise `EmitError`, not a gap. Closing any (e.g. wasm `Map`/`Pool`) is its own design decision, first-party.

---

### Suggested first launch

Wave A: four tier-agents in parallel (rust, java, wasm, ts) on A1/A2. A0 (the gate) is already in, so they build on a green scaffold. Highest value per unit risk — closes the runtime-truth gap, all disjoint. The rust agent carries the one non-mechanical item (A1, the reactive-isolate layer decision); the other three are executed-scenario coverage. Wave C can run alongside if new-runtime demand is real.
