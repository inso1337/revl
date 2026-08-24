# Findings — FR-4 follow-up: non-String values in the java tier's host `Map`

Branch: `agent/fr4-java-map` (FEATURE-REQUESTS.md FR-4 — the java tier follow-up
filed in `dogfood/findings-harness-followup.md`; roadmap item 77(c) landed on
rust in `agent/fr4-rust-map`, this is the java mirror).

The harness's session ledger is `Map[Str, List[Msg]]`; the java tier emitted a
hardcoded `public static final class Map { HashMap<String, String> ... }`, so
the ledger's `store.get(id)` was `Optional<String>` where `List<Msg>` was
needed, and the emitted java failed javac:

```
incompatible types: bad return type in lambda expression
  return this.store.get(id).orElseGet(() -> java.util.List.of());
no instance(s) of type variable(s) E exist so that List<E> conforms to String
```

## What was done

- The host `Map` stub (`backends/java/emit.py`, `_emit_map_runtime`) is now
  `public static final class Map<V>` with `HashMap<String, V>`,
  `insert(String key, V value)`, `get(String key) -> Optional<V>`, and a
  generic static factory `public static <V> Map<V> create()` (the `Map.new()`
  rename is unchanged). `_HOST_STUBS["Map"]` documents the generic signatures.
- The value type is learned **per site from the IR**, mirroring the rust tier:
  `_component_map_values` walks every `store.insert(k, v)` call across the
  component (activation body + every provide method, both the v1
  `target`/`method` call shape and the 2.0 `callee`-field shape) and types `v`
  with a small oracle: method params (from the service declaration), literals,
  non-empty lists, `push`/`length`/`indexOf`/`to_int`/`split`/`join`/`slice`/
  `concat` stdlib results, free-fn return types, required-service return types,
  record literals (via `_V3Ctx.record_type_for_fields`), ADT cases (the IR
  carries the ADT name on the node), record-field accesses (`row.name`), and
  config fields. Anything unproven falls back to `Str` — the historical
  surface — so String-valued maps emit byte-identically.
- The learned type is carried into the emitted java at the three places javac
  cannot infer it: provider-struct fields become `Map<java.util.List<Msg>>`,
  constructor parameters match, and the `apply()` local is pinned
  `Map<java.util.List<Msg>> store = Map.create();` instead of `var` — `var`
  would infer `Map<Object>` from the generic static factory and silently erase
  every insert/get's static type. Both emission paths are covered: the modern
  path (`_bind_type` + `_emit_component_stmts`) and the legacy v1/v2 simple
  path (`_bind_decl_type`), which also handles v3 components that take the
  simple path (plain-call provide bodies).
- Coverage: `Map[Str, Str]` (byte-identical emission apart from the shared
  generic class shape), `Map[Str, Int]` (`Map<java.lang.Long>`),
  `Map[Str, Bool]`, `Map[Str, List[X]]`, record values (`Map[Str, Profile]`),
  and the ledger shape `Map[Str, List[Msg]]`. `Float`/`Bytes` values fall out
  of the same path.
- Golden impact: `backends/java/golden/user_cache.java` regenerated — the diff
  is exactly the shared-stub shape change (generic class, `Map<String>`
  pinned field/ctor/local); nothing else moved.
- Tests (backends/java/test_emit_java.py): structure asserts on the emitted
  ledger types, `javac` for the ledger shape (the FR-4 exit criterion),
  `javac` for Int/Bool/List/record values, and a JVM run on the stub reference
  runtime driving `Map[Str, Int]` and `Map[Str, List[Str]]` components
  (insert/get round-trip, absent-key fallback, LIFO teardown) — runtime proof,
  not just a compile, mirroring the rust tier's `cargo test`.

## 1. Refusal log

No `revl compile` refusals were hit. The harness ledger compiles cleanly; the
failure FR-4 describes is emission-time (javac "incompatible types"), and the
checker never sees it because host-object *results* are deliberately opaque and
component host-local calls bypass the host-family argument check (the same
G8 audit surface the rust agent documented).

One refusal-adjacent finding, shared with the rust tier: the checker's
`_HOST_ARG_SIG["Map.insert"]` still types the value parameter as `Str`
(`src/revl/typecheck.py`) — *wrong* for the ledger shape but unreachable from
component bodies. A future pure-`fn` body that calls `Map.insert(k, list)`
would be refused with a `Str`/`List[...]` mismatch — a `friction`-class false
negative (the diagnostic names `Str` when the tier now accepts any value).

## 2. Friction log

- `[blocker]` The Map value type is **not in the IR**, exactly as on rust:
  `Map.new()`'s acquire node carries no value type and the checker records the
  binding as the opaque family `Map`. The FR's "the frontend knows the value
  type" claim is only true if you squint — the type is *derivable* from the
  IR's `insert` sites, so the emitter had to learn it there. The java oracle
  is a port of the rust one, so this cost a port cycle, not a design cycle.
- `[slow]` `prev.push(msg)` cannot be typed from `infer_ir` (receiver `prev`
  is a let-local with no recorded type), so the oracle special-cases `push` →
  `List[arg0]` — the same anchor the rust tier uses. `insert` is the only
  reliable site; inference keyed on `get` results would be circular.
- `[nit]` `config.replicas` is reachable in the activation body but a bare
  `seed` is not — I wrote the first "config-valued map" probe as
  `effect b.insert("k", seed)` and the checker refused `seed` as "not a
  declared requirement" before I remembered v3 spells it `config.seed`. The
  diagnostic is honest but the first probe cycle was mine.
- `[nit]` `_bind_type` (modern path) and `_host_of` (legacy path) type
  bindings in two different places; genericizing the Map touched both, and the
  legacy path needed `render_type`-aware boxing (`_java_type_arg` for v1/v2,
  `_java_v3_type(..., boxed=True)` for v3) because the two renderers disagree
  on how `Int` renders as a type argument (`Long` vs `java.lang.Long`). Both
  compile; the split is a pre-existing structural cost, not new.

## 3. What revl gave you

- The **existing javac gate** (`_javac_compile` against the in-repo cordis4j
  stubs) turned "does the ledger compile?" into a one-line test — the same
  fast, offline-first green baseline the rust tier's `cargo check` provides.
- **javac's diagnostics were the oracle**: the very first compile of the
  pre-change ledger named exactly where the value type leaked
  (`List<E> conforms to String`), and after the fix, `-Xlint:all` across all
  value shapes confirmed no unchecked/raw-type warnings crept in from a
  mis-typed `var`.
- The **byte-equality golden policy** (tests/test_goldens.py folds the java
  golden into the default suite) is what makes "String maps stay byte-identical
  modulo the shared stub shape" a *checked* claim rather than an aspiration.

## 4. Time-to-green

Compile→fix cycles: ~4 (ledger javac error → generic class; `var`-inference
erasure risk → pin every declaration site; legacy-path type split → renderer-
aware boxing; golden byte-equality → regen + review). Longest single stall was
confirming how javac treats `var x = Map.create();` on a generic static factory
(it infers `Map<Object>`, which *compiles* the ledger but erases the value
type — so the fix had to pin, not just compile). That was one scratch javac
file, not a read.

## 5. Cost ledger

- `spec-ambiguity` — FR-4's "the frontend knows the value type from the IR"
  is false at the frontend: no pass records it. The rust agent already decided
  the answer (emitter-side oracle, conservative String fallback); java re-used
  the decision, so this cost a port, not a second design cycle. The FR's own
  suggested shape (carry the type from the checker's unification into the IR)
  would still be the one change that removes the oracle entirely.
- `diagnostic` — `Map.insert`'s checker signature still says the value is
  `Str`; javac's "List<E> conforms to String" was the only place the real type
  was visible, and it only fires at emit/compile time, not at `revl compile`.
- `tooling` — the pre-commit hook runs the full `pytest tests/ -q` (frontend +
  emitted-code validation); it exceeded the sandbox timeout in the rust agent's
  run and this one ran the suite manually instead. A `--quick` hook mode would
  have saved the wait; not a blocker here.

Single change that would have cut the most cost: the same one the rust agent
named — carrying the Map value type from the checker's unification into the IR,
so both emitters consume it instead of each re-deriving it from `insert` sites.
