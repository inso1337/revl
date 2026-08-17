# revl → cordis-rs backend

Emits idiomatic Rust targeting **[cordis-rs](https://docs.rs/cordis-rs)** — a Rust
port of `@deepseek-ai/cordis` 4.x (the same runtime model the TypeScript backend
targets). `emit(ir) -> str` produces one Rust module (crate root); `cargo_toml()`
produces a matching `Cargo.toml`.

## Mapping (DESIGN.md §7 — the backend contract)

| revl | Rust (cordis-rs) |
|---|---|
| `service` | `pub trait <Name>: Send + Sync { fn <m>(&self, …) -> … }` |
| `component` | `pub fn <snake>() -> PluginHandle` via `plugin_sync` |
| `requires` | `Inject::new([…])` + `ctx.require::<Box<dyn Svc>>("key")?` |
| `provides` | `impl <Svc> for <Comp><Key> { … }` + `ctx.provide("key", Box::new(impl))?` |
| `effect E undo U` | `let x = Arc::new(E); ctx.effect(label, move || { U; Ok(()) })?;` |
| `emit` | plain method call |
| `format` | `format!(…)` |

Services are `Box<dyn Trait>` because cordis-rs `require`/`provide` take a
`Sized` `T`; `Box<dyn Trait>` is the sized, dynamically-dispatched equivalent of
the TS runtime's object-valued services.

## Spike limits (tracked in docs/v2.0-roadmap.md)

- **ir_version 1 only.** v2 realms and v3 types/functions are rejected with a
  clear error (they are the follow-up).
- **Host objects** (`Pool`/`Map`/`Job`) are emitted as opaque stubs whose bodies
  `todo!()` — their real Rust forms are host-runtime work (like the wasm tier's
  "host builtins").
- **Effectful provide-method bodies** are stubbed `todo!()` (they need a
  ctx-carrying design). Pure delegation bodies are emitted for real.
- Config **`default` values** are not applied (`Plugin::validate_config`
  territory, not exposed by `plugin_sync`).

## Verify

```bash
python3 emit.py ../../examples/user_cache.ir.json > lib.rs   # emit
# build a scratch crate against cordis-rs:
cargo check        # with Cargo.toml from `cargo_toml()` + this lib.rs
```
