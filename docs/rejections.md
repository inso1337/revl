# Rejections — what the compiler refuses, and how to fix it

A rejection is a deliverable, not a failure mode (DESIGN.md §9): every
refusal names the guarantee it enforces — the `(G4)` in the message — and,
where one exists, carries a fix hint on the line below. The point is that an
author, or an agent, learns something from being refused.

This document catalogs the guarantee-code families. It is the prose twin of
two executable sources of truth:

* `examples/rejections/` — the spec corpus. One program per refusal, each
  naming its expected error in a header comment; `tests/test_frontend.py`
  compiles every file and holds it to that text.
* `src/revl/diagnostics.py` — the machine-facing projection. `GUARANTEES`
  says what each code is about, `FIXES` says how to satisfy it, and
  `revl explain <code>` prints the pair. A code with no `FIXES` entry still
  explains itself through `GUARANTEES`.

The fenced examples below are compiled by `tests/test_doc_examples.py`: a
`reject CODE` fence must be refused, and the diagnostic must classify as
`CODE`. If you edit a checker message, this page fails until its quotes
match — that is the gate working.

Codes are derived, not hand-maintained at ~100 raise sites: a rejection that
names its guarantee in the message or hint carries the tag through, and the
rest are classified by message shape (`_PATTERNS`). For the three rejections
that are the verdict of a whole-composition search — G4's emission fixed
point, G3's cycle, G2's provider table — the derivation itself is attached
to the diagnostic; see docs/why-traces.md.

## The families

<!-- docgen:guarantees begin -->
| code | guarantee | refused by |
| ---- | --------- | ---------- |
| G1 | declared access: a component reads only what it requires | lower |
| G2 | provision disjointness: one provider per key (per realm) | linker |
| G3 | acyclic dependencies: a cycle can never activate | linker |
| G4 | every mutation carries an inverse, or admits irreversibility with `emit` | lower |
| G5 | teardown cannot register effects | by construction |
| G6 | purity outside effect forms | parser/checker |
| G7 | derived LIFO teardown | by lowering (+ totality check) |
| G8 | the boundary surface is enumerable | parser (extern classification) |
| G9 | untrusted data cannot create authority without a declared declassification | lower (taint flow) |
| G-SECRET | a capability-bound secret never leaves its capability's own extern bodies through any revl construct or declared crossing | lower (taint flow) |
| G-SECRET-FLOW | a Secret[T] value never reaches a disclosure sink (a log, a serialization, an LLM prompt, an MCP return, an unapproved realm or an undeclared receiver); it crosses only at a declared Secret[T] receiver and downgrades only at a declared endorse[confidential] | lower (taint flow) |
| A1 | iteration boundaries exist only during activation | lower |
| A2 | no acquisition after a provision | linker |
| A3 | host-safe identifiers | lowering transform (renames, never refuses) |
| A5 | compensation accompanies an emission | by construction |
| A6 | provide-methods match the service signature | lower / compat gate |
| A8 | mid-body failure reverts and contains (L-Raise) | lower |
| A9 | a provide key is declared in the component's `provides` clause | lower |
| T1 | declared types are checked | checker |
| T2 | absence is Opt[T]; `null` has no type | checker |
| T3 | a hole is an obligation: it checks, but it never runs (docs/holes.md) | admission gate |
<!-- docgen:guarantees end -->

## G1 — declared access

A component body may only name what it declares: `requires` keys, config
fields, and its own `let` bindings. Anything else is refused — the library
equivalent is a runtime proxy error at best.

```revl reject G1
service Database { fn query(sql: Str) -> List[Row] }

component Logger {
  let rows = effect db.query("SELECT 1") undo rows.drop()
}
```

```
g1_undeclared_access.rvl:12: `db` is not a declared requirement of Logger
  component Logger requires <nothing> — add `requires db: <Service>`?
```

Fix: add the key to the component's `requires` clause, or drop the access.
The same rule refuses an undeclared name inside a function body
(`` `nobody` is not declared in this function ``) and an `intercept` of a
key the component does not require.

## G2 — provision disjointness

A key has at most one possible provider per realm: dependents bind to a key,
not to a component, so two providers would make the binding ambiguous
(paper Def. 43).

```revl reject G2
service Database { fn query(sql: Str) -> List[Row] }

component PgDatabase provides db: Database {
  let pool = effect Pool.open("pg://", 4) undo pool.close()
  provide db { fn query(sql) = pool.query(sql) }
}

component SqliteDatabase provides db: Database {
  let pool = effect Pool.open("sqlite://", 4) undo pool.close()
  provide db { fn query(sql) = pool.query(sql) }
}
```

```
provision conflict: key `db` is provided by both PgDatabase and SqliteDatabase (G2)
  why `db` has more than one provider:
    PgDatabase      g2_provision_conflict.rvl:10  provides `db`
    SqliteDatabase  g2_provision_conflict.rvl:15  provides `db`
```

Fix: one provider per key per realm — withdraw one component, or `isolate`
them into different realms (then the same pair is legal; see
v2_same_realm_conflict.rvl for the realm-scoped variant). The why-trace
names both providers so nobody has to grep for the second one.

## G3 — acyclic dependencies

If Alpha requires what Beta provides and vice versa, neither satisfaction
predicate can ever hold. The linker refuses at compile time instead of
leaving both components permanently inactive (paper §6.5).

```revl reject G3
service A { fn ping(tag: Str) -> Str }
service B { fn pong(tag: Str) -> Str }

component Alpha requires b: B provides a: A {
  provide a { fn ping(tag) = b.pong(tag) }
}

component Beta requires a: A provides b: B {
  provide b { fn pong(tag) = a.ping(tag) }
}
```

```
dependency cycle: Alpha -> Beta -> Alpha (G3)
  why `Alpha` is in a dependency cycle:
    Alpha -> Beta -> Alpha
      Alpha  g3_dependency_cycle.rvl:12  provides `a`
      Beta   g3_dependency_cycle.rvl:16  provides `b`
      Alpha  g3_dependency_cycle.rvl:12
```

Fix: break the cycle — split the interface, or move the shared state into a
third component both depend on. Import cycles between modules refuse with
the same code (`import cycle:`, v2_use_cycle.rvl).

## G4 — inverse or emit

Every mutation carries an inverse, or admits irreversibility with `emit`.
Four distinct programs violate it, all in `examples/rejections/`.

**A bare acquisition** — `effect` without `undo` where the callee is not
pure (g4_missing_undo.rvl):

```revl reject G4
component Leaky {
  config { url: Str }
  let pool = effect Pool.open(config.url)
}
```

```
effect has no `undo` and `Pool.open` is not pure
  write `effect Pool.open(...) undo <expr>`, or mark the call `emit`
  if it deliberately crosses the system boundary (G4)
```

**An unmarked emission call** — the operation is declared `emission fn`,
so the call site must say `emit` (g4_unmarked_emission.rvl):

```revl reject G4
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}

component Auditor requires db: Database {
  effect db.execute("CREATE TABLE audit (id INT)")
  undo   db.execute("DROP TABLE audit")
}
```

```
call to emission `db.execute` must be marked `emit` (G4)
```

**A provider that exceeds a plain declaration** — a service declaration is
an *upper bound* on its providers' effects, because consumers bind to the
service and providers are hot-swappable (g4_emission_not_declared.rvl):

```
`Cache.put` is declared plain, but this implementation reaches `db.execute`
```

**A provider that emits through an undeclared capability** — the
declaration bounds *which* boundary the method reaches, not just whether
(g4_capability_not_declared.rvl):

```
`Cache.put` is declared `emission[db]`, but this implementation emits through `bus`
```

Fix: give the mutation an `undo`, or admit it is irreversible — `emit` at
the call site and `emission fn` on the service operation. The propagation
check is a least fixed point over the call graph; when it refuses, the
diagnostic carries the chain (docs/why-traces.md). An `acquire`-classified
extern without `undo` refuses under the same code
(v2_extern_acquire_no_undo.rvl).

## G5 — teardown cannot register effects

There is no refusing example, because there is no violating program:
`undo`/`compensate` bodies are pure expressions typed in `teardown` mode,
and `effect`/`emit` are statements that only type-check in `setup` mode —
an `effect` inside an `undo` is a parse error, not a G5 diagnostic. The
guarantee is by construction (DESIGN.md §5). The residue it guards against
at runtime is a host-framework concern; see docs/contract-errata.md
(the cordis-TS `assertActive` bug).

## G6 — purity outside effect forms

A component body contains only effect forms (`let`, `effect`, `emit`,
`fail`, `if`, `provide`). A bare expression statement has no effect to
record, so there is no syntactic position for unconfined work.

```revl reject G6
service Database { fn query(sql: Str) -> List[Row] }

component Peek requires db: Database {
  db.query("SELECT 1")
}
```

```
expected a statement (`let`, `effect`, `emit`, `fail`, `if`, `provide`), found 'db'
  revl bodies contain only effect forms — plain expressions have no effect to record (G6)
```

Fix: outside an effect form every statement is pure — bind the value with
`let`, or wrap the call in `effect ... undo ...`. Reassignment of a `let`
refuses under the same code (`cannot reassign `n` — it is `let`
(single-assignment)`, v2_let_reassignment.rvl).

## G7 — derived LIFO teardown

Teardown order is compiler-derived, so it cannot be written incorrectly:
the property is by lowering (DESIGN.md guarantee table, Thm. 16), exercised
by the runtime scenarios under `backends/`. The one checker-side lever is
totality: a `verified fn` (which a derived teardown may trust) must be
total, and recursion breaks that.

```revl reject G7
verified fn recurse(n: Int) -> Int {
  return recurse(n)
}
```

```
verified fn `recurse` is not total: it participates in direct or mutual recursion (verified totality, syntax-2.0 §7)
  use structural recursion on a structurally smaller value, or a syntactically bounded loop
```

Fix: make every recursive call structurally smaller.

## G8 — the boundary surface is enumerable

Everything that reaches the host must appear on the audit surface, and an
`extern` is exactly the escape hatch — so an extern must say what it is.
An unclassified one is refused:

```revl reject G8
extern fn f() = @py { pass }
```

```
unclassified extern — expected `pure`, `acquire`, or `emission` after `extern`
  classification is mandatory: `pure` has no observable effect, `acquire` must declare `undo`, and `emission` may declare `compensate`
```

Fix: keep the boundary enumerable — declare host code as an `extern` with a
`pure`/`acquire`/`emission` classification. Emissions that reach the
boundary without a declaration are caught earlier, by G4's propagation;
G8 is the reason those declarations are an upper bound (see
docs/capabilities.md §3).

## A1 — iteration boundaries exist only during activation

`await` diverts a component transition; a provide method runs while the
component is ACTIVE, where there is no transition to divert.

```revl reject A1
service Cache { fn get(key: Str) -> Opt[Str] }

component BadAwait provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn get(key) {
      await Job.run("lookup")
      return store.get(key)
    }
  }
}
```

```
`await` is only allowed in a component body
  a provide method runs while the component is ACTIVE; iteration boundaries (paper §4.3.2) exist only during activation (A1). Declare the operation `async fn` to `await` a host async value in a provide method (services 2.0, §5)
```

Fix: move the `await` into the component body, or declare the operation
`async fn` if it must suspend per-call.

## A2 — no acquisition after a provision

An effect acquired after `provide` would be reverted (LIFO) while
dependents can still call the service through the withdrawal guard —
provide-methods would run against a torn-down acquisition.

```revl reject A2
service Cache { fn get(key: Str) -> Opt[Str] }

component BadOrder provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key) }
  let extra = effect Map.new() undo extra.drop()
}
```

```
acquisition after `provide` — an effect acquired after a provision would be reverted while dependents can still call the service
  move acquisitions above the `provide` block (linker rule A2)
```

Fix: acquire everything before the first `provide`.

## A3 — host-safe identifiers

Never refused: names colliding with host keywords are *renamed*
(`class` → `class_`, `frame` → `frame_`) by a lowering transform, so the
source keeps the natural name and the emitted host code stays legal. The
positive test is `test_a3_host_colliding_names_are_renamed`. (`config` and
`await` reject as revl's *own* reserved words — unrelated to A3.)

```revl
component Renamer {
  let frame = effect Map.new() undo frame.drop()
  let class = effect Map.new() undo class.drop()
}
```

Fix, when the collision was deliberate: rename the source binding.

## A5 — compensation accompanies an emission

No refusing example: `compensate` is an *optional* slot (DESIGN.md §3.5 —
an emission "may declare" one) that the grammar binds only to an `emit`,
and `emit` requires a declared `emission` (G4). There is no
"compensation required but missing" program; the guarantee is by
construction. The lowering test is `test_a5_compensate_lowering`.

## A6 — provide-methods match the service signature

The service declaration is the source of truth for what a key offers: a
call to something it does not declare has no signature to check against.

```revl reject A6
service Database { fn query(sql: Str) -> List[Row] }

component Migrator requires db: Database {
  let n = effect db.execute("DROP TABLE old") undo db.query("ROLLBACK")
}
```

```
`db.execute` is not a method of service Database
```

Fix: a provide-method must match the service declaration — name, arity,
parameter types, `async`. The same amendment is enforced across upgrades by
the compat gate: a running provider that implements a method at a retyped
signature refuses the new manifest (`... implements it at \`Int\` (A6)`,
docs/service-compat.md; tests/test_service_compat.py).

## A8 — mid-body failure reverts and contains

`fail` is an activation-body transition: deliberate L-Raise only makes
sense while a component activation is loading, where the runtime can revert
the accumulated effects and land the fiber FAILED. Pure functions return
`Result` values instead.

```revl reject A8
fn f() -> Int {
  fail "not a component activation"
  return 0
}
```

```
`fail` is only allowed in a component activation body (A8)
  pure functions and tests return `Result` values; deliberate L-Raise is a component activation transition
```

Fix: put the `fail` in a component activation body, or model the outcome as
`Result[T]`.

## T1 — declared types are checked

The workhorse family: argument types, arity, returns, exhaustiveness,
field access — every shape mismatch refuses with a `T1` diagnostic. The
minimal case is a service argument of the wrong type:

```revl reject T1
service Database { fn query(sql: Str) -> List[Row] }

component Probe requires db: Database {
  let rows = effect db.query(42) undo db.query("cleanup")
}
```

```
`db.query` argument `sql` expects `Str`, got `Int`
```

Fix: make the types agree at the call site, or change the declaration.
Where a value flows from context the diagnostic carries `expected` /
`actual` fields for the machine-facing view. Twenty of the corpus entries
exercise T1 shapes — see `t3_` … `t20_` in `examples/rejections/`.

## T2 — absence is `Opt[T]`

`null` has no type in revl expressions; absence is modeled as `Opt[T]` and
unwrapped with `??`, `?.` or `match`.

```revl reject T2
service Cache { fn get(key: Str) -> Opt[Str] }

component BadNull provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn get(key) { return null }
  }
}
```

```
`null` has no type in revl — absence is `Opt[T]`
  use `None` for an absent optional, or restructure with `match`/`??` (syntax-2.0 §2; `null` remains legal only as a config default)
```

Fix: revl has no `null` — model absence as `Opt[T]` and unwrap with `??`,
`?.` or `match`.

## T3 — a hole is an obligation

A `hole` checks and compiles — that is the point (docs/holes.md): drafts
compile so the checker can comment on the whole file. But a hole can never
run: admission refuses a draft with open holes, and `revl compile` lists
every obligation on stderr:

```revl
service Cache { fn get(key: Str) -> Str }

component C provides c: Cache {
  let pool = effect hole[Db] "a pooled connection" undo pool.close()
  provide c {
    fn get(key) = hole "look up in the store"
  }
}
```

```
2 open holes — this is a draft: it compiles, admission will refuse it (docs/holes.md)
  draft.rvl:4: expects `Db` — "a pooled connection"
  draft.rvl:6: expects `Str` — "look up in the store"
```

The obligations are severity `obligation`, not `error` — an agent should
treat the list as remaining work, not as a rejection. Fix: fill the hole in.

## T4 — no field read off an erased value (`Any` / `Value`)

An erased value — a `json_parse` result, any `Any` or `Value` — has no known
fields, so a plain field read on it (`v.kind`) is the silent-divergence class
items 279/299 chased: the py tier raises `KeyError` on an absent key, the ts
tier yields `undefined`, and neither is a defensible total answer for a field
the author spelled as present. The frontend refuses it, uniformly, before any
tier emits.

```revl reject T1
pub extern pure fn jp(s: Str) -> Any
  = @py { import json; return json.loads(s) }
  = @ts { return JSON.parse(s) }

fn read_kind(s: Str) -> Str {
  let v: Any = jp(s)
  return v.kind
}
```

```
field read `.kind` on a value of type `Any` — an erased value has no known fields
  bind it to a record type first (`let e: SomeRecord = …; e.kind` — an `Opt[T]` field then reads back the empty Opt on absence), or walk it with stdlib/value.rvl (`value_is_object(v)`, `value_opt(v, "kind")`, `value_field_or`)
```

Fix, two designed surfaces (roadmap item 380):

* bind the value to a **record type** and read the field there. A field whose
  declared type is `Opt[T]` then reads TOTAL on every tier — absent is the
  empty Opt, never a raise or a stray `undefined` — so `e.kind ?? "<none>"` is
  the one spelling for "may be absent" and it means the same everywhere;
* walk the erased value with the total **shape accessors** in `stdlib/value.rvl`
  — `value_is_object(v)` / `value_is_list(v)` / `value_is_scalar(v)` discriminate
  the shape, and `value_opt(v, "kind")` / `value_has(v, "kind")` /
  `value_field_or(v, "kind", d)` read a field tolerantly by string key (which
  also sidesteps the reserved-word-key mangle that started items 279/299).

## Everything else

Rejections that enforce no guarantee code exist too, and follow the same
message-plus-hint discipline: parse and lex errors (`expected ..., found
...` — classified `SYNTAX`), arithmetic definedness (`mod` by a literal
zero), integer literal range, lifecycle-test mistakes (`unknown component
Ghost`, `` `Kv` is already loaded ``), realm-label rules, and module-system
refusals (missing import, private access). Each has its entry in
`examples/rejections/`; `REJECTIONS` in tests/test_frontend.py is the index.

Two commands close the loop from either side:

* `revl explain <code>` — what a code means and how to fix it. An unknown
  code answers with the roster, so a typo is one command from the right
  code.
* `revl compile --json-diagnostics <file>` — the failed compile as a
  structured document: stable `code`, `category`, `guarantee`, the
  expected/actual pair where one exists, and the why-trace for search-based
  verdicts.
