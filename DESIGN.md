# revl — a language for spatiotemporal composability

**Status:** design draft v0.1 (2026-08-16) · pre-implementation

revl is a research language that makes *dynamic composition* — loading, unloading,
and hot-swapping components in a running system — a checked property instead of a
runtime discipline. It is the language-level realization of the paradigm
formalized in [*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper)
(the "Cordis paper") and implemented as a library by [Cordis](https://github.com/cordiverse/cordis).

The name is from **rev**ertib**l**e effects, the paradigm's signature construct.

---

## 1. Thesis

The Cordis paradigm rests on two runtime mechanisms:

- **Revertible effects** (temporal composability): every mutation of the shared
  environment carries an inverse the runtime tracks; unloading a component
  replays its inverses, so removal leaves no residue.
- **Reactive coeffects** (spatial composability): a component declares the
  dependencies it reads; the runtime activates it when they are provided and
  deactivates it when they are withdrawn.

The paper proves strong metatheorems about this model (recovery exactness,
withdrawal ordering, progress, confluence). But **every one of those theorems
rests on hypotheses no library can enforce**:

| Hypothesis (paper) | Library reality (Cordis/TS, cordis-py) |
|---|---|
| Confinement (Def. 48): a component writes nothing outside its context | any closure can reach a global |
| Witnessed inverses (Def. 8): the inverse actually reverts the effect | inverse is an unchecked callback |
| Declared-only access: reads go through the coeffect specification | proxy check at runtime, or nothing |
| Provision disjointness, acyclic dependencies | runtime error at best |
| Lifecycle reentrancy invariants (§4.3) | hand-hardened after the fact¹ |

¹ Empirically: DeepSeek Harness ships a patched `fiber.ts` closing three
reentrant-disposal gaps ([vendor log, mod 6](https://github.com/deepseek-ai/deepseek-harness/blob/master/vendor/README.md));
upstream has the same territory pending in
[cordiverse/cordis#39](https://github.com/cordiverse/cordis/pull/39); the same
gaps reproduced in cordis-py and were fixed in
[geohotstan/cordis-py#1](https://github.com/geohotstan/cordis-py/pull/1).

**revl's job is to move that table's right column to the left.** The analogy we
hold ourselves to: C++ had RAII as a discipline; Rust made it a type system.
Cordis has revertible effects as a discipline; revl makes them a language.
The borrow checker governs *lexical* resource scope — revl's checker governs
*dynamic component* scope.

A second motivation is forward-looking: if components are increasingly written
by AI agents (the paper's "self-evolving agent harness" scenario, now shipping
as DeepSeek Harness), then *machine-checkable safety of a generated component
before it self-deploys* matters more than syntax familiarity. revl is designed
to be the language such a harness could require its own components to pass.

## 2. Design pillars

1. **The component is the compilation unit.** Its signature — what it requires,
   what it provides — is the whole of its interface to the world.
2. **Effects are syntactically paired with inverses.** You cannot write the
   mutation without writing (or deriving) the undo. Composite teardown is
   compiler-derived, LIFO, and not expressible incorrectly.
3. **Confinement by construction.** There is no global mutable state and no
   ambient I/O. Everything a component reaches, it reaches through a declared
   capability. "Undeclared access" is a type error, not a proxy trap.
4. **The lifecycle is the calculus.** Operational semantics follow the paper's
   §4.3 rules (Reloading/Unloading transition states, divert, failure routing,
   withdrawal guard) with the known reentrancy hardening as *spec*, not patch.
5. **Don't build a runtime.** revl compiles to an existing, hardened Cordis
   runtime (tiered backends, §8). The language is the checked front-end.

### Non-goals (v0)

- Raw native code generation (see §8 — the Wasm component model is our "native").
- General-purpose ergonomics: revl v0 is for writing *components*, not
  applications-at-large; the host application assembles compiled components.
- Isolation realms and interception (§3.2.3 of the paper) — deferred to v1.
- Verifying inverse *semantic* correctness in general (undecidable); v0 checks
  presence and shape, §6 sketches the opt-in verification story.

## 3. Language tour

### 3.1 Services (coeffect interfaces)

A *service* is the typed interface behind a coeffect key — what the paper calls
the coeffect at key *k*.

```revl
service Database {
  fn query(sql: Str) -> List[Row]
  fn execute(sql: Str) -> Int
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  fn put(key: Str, value: Str)
}
```

### 3.2 Components

A component names its requirements and provisions in its header. The body is
its *activation*: it runs when every requirement is satisfied, and everything
it does is undone — in derived, LIFO order — when it deactivates.

```revl
component PgDatabase provides db: Database {
  config { url: Str, pool_size: Int = 10 }

  let pool = effect Pool.open(config.url, config.pool_size)
             undo   pool.close()

  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
  }
}
```

```revl
component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      db.execute("INSERT INTO cache_log VALUES ($key)")   // emission, §3.5
    }
  }
}
```

Reading guide:

- `requires db: Database` — the coeffect specification. `db` is in scope in the
  body, typed as `Database`; the component cannot activate until some active
  component provides it, and is deactivated (in dependency order, per the
  paper's Theorem 63) when its provider leaves. **`db` stays readable
  throughout this component's own teardown** — the withdrawal guard is part of
  the semantics.
- `provides cache: Cache` — the provision. Installed as an effect (with a
  compiler-derived inverse that withdraws it); the checker enforces provision
  disjointness across a composition and rejects dependency cycles.
- `effect E undo U` — a revertible effect. `E` runs at activation; `U` is
  pushed onto the component's accumulator. `let x = effect ... undo ...` binds
  the acquisition; the `undo` expression may refer to it.

### 3.3 The lifecycle is implicit

There is no `activate()`/`deactivate()` to write or call. A compiled component
is a *description*; the runtime holds the fiber state machine
(`Pending → Loading → Active → Unloading`, with divert and failure edges per
the paper's Figure 2). What the language guarantees statically:

- code outside `effect` forms is pure (no observable mutation to revert);
- every mutation is inside an `effect` form and therefore accumulated;
- nothing can register an effect during teardown (there is no syntactic
  position for it — `undo` bodies type-check against a context with no effect
  capability, which is the language-level form of the `INACTIVE_EFFECT`
  hardening).

### 3.4 Iteration boundaries and asynchrony

An activation body may `await`. Every `await` and every `effect` form is an
*iteration boundary* in the paper's sense (§4.3.2): the compiled state machine
can be diverted there, reverting exactly the effects accumulated so far. This
is the compiler-lowered form of Cordis's async-generator effects — no closure
per step; one state machine per component with inverse slots in its frame.

```revl
component Migrator requires db: Database {
  let lock = effect db.execute("SELECT pg_advisory_lock(42)")
             undo   db.execute("SELECT pg_advisory_unlock(42)")
  await run_migrations(db)          // divert point: lock is reverted if we go
}
```

### 3.5 The system boundary: acquisitions vs emissions

Following the paper's §6.1, revl distinguishes what an inverse can and cannot
mean, *in the types*:

- `effect E undo U` — an **acquisition**: revertible, tracked.
- `emit E` — an **emission**: crosses the boundary (bytes sent, rows written to
  a shared table). No inverse exists; the checker *requires* the `emit` marker
  wherever a declared-`emission` operation is called, so irreversibility is
  visible at every call site. Services declare which operations are emissions:

```revl
service Database {
  fn query(sql: Str) -> List[Row]            // observation: pure w.r.t. Γ
  emission fn execute(sql: Str) -> Int       // leaves the boundary
}
```

An `emission` may declare a `compensate` clause (saga-style), which composes in
LIFO order like an inverse but is typed distinctly — the metatheory's exactness
claims apply only to acquisitions, and the types keep the two apart.

### 3.6 Foreign functions

FFI is the only door out of confinement, and it must classify itself:

```revl
extern acquire fn listen(port: Int) -> Socket undo close(socket)
extern emission fn send(sock: Socket, data: Bytes)
extern pure fn sha256(data: Bytes) -> Bytes
```

An `extern` without a classification does not compile. This is where the
honesty lives: revl cannot verify the foreign inverse, but it can force every
escape hatch to be declared, enumerable, and auditable — `revl audit` lists a
component's entire boundary surface.

## 4. Checked guarantees (the point of the language)

| # | Guarantee | Checked | Paper anchor |
|---|---|---|---|
| G1 | Every requirement is declared; undeclared access cannot be written | compile | Def. 25 |
| G2 | Provision disjointness in a composition | compile (link) | Def. 43 |
| G3 | Dependency cycles rejected | compile (link) | §6.5 |
| G4 | Every mutation carries an inverse or an `emit` marker | compile | Def. 8, §6.1 |
| G5 | Teardown cannot register effects | by construction | DS mod 6 / PR #39 |
| G6 | Code outside effect forms is pure (confinement) | compile | Def. 48 |
| G7 | Derived teardown is LIFO-complete over accumulated effects | by lowering | Thm. 16 |
| G8 | Boundary surface (externs, emissions) is enumerable | compile | §6.1 |

"Compile (link)" means checked when a *composition* (a set of components) is
assembled — the language has a linker phase precisely so G2/G3 are static even
though loading is dynamic: a composition manifest type-checks as a whole.
Dynamically-added components re-run the link check against the running
manifest before admission — the runtime gate exists, but it is the *same
checker*, not a separate weaker one.

## 5. Type system sketch

- **Context rows.** A component body types against a row `Γ = {db: Database,
  ...}` fixed by its `requires` clause — row-typed coeffects in the sense the
  paper attributes to Koka-style rows (§6.7). There is no subsumption trickery
  in v0: exact rows, width subtyping later.
- **Effect capability as an index.** Judgments carry a mode `m ∈ {setup,
  teardown, pure}`. `effect`/`emit` forms are only well-typed in `setup` mode;
  `undo`/`compensate` bodies type in `teardown`; `provide` method bodies type
  in `setup` (they run while active and may install effects, which join the
  accumulator — the paper's coeffect-operations-are-effects synergy).
- **Purity by default.** The expression language is pure; mutation exists only
  via effect forms and service calls. This is what makes G6 a type-level fact
  rather than an escape analysis.
- **No first-class context.** The context is not a value; you cannot store,
  return, or alias it. This closes the leak the paper's §6.7 names (a
  component reaching another's context through a closure) syntactically.

Formalization plan: elaborate revl to a kernel calculus that is exactly the
paper's §4 calculus with a typed effect language in place of opaque effect
functions; the metatheorems then transfer by construction. This — not novelty
of type rules — is the research claim: *a surface language whose every
well-typed program satisfies the calculus's hypotheses*.

## 6. What remains unchecked, and the story for it

- **Semantic inverse correctness** (`U` really reverts `E`): undecidable in
  general. v0: trusted for externs, derived (hence correct) for compositions of
  checked primitives. v1 sketch: an opt-in `verified` marker for effects over
  datatypes with algebraic undo laws (insert/remove on maps, counters), checked
  by property tests generated from the effect signature; `tests/paper/` in
  cordis-py shows the shape.
- **Independence/commutativity of keys** (paper Def. 39, needed for
  reorderable recovery): v1 opt-in `commutative` annotation on service
  operations, discharged the same way. Without it, revl only promises the
  paper's LIFO guarantees — which is exactly what the runtime delivers anyway.

## 7. Compilation model

```
.rvl source ──parse──▶ AST ──check──▶ typed AST ──link──▶ composition
                                        │                    │
                                        ▼                    ▼
                              per-component lowering   manifest (G2/G3)
                                        │
                                        ▼
                        backend: cordis-py component (v0)
                                 wasm component (v1)
```

Lowering (v0, cordis-py backend):

- `component` → a plugin dict `{"name", "inject", "apply", "Config"}`;
  `requires` → `inject`, `config {}` → a schema, body → `apply` compiled to an
  async generator whose `yield`s are the inverses — landing exactly on
  `ctx.effect`'s iterator protocol, so divert-at-boundary comes from the
  runtime for free.
- `provide` → `ctx.provide`/`ctx.set` with the derived withdrawal inverse.
- Service method calls on required capabilities → calls through the committed
  view (`ctx.<name>`), which is what keeps them readable during teardown.
- `emit` → plain call (tracked as `id`), `compensate` → accumulator push typed
  apart in the manifest.

The backend contract is small (install effect with inverse; provide/read keys;
reactive refresh) and documented, so backends are swappable — this is what
makes the Wasm tier a codegen project rather than a redesign.

## 8. Backend tiers ("shouldn't it compile to native?")

Deliberately not, and not first. The paradigm's hardest runtime requirement is
*unloading code and reclaiming everything it owned*. Raw native (`dlopen`/
`dlclose`) offers no per-component heap, no reclamation guarantee, and no
confinement — it is the paradigm's known-hard case, and rebuilding a hardened
lifecycle runtime is precisely the multi-year cost this project's architecture
avoids (§2.5).

| Tier | Backend | What it proves |
|---|---|---|
| v0 | cordis-py | the semantics and the checker, on a runtime we have hardened ourselves and whose paper-conformance suite doubles as ours |
| v1 | Wasm component model | performance *and* enforcement: imports-at-instantiation are coeffects made physical, instances drop cleanly, and confinement stops being advisory |
| — | raw native | non-goal until something the Wasm tier cannot do demands it |

Precedent: Rust bootstrapped on OCaml, Kotlin on the JVM, TypeScript on JS.
Hosted first is how languages are born; native is graduation, and for this
paradigm the graduation target is Wasm, which is "native" with the two
properties we cannot give up (confinement, reclaimable instances).

## 9. v0 milestone

**One checked component compiles, links, runs, hot-swaps, and provably
reverts** on cordis-py, and each guarantee G1–G8 has a test that shows a
program *rejected* (or an outcome forced) that the library version accepts:

1. Grammar + parser (hand-written recursive descent; the grammar is small).
2. Checker: rows, modes, effect/emit classification (G1, G4–G6, G8).
3. Linker: composition manifest (G2, G3).
4. Codegen: cordis-py backend (§7).
5. Demo: `PgDatabase` (stubbed pool) + `UserCache` — swap the database
   provider at runtime; observe the cache deactivate → reactivate; unload
   everything; assert the environment is exactly recovered.
6. Negative suite: one minimal source file per guarantee, each failing to
   compile with a good error message. **The error messages are a deliverable**
   — a research language earns readers through its rejections.

Out of scope for v0: realms/interception, `verified`/`commutative`
annotations, Wasm tier, self-hosting, editor tooling beyond syntax
highlighting.

## 10. Open questions

- Surface syntax for *replacing* a provision (rolling update / service broker,
  paper §6.2) — language construct or library pattern on top?
- Should `config` changes be diffable in the language (reload-on-change per
  field, like the Cordis loader) or always restart-the-component in v0?
- How much of the linker's manifest should be a stable, documented format?
  (It is the natural interchange point with agent harnesses that want to admit
  components at runtime.)
