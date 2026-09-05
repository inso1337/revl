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

## Declared `Secret[T]` values and the runtime registry (421 F6)

A `Secret[T]` declaration authorises disclosure to the declared receiver. It
does not authorise any sink this tier keeps to hold a copy: the console the
placement runners print, the failure text a provider sends back across a seam,
or the WAL under `--record`. The frontend refuses the crossings it can see
statically; what is left is a value that reaches a host body legitimately (a
config credential handed to the component's own binding, a parameter the
service declared `Secret[T]`, a token an extern minted) and then turns up in a
message the host wrote, such as a driver error that quotes its arguments.

The emitted `Components` carries a registry for a document that declares a
`Secret[T]` anywhere (`_SECRET_MODE`; a secret-free document is byte-identical
to before), the same mechanism as the go tier's `revlMarkSecret` block and
the ts tier's `markSecret`:

* `revlMarkSecret(this.<field>)` in **both** plugin constructors for a config
  field declared `Secret[T]` (the value arrives once, through the constructor,
  whichever door supplied it);
* `revlMarkSecret(<param>)` at the head of a provide method whose service
  declares that parameter `Secret[T]` (the receiver end);
* `revlSecretResult(...)` around an extern whose declared return was
  `Secret[T]` (the origin end); the verbatim `@java` body keeps its signature
  under a private name and the public one registers what it returns;
* `revlRedactText(String)` over the registered values (exact match, longest
  first, values under four characters are not remembered), and the WAL
  descriptor arguments read through it under `--record`.

The runners keep no registry of their own. `PlacementRunner`, `RunOnce` and
`RealPlacementRunner` bind to the container's `revlRedactText` reflectively at
boot (`bindSecretRegistry`) and funnel every console line through it in `log`,
so a printer added later reads an already-redacted line. `seamFailure` is two
stages: this call's own argument values become `<redacted:arg>`, then every
registered value becomes `<redacted:secret>` (the py and ts markers, so a
polyglot seam reads the same whichever tier answered). The dispatch wrapper
`Method.invoke` puts around a provider throw is unwrapped first, so the reply
names the provider's failure rather than reading `InvocationTargetException:
null`; that unwrap and the registry stage are one change, since the cause is
exactly where a host message quotes a held credential.

`test_secret_registry.py` proves it by running two JVMs across a real socket
seam and grepping what they printed and what the wire carried: the value is
absent and its marker present, an ordinary config value beside it is verbatim,
a copy of the runner that renders the whole cause chain still leaks nothing,
and the same run with the registry unbound (or the constructor's registration
stripped) prints the value, so the absence is not vacuous.

## Verify

Every emission in this tier's suite goes through
[`javac_gate.py`](javac_gate.py), which **compiles the emitted unit with
`javac` against the assumed cordis4j API in [stubs/](stubs/)** before the test
asserts anything about it, and some tests go on to run an emitted `test` block
on the JVM (`REVL_TESTS`). So a string-level assertion here is a claim about a
program javac has already accepted, which it was not before issue #154: two
uncompilable-output defects shipped under substring matches that all passed.

The gate resolves a JDK through `revl.run_java`'s resolver rather than bare
`shutil.which`, because `/usr/bin/javac` on macOS is a shim that answers before
a keg-only Homebrew JDK and made javac look absent on a machine that had one.
It is a no-op where no JDK is reachable, so the suite still runs on a
toolchain-free checkout; that this cannot happen in CI is asserted statically
by `tests/test_java_javac_gate_runs_in_ci.py`, which fails unless the
`backend-java` job both installs a JDK and collects the whole
`backends/java/` root (issue #183).

The stdlib surface lowers through typed `revl*` static
overloads so every (method, Str|List) pair from docs/stdlib-2.0.md resolves
at compile time; revl `Map.new()` is emitted as `Map.create()` (`new` is a
Java reserved word).

Against the real jar:

```bash
python3 emit.py ../../examples/user_cache.ir.json > Components.java
javac -cp cordis4j-core.jar Components.java
```

The stubs are the emitter's declared API assumption, and they are validated
against the real thing: `test_runtime_scenarios_on_real_cordis4j` in
`backends/java/test_emit_java.py` drives the A1/G7 runtime scenarios, and CI's
`backend-java` job clones cordis4j and compiles `cordis4j-core` from source on
every push.
