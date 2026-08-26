# revl → cordis4j backend

Emits idiomatic Java targeting **[cordis4j](https://github.com/1na-ko/cordis4j)** —
the JVM port of Cordis (the DeepSeek Harness kernel). `emit(ir) -> str` produces
one Java source file (service interfaces + plugin classes).

## Mapping (DESIGN.md §7 — the backend contract)

| revl | Java (cordis4j) |
|---|---|
| `service` | `public interface <Name> { <ret> <m>(<params>); }` |
| `component` | `public final class <Name>Plugin implements Plugin { apply(ctx) }` |
| `requires` | `ctx.get(<Svc>.class)` |
| `provides` | `ctx.provide(ServiceKey.of(<Svc>.class), new <Impl>(…))` |
| `effect E undo U` | `Disposables.of(() -> <undo>)`, combined via `Disposables.composite(…)` / `Context.EffectScope` |
| `emit` | plain call |
| `format` | `String.format(…)` |
| `config` | plugin constructor parameters + no-arg constructor with IR defaults |

## Supported IR

- **ir_version 1, 2, and 3.** v2 realms/`isolate`/`intercept`, and v3
  types/functions/match/externs are emitted; version 4 is rejected.
- **Host objects** (`Pool`/`Map`/`Job`) are emitted as a minimal working Java
  runtime in the generated file. `Pool` is a functional placeholder, `Map` is
  a real `HashMap<String, String>`, and `Job.run(name)` returns an awaitable
  handle (`Job.pending()` counts the ones still in flight).
- **Effectful provide-method bodies** are lowered into the component
  `Context.EffectScope`.
- **`await` steps** *join* the handle they start — `Job.run("x").await();`,
  not a bare `Job.run("x");` — inside an `AsyncPlugin` (`ctx.pluginAsync`);
  the emitted class implements `io.cordis4j.core.AsyncPlugin` and `apply`
  declares `throws Exception`.
- **`if` guards, block-effect `setup`, and `fail`** are lowered to Java
  control flow, setup statements before acquisition, and
  `io.cordis4j.core.CordisException` respectively.

## Witnessed effects and two-phase teardown (243 / 247 / 318)

A component that registers a `witnessed` (transactional, item 243) or
`emit … compensate` (item 247) entry emits a per-file `RevlFrame` — the
shared bracket / transactional / compensation two-phase teardown accumulator
from docs/design/teardown-contract.md. It supplies what cordis4j's raw
`EffectScope` LIFO does not: a `committed` commit/abort discriminator (flipped
once by `apply()`'s `frame.commit()` right before it returns), a per-inverse
guard so a failing inverse is recorded as residue and never stops the Phase-1
replay, and the Phase-2 split for compensations. A **bracket-only** component
emits exactly as before — no `RevlFrame` (see `_component_needs_frame`).

**Per-tool-call witnessed effects (item 318 — the agent H1 gate).** A witnessed
fs mutation can fire from a **provide-method** body (per request), after
activation. Each call registers the extern's declared inverse into the
*enclosing component's activation frame* via `RevlFrame.transactionalMethod`,
tracked on the provider struct's activation-scope `this.fx` / `this.frame` — so
the inverse outlives the method call and is disposed by the component's own
unload. On a clean unload the mutation **persists** (discharged — it is the
deliverable); on a **`RevlActivation.abort()`** followed by unload it **reverts**
residue-free, across *every* per-call mutation.

*Disposal-ordering note (why java needs no py-style parking).* The py tier flips
`_committed` at TEARDOWN (`drain`), so a method-registered entry disposed as an
ordinary sibling would observe not-committed on a clean unload and wrongly
revert the deliverable; py fixes that by parking the entry in
`_deferred_transactional` and disposing it inside `drain`, after the flip. Java
flips `committed` at ACTIVATION-END, strictly before any method runs and before
any teardown, so the entry already observes the settled bit when `fx` disposes
it — no deferred-yield window, no parking. The one rule the emitter honours is
that the entry registers into the **component activation** `fx`/`frame`, never a
per-call scope (which would discharge at method-return and leave a later abort
unable to revert — residue). `apply()` returns a `RevlActivation` (wrapping
`fx` + `frame`) so a session-level reject can reach `frame.abort()` after a
clean activation; its `dispose()` runs Phase 1 then drains Phase 2. The WAL
discharge-descriptor enumeration is a py-only owned deliverable (no java-side
recording channel yet — teardown-contract.md, "Owned deliverable"); on java the
persist-vs-revert outcome is proven by observable filesystem state
(`scenarios/method_witnessed.rvl` + `scenarios/RunMethodWitnessedH1.java`,
driven by `test_java_method_witnessed_h1_on_stub_runtime`).

## Verify

The suite (`test_emit_java.py`) asserts emitted structure at the string
level everywhere, and — when a working JDK is present (CI pins Temurin 21)
— **compiles emitted sources with `javac` against the assumed cordis4j API
in [stubs/](stubs/)** and runs an emitted `test` block on the JVM
(`REVL_TESTS`). The stdlib surface lowers through typed `revl*` static
overloads so every (method, Str|List) pair from docs/stdlib-2.0.md resolves
at compile time; revl `Map.new()` is emitted as `Map.create()` (`new` is a
Java reserved word).

Against the real jar:

```bash
python3 emit.py ../../examples/user_cache.ir.json > Components.java
javac -cp cordis4j-core.jar Components.java
```

The stubs are the emitter's declared API assumption — validating them
against the real cordis4j jar (and porting the A1/G7 runtime scenarios) is
tracked in docs/v2.0-roadmap.md.
