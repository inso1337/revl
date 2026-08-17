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
| `effect E undo U` | `Disposables.of(() -> <undo>)`, combined via `Disposables.composite(…)` |
| `emit` | plain call |
| `format` | `String.format(…)` |
| `config` | plugin constructor parameters |

## Spike limits (tracked in docs/v2.0-roadmap.md)

- **ir_version 1 only.** v2 realms and v3 types/functions rejected with a clear error.
- **Host objects** (`Pool`/`Map`/`Job`) are stubs that throw
  `UnsupportedOperationException` (host-runtime work, as in the wasm tier).
- **Effectful provide-method bodies** are stubbed; pure delegations are real.
- Config **`default` values** are not applied (host loader territory).

## Verify

A JDK is required (not present in the dev environment):

```bash
python3 emit.py ../../examples/user_cache.ir.json > Components.java
javac -cp cordis4j-core.jar Components.java
```

The test suite asserts the emitted structure at the string level (no JDK needed).
