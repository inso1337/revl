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
  a real `HashMap<String, String>`, and `Job.run` is a no-op placeholder.
- **Effectful provide-method bodies** are lowered into the component
  `Context.EffectScope`.
- **`await` steps** lower to a blocking call inside an `AsyncPlugin`
  (`ctx.pluginAsync`); the emitted class implements
  `io.cordis4j.core.AsyncPlugin` and `apply` declares `throws Exception`.
- **`if` guards, block-effect `setup`, and `fail`** are lowered to Java
  control flow, setup statements before acquisition, and
  `io.cordis4j.core.CordisException` respectively.

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
