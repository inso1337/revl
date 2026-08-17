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

## Limits (tracked in docs/v2.0-roadmap.md)

- **ir_version 2** realms are emitted as `Context::isolate_with` +
  `Inject::require_with`; **ir_version 3** records/variants/functions/match/
  externs/tests are emitted as Rust items.
- **Host objects** (`Pool`/`Map`/`Job`) are a minimal real runtime. `Map` is a
  thread-safe `HashMap<String, String>`; `Pool` is a deterministic in-memory
  fake; `Job::run` is an async no-op.
- **Effectful provide-method bodies** compile to real `self.ctx.effect(...)`
  calls; the impl struct carries `Arc<Context>` and cloned requirement handles.
- Config **`default` values** are emitted as `impl Default for <Comp>Config`;
  the plugin body reconstructs its local config through `..Default::default()`.
- Component **`await`** steps use `plugin_async` and lower to `.await`.
  `await` steps inside provide methods are still rejected (A1).

## Verify

```bash
python3 emit.py ../../examples/user_cache.ir.json > lib.rs   # emit
# build a scratch crate against cordis-rs:
cargo check        # with Cargo.toml from `cargo_toml()` + this lib.rs
```
