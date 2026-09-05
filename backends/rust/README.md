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

## Witnessed teardown (`RevlTeardown`) — items 243 / 247 / 324

A `witnessed[..]` extern or an `emit … compensate` step compiles to the
per-activation `RevlTeardown` accumulator (`_revl_teardown_preamble` in
`emit.py`; design in `docs/design/243-witnessed-externs.md` and
`docs/design/teardown-contract.md`). It is fully gated: a program using neither
emits byte-identically to before the slice — no `RevlTeardown`, no
`revl_teardown_begin`.

- **`committed`** is the abort-vs-commit discriminator. Unlike cordis-py (which
  flips its `_committed` lazily inside `Frame.drain` at teardown), the rust tier
  flips `committed` **eagerly**, right before `apply` returns `Ok`
  (`_emit_teardown_commit`). A `transactional`/`compensation` disposer reads it
  at disposal time: discharge on commit, replay/queue on abort.
- **Activation-body** witnessed effect → a `ctx.effect` transactional disposer
  yielded into cordis-rs's LIFO unload.
- **Provide-method body** witnessed effect (item 324, the per-tool-call H1
  gate) → the extern's declared inverse is registered on the *enclosing
  component's* activation frame, recovered through `revl_teardown_of(&self.ctx)`
  (the same fiber `revl_teardown_begin` extended). It is a fire-and-forget
  sibling `self.ctx.effect` disposer. **No park-for-drain is needed** the way py
  requires: rust's eager commit means the commit bit is already settled by the
  time a per-call method registers, so the sibling disposer observes it
  correctly in the fiber's single LIFO unload — the premature-disposal window
  that forces py to park (`_deferred_transactional`) does not exist here.
- **`revl_abort(label)`** is the out-of-band abort seam (the faithful mirror of
  py's `_FRAME_BY_CTX` + `_sole_frame`). A session-level reject runs outside the
  fiber; the activation's `RevlTeardown` lives on its private extended context
  (`Context::extend` derives a child the fiber's own context cannot see), so a
  weak, label-keyed registry (`REVL_TEARDOWN_REGISTRY`, keyed by
  `<component>.teardown.phase2`) is the reach-in. `revl_abort` clears
  `committed`, so the next unload replays every transactional inverse —
  per-tool-call and activation-body alike — instead of discharging it.

The runtime proofs run against the real cordis-rs crate in
`test_emit_rust.py`: `test_witnessed_teardown_loop_runs_on_real_cordis_rs`
(activation-body / compensation) and `test_method_witnessed_h1_runs_on_real_cordis_rs`
(the per-tool-call H1 persist/revert loop, mirroring
`tests/test_provide_method_witnessed.py`).

## Declared `Secret[T]` values and the runtime registry (421 F6 / #392)

A `Secret[T]` declaration authorises disclosure to the declared receiver. It
does not authorise any sink this tier keeps to hold a copy. This tier carried
only the emit-time `_REDACTED_SECRET` witness redaction (the `secret_witness`
referent written into the WAL); it had no RUNTIME registry, so a driver error, a
panic message, or any host string that quotes a held credential reached this
tier's trace verbatim. The frontend refuses the crossings it can see
statically; what is left is a value that reaches a host body legitimately (a
config credential handed to the component's own binding, a parameter the
service declared `Secret[T]`, a token an extern minted) and then turns up in a
message the host wrote.

The emitted crate carries a process-global registry for a document that
declares a `Secret[T]` anywhere (`_SECRET_MODE`; a secret-free document is
byte-identical to before), the same register-at-load shape the go tier uses.
rust (native) has no reflection, so — unlike java, which binds its runners to
the container reflectively — the emitted code populates the registry directly at
each declared end and the emitted sinks read through it in line:

* `revl_mark_secret(&config.<field>)` at the head of the plugin closure for a
  config field declared `Secret[T]` (the one door every load goes through);
* `revl_mark_secret(&<param>)` at the head of a provide method whose service
  declares that parameter `Secret[T]` (the receiver end);
* `revl_secret_result(...)` around an extern whose declared return was
  `Secret[T]` (the origin end); the verbatim `@rs` body keeps its signature
  under a private `_revl_secret_<name>` and the public one registers what it
  returns, so no call site changes;
* `revl_redact_text(String)` over the registered values (exact match, longest
  first, values under four bytes are not remembered) at every emitted-runtime
  free-form sink: the host trace `revl_stream_record` gathers (so a
  `<redacted:secret>` mark replaces a secret an item quoted) and the WAL
  descriptor arguments under `--record`.

`<redacted:secret>` equals the py tier's `confidential.REDACTED`, so a polyglot
composition redacts to the same marker whichever tier wrote the line.

`test_secret_registry.py` proves it two ways: emit-shape assertions that the
registration and the sink scrubs are present (and absent from a secret-free
document), and — under a rust toolchain — by RUNNING the emitted crate under
`cargo test` and grepping the real host trace `revl_stream_marks()`: the
registered value is absent and its marker present, an ordinary value beside it
is verbatim (no over-redaction), `revl_secret_result` returns its value
unchanged, and with nothing registered the value flows verbatim (non-vacuity).

The placement-runner seam-failure text (`src/main.rs`'s `FAILED … — {error}`)
and cordis `fail`-message paths are a hand-written harness, not emitted output;
closing those on a no-reflection tier without breaking byte-identity is a
separate design step, tracked as a follow-up under item 421 F6.

## Verify

```bash
python3 emit.py ../../examples/user_cache.ir.json > lib.rs   # emit
# build a scratch crate against cordis-rs:
cargo check        # with Cargo.toml from `cargo_toml()` + this lib.rs
```
