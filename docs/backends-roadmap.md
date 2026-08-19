# Backends roadmap

The organizing fact: **the five tiers are disjoint by directory** (`backends/{python,typescript,rust,java,wasm}`), so one agent per tier is the natural wave shape — parallel, no shared-file contention. The only cross-tier files are the conformance gate (`tests/test_realm_conformance.py`, `tools/conformance.py`) and the IR schema; work touching those is sequenced, not parallelized.

## Current state

| tier | ownership | runtime coverage | headline gap |
|---|---|---|---|
| cordis-py | user fork (carries revl fixes) | reference — 29 test files, executed | none blocking; the place instance-parametric work lands |
| cordis-ts | upstream | 10 test files, `tsc` + executed | thinner realm runtime coverage than py |
| cordis-rs | upstream crate (0.3.0) | 1 scenario file (8 tests), `cargo check`/`cargo test` | none blocking — reactive isolate-linking fixed (plug-time isolation, emitter-side); realm labels fixed |
| cordis4j | upstream (github/1na-ko) | 1 scenario file, real-jar scenarios | global-realm divergence (errata'd, xfail) |
| cordis-wasm | **user wrote it** (first-party) | 1 test file, wasmtime exec | i64 port in flight; service boundary is scalar-only (rich types = follow-up after the port); named realms conform |
| cordis-go | third-party `0xdenny218/stc-go` (pinned `b3d6788`) | scenarios + v3 fixtures executed; **0 gaps in the conformance matrix**, CI-gated (`go build`) | v1/v2/v3 all emit & build; teardown-ordering divergence from cordis-rs (errata); no spawn yet |

**Conformance matrix status:** all five *language* tiers — python, typescript, rust, java, **go** — are at **0 gaps**, each validated by its real compiler. Only cordis-wasm has gaps (its i32→i64 port is in flight; its remaining gaps are scalar-only-service-boundary + config/host, not extern gaps).

The through-line across every past wave: **a claim with no gate behind it** — emitters marked correct with nothing executing their output (the `Map.new()` that shipped in a golden; realm semantics asserted at runtime nowhere). The runtime-truth theme below exists to close that class for good.

Ownership dictates fix path: **wasm and py are first-party** (change directly); **rs/ts/java are upstream** (fork → fix → PR, with sign-off before any external push). See the runtime-ownership memory.

---

## Wave A — Runtime truth (LANDED)

Goal: every guarantee a tier claims is asserted by *executing* emitted code on the real runtime, not by compile-gating or golden text. **It paid off immediately — executing instead of inspecting caught two correctness bugs that had shipped to `main`, invisible to the emit-string/golden gates that were green the whole time.**

- **A0 · realm conformance gate** — ✅ **on main.** `tests/test_realm_conformance.py`: runs the named-realm contract ("equal strings = same realm", G2 same-realm conflict) on all five runtimes by executing emitted code. **py / ts / rs / wasm PASS, java `strict xfail`** (the errata'd divergence). Asserts *provider-side* separation; *consumer-side* reactive separation was A2-ts.
- **A2-wasm** — ✅ **on main.** 23 wasmtime scenarios + **two lowering fixes**: a *silent miscompile* (nested record/list/variant/`??` shared one module-wide scratch, so `{inner:{v:11},k:5}` computed `1285` instead of `16`), and a valid-code rejection (`match`-bind inside a loop). Both invisible to emit-string tests.
- **A2-java** — ✅ **on main.** 42 real-cordis4j-jar value assertions + a **compile fix**: a host call in a top-level `fn` (`Pool.open(..)`) emitted non-compiling Java (`Pool.open.apply(..)` → "package Pool does not exist") — a construct that worked on three other tiers, silently broken on Java because nothing executed it.
- **A2-ts** — ✅ **on main.** Proved cordis-ts reactively resolves an isolated consumer to its same-realm provider (the reference point for the rust layer decision below) + 8 executed v3 stdlib/Opt/interp/match assertions that were `tsc`/golden-only. No bug found.
- **A1 · rust reactive isolate-linking** — ✅ **resolved — revl emitter fix** (lands with `agent/rust-realm-reactive-link`). An isolated consumer's `requires kv in realm("t")` stayed `Pending` because the emitter applied `ctx.isolate_with(...)` *inside* the plugin closure, so cordis-rs evaluated the `Inject` gate against the un-isolated root context (the fiber's `meta.isolates` never carried the realm). **Layer decided empirically, not from the ts baseline alone:** driven directly, the raw cordis-rs, cordis-py and cordis-ts runtimes *all* link an isolated same-realm consumer/provider when isolation is applied at the context level, and *all* hang `Pending` when it is applied inside the closure — so cordis-rs is correct and **no upstream PR is needed**. Fix moves isolation to plug time (`_revl_isolate_ctx`, mirroring the py/ts `plug()` helper) in `backends/rust/emit.py`; a new same-realm consumer/provider scenario (`backends/rust/scenarios/`) and the updated conformance harness (`tests/fixtures/realm_conformance/harness/realms.rs`) assert it on the real runtime.

> **Correction folded in (gate result):** an earlier draft listed "wasm local realms" as a Wave-A win. Wrong — wasm has no xfail; its *named* realms already conform at runtime. What wasm lacks is *local/identity* realms for runtime-created instances → design-gated Wave-B work (B3 below).

## Wave B — Instance-parametric foundation (PHASE 1 LANDED)

Addressing decided: **supervision-tree** (an instance is reachable by its spawner and its own children, never by arbitrary siblings). **Phase 1 is on `main`**: a frozen `spawn` IR (instantiation-as-acquisition — a `let-effect` step whose `acquire` is a `spawn` node, so no new IR step kind), one new grammar form (`spawn C with {…}`), the G2/G3/G4/G8 rule changes, and a **cordis-py reference that executes** (two live instances in distinct local realms, independent LIFO teardown, request-scoped early reclamation, supervision-tree addressing). Two design simplifications fell out: the nested teardown scope is just a child fiber (no `Frame` subclass), and hierarchical realm resolution wasn't needed because spawn targets are templates excluded from the static manifest. Remaining:

- **B1 · sub-component teardown scopes** *(first-party: py runtime + emitter)* — item zero from the static audit. Today effects created in a provide-method are adopted into the component-level `Frame` and live until the component tears down; a request-scoped instance needs a nested scope. `backends/python/runtime.py` `Frame`.
- **B2 · hierarchical realm resolution in the checker** — `src/revl/lower.py:3047` `_realm` is flat; the runtime walks the parent chain. Diverges the instant a child is plugged onto its spawner's context. Prerequisite for `spawn`, opens before per-instance realms. *(compiler, not a backend — noted here because it gates the backend work.)*
- **B3 · wasm local/identity realms** *(first-party)* — the gate proved wasm handles *named* realms, but instance-parametric components need *local* realms (each runtime instance in its own realm without a distinct label string). wasm's provider table is flat and realms are compile-time mangling, so a runtime-determined instance count can't be expressed today. Add a realm prefix applied at resolve / publish / conflict-check (`backends/wasm/runtime.py` ~`:293`, `:332`, `:131-137`). This is the ~10-line first-party change — but it only matters once instance-parametric is accepted, hence Wave B not A.
- **B4 · spawn lowering across tiers** — once py proves the shape, one agent per remaining tier, disjoint.

## Wave C — New Cordis runtimes

- **Go — ✅ DONE.** cordis-go targets third-party `0xdenny218/stc-go` (pinned `b3d6788`), a paper-faithful Go runtime. Emits v1/v2/v3, executed on real stc-go, **0 gaps in the conformance matrix**, CI-gated. One divergence pinned (teardown ordering vs cordis-rs, errata).
- **C / C++ / Zig — blocked on the runtime.** No Cordis-paradigm runtime exists on GitHub for these (searched); revl can't emit-and-execute against a runtime that isn't there. This is runtime-authoring work (the user's domain, like cordis-wasm), not an emitter wave. revl's side starts once the first one runs.

The backend contract is small (install effect with inverse; provide/read keys; reactive refresh), so each new tier is a fresh directory = one agent, disjoint from every other wave — *once its runtime exists*.

## Wave D — Emitter structural cleanups (SEQUENCED — do not naively parallelize)

These touch shared emit structure/IR semantics, so they conflict if run in parallel. One at a time, integrated between:

- **D1 · single expression renderer per backend** — ✅ **on main.** ts/rust/java each converged their v1/component renderer and their 2.0 renderer into one shape-dispatching function (py/wasm were already single-renderer). This is the structural fix behind most of the divergences the runtime-truth wave then surfaced.
- **D2 · uniform component IR dialect** — reduce per-backend divergence in how components lower.
- **D3 · `call` node disambiguation** across dialects (partly addressed by D1's shape-dispatch).

## Deferred / tracked, not scheduled

- **Java global-realm fix** — errata'd (`docs/contract-errata.md`). The real fix routes emission through cordis4j `Loader` + `ComponentSpec`, replacing the `plugin`/`inject` composition that G7/A8/Theorem-63 are verified against — out of proportion to a multi-tenancy detail. A characterization test fails loudly if the behavior ever changes. Revisit only if global realms on Java become load-bearing.
- **wasm deliberate boundaries** — `config` blocks, host builtins (`Map`/`Pool`), non-`Int` service types, compound interpolation, escaping arrows. Each is a precise `EmitError`, not a gap. Closing any (e.g. wasm `Map`/`Pool`) is its own design decision, first-party.

---

### Status / what's next

Wave A is landed on `main` for wasm, ts, java (and the A0 gate); rust A1 is finishing in a separate session and folds in when done. Wave D1 (single renderer) is also on `main`.

The next decision is **Wave B's gate**: the `docs/design-v2-instances.md` addressing question (instances reachable only by parent + own children, no sibling-by-id lookup). Nothing in Wave B starts until that's accepted. Independently, **Wave C** (new runtimes) and **Wave D2/D3** (uniform IR dialect, `call` disambiguation) are startable whenever a wave is pointed at them.
