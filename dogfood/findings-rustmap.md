# Findings — FR-4: non-String values in the rust tier's `Map`

Branch: `agent/fr4-rust-map` (roadmap item 77(c), FEATURE-REQUESTS.md FR-4).

The harness's session ledger is `Map[Str, List[Msg]]`; the rust tier emitted a
hardcoded `pub struct Map { inner: HashMap<String, String> }`, so the ledger's
`store.get(id)` was typed `Option<String>` where `Vec<Msg>` was needed, and the
emitted rust failed E0308/E0599 at `cargo check`.

## What was done

- The host `Map` stub (`backends/rust/emit.py`, `_emit_host_stubs`) is now
  `pub struct Map<V>` with `impl<V> Map<V>` for `new/drop_/insert/remove` and
  `impl<V: Clone> Map<V>` for `get` (`.cloned()` needs `V: Clone`; every revl
  value derives Clone on this tier).
- `Map::get`/`Map::remove` now borrow their key (`&String`) instead of moving
  it — a component that reads then writes the same key (the ledger's `append`)
  kept hitting E0382 ("borrow of moved value") once the value-type errors were
  fixed. The renderer emits `&` on the first argument of a Map binding's
  `get`/`remove`, detected through a `Map[...]` marker registered in
  `env.v3_ctx().var_types` (the same per-method table that already types `+`).
- The value type is learned **per site from the IR**, mirroring the go tier's
  `map[string]V` and the py `dict`: `_map_value_rust_type` walks every
  `store.insert(k, v)` call across the component (activation body + all
  provide methods) and types `v` with a small oracle (params, literals, lists,
  `push`/`length`/`split`/… stdlib results, free-fn and required-service
  return types, record literals, ADT cases). The oracle is deliberately
  conservative: anything unproven falls back to `String`, so String-valued
  maps emit exactly as before.
- The learned type is carried into the emitted rust at the two places rustc
  cannot infer it: provider-struct fields become `Arc<Map<Vec<Msg>>>`, and
  the constructor is pinned `Map::<Vec<Msg>>::new()` (a binding no provider
  struct captures, or one used only for `get`, would otherwise leave `Map<_>`
  open — E0282).
- Coverage: `Map[Str, Str]` (byte-identical golden), `Map[Str, Int]`,
  `Map[Str, Bool]`, `Map[Str, List[X]]`, and record values
  (`Map[Str, Msg]` / `Map[Str, List[Msg]]`). `Map[Str, Float]`/`Bytes` fall
  out of the same path.
- Golden impact: `backends/rust/golden/user_cache.rs` and
  `backends/rust/placement_runner/src/components.rs` were regenerated. The
  diff is the struct shape change (`Map` → `Map<V>`, two impl blocks, borrow
  signatures, `Arc<Map<String>>`, `Map::<String>::new()`). The placement
  snapshot's diff also carries a **pre-existing staleness**: the committed
  `components.rs` predated the current emitter's real `Pool` stub, so
  regenerating brings that section up to date too (the runner always
  regenerates from the IR at build time — `src/revl/placement.py::_build_rust`
  — so the snapshot is convenience, not source of truth).
- Tests (backends/rust/test_emit_rust.py): structure asserts on the emitted
  types, `cargo check` for the ledger shape, `cargo check` for
  Int/Bool/List/record values, and a `cargo test` that drives a `Map[Str, Int]`
  and a `Map[Str, List[Str]]` component through the real cordis-rs runtime
  (insert/get round-trip, absent-key fallback, teardown) — runtime proof, not
  just a compile.

## 1. Refusal log

No `revl compile` refusals were hit. The harness ledger compiles cleanly; the
failure FR-4 describes is emission-time (rustc E0308/E0599), and the checker
never sees it because host-object *results* are deliberately opaque
(`docs/contract-errata.md`, G8 audit surface) and component host-local calls
bypass the host-family argument check (`lower.py`, host-provenance path).

One refusal-adjacent finding: `_HOST_ARG_SIG["Map.insert"]` types the value
parameter as `Str` (`typecheck.py`), which is *wrong* for the ledger shape
but unreachable from component bodies. If a future author calls
`Map.insert(k, list)` in a pure `fn` body, the checker would refuse it with a
`Str`/`List[...]` mismatch — a `friction`-class false negative (the diagnostic
names `Str` when the tier now accepts any value type).

## 2. Friction log

- `[blocker]` The Map value type is **not in the IR**. `Map.new()`'s acquire
  node carries no value type; `store.get(id)` infers to `None` in
  `infer_ir`; the checker's type env records the binding as the opaque family
  `Map`. The "frontend knows the value type" claim in FR-4 is only true if you
  squint: the type is *derivable* from the IR's `insert` sites, so the rust
  emitter had to learn it there rather than consume a frontend annotation.
- `[blocker]` Once the value-type errors were fixed, the next rustc failure
  was E0382 ("borrow of moved value"): `get`/`remove` took `key: String`
  (owned), so the ledger's `let prev = store.get(id) …; store.insert(id.clone(),
  …)` moved `id`. Go/TS/py have no ownership, so only rust hits this; the fix
  (borrow the key) is invisible to the other tiers.
- `[slow]` `prev.push(msg)` cannot be typed by `infer_ir` (receiver `prev` is
  a let-local with no recorded type, and `push`'s result with an unknown
  receiver is `None`), so the oracle special-cases `push` → `List[arg0]`.
  The same shape would defeat any inference keyed on `get` results; `insert`
  is the only reliable anchor.
- `[nit]` The committed `placement_runner/src/components.rs` was stale
  (older `Pool` fake); regenerating per the task produced a larger diff than
  the Map change alone. Not caused by this work; documented so the reviewer
  does not attribute it to FR-4.
- `[nit]` The pre-commit hook runs the full `pytest tests/ -q` + conformance
  matrix and exceeded the 60 s sandbox timeout; the commit had to be retried
  after running the suite manually.

## 3. What revl gave you

- The **conformance matrix** (`tools/conformance.py`) reported `rust 0 gaps`
  after the change — the emitter's refusal surface is unchanged, which is the
  strongest available signal that the genericization did not silently break a
  construct.
- The **existing cargo-gated tests** (`test_emit_rust.py`) caught nothing
  wrong with my change but gave a fast, offline-first green baseline to diff
  against (byte-identical golden test, scenarios, stdlib semantics).
- The **rustc diagnostics themselves** were the oracle: E0308/E0599 told me
  exactly where the value type leaked, and E0382 told me the key had to
  borrow. The emitted code compiles only because the emitter now writes the
  type the checker *would* have written had the IR carried it.

## 4. Time-to-green

Compile→fix cycles: ~5 (ledger E0308/E0599 → generic struct; E0382 move →
borrow the key; E0282 risk on unconstrained `Map::new` → turbofish; golden
byte-equality → regen; runner snapshot → regen). Longest single stall was
designing the value-type learning pass: the IR has no type, `infer_ir` cannot
type the ledger's expressions, and the checker's family surface is opaque —
choosing "learn from `insert` sites with a conservative fallback to String"
took the most reading, not the most code.

## 5. Cost ledger

- `spec-ambiguity` — FR-4 says "the frontend knows the value type from the
  IR", but no frontend pass records it; had to decide between extending the
  checker's type env (risky for the admission gate) and an emitter-side
  oracle (self-contained). Chose the latter; the decision cost one design
  cycle that a single line in the FR ("the value type is learned from each
  `insert` call site") would have removed.
- `diagnostic` — `Map.insert`'s checker signature still says the value is
  `Str`; rustc's E0308/E0599 were the only place the real type was visible.
- `tooling` — pre-commit hook timeout (60 s) forced a manual full-suite run +
  retry of the commit; a `--quick` hook mode would have saved the retry.
- `docs-gap` — nothing in docs/stdlib-2.0.md §Map documents the host Map's
  value type or that component host-local calls bypass `_HOST_ARG_SIG`; both
  had to be read out of `typecheck.py`/`lower.py`.

Single change that would have cut the most cost: carrying the Map value type
from the checker's unification (or at least from a lowering-time annotation)
into the IR, so the rust emitter consumes it instead of re-deriving it — the
FR's own suggested shape.
