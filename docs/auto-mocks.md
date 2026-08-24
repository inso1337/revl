# Auto-mocks — every `service` declaration ships a free fake

A consumer component cannot be developed or tested until something provides
its `requires`. Today that something is a *real* provider — a live database, a
running cache — and standing one up is the setup tax that keeps a consumer's
`lifecycle test` from being cheap. `revl test --mock-requires` removes the
tax: from a `service` declaration alone it derives an **in-memory mock
provider** whose every operation returns an item-37-generated value of the
declared return type (typed, seeded, deterministic), so booting a composition
in **mock world** needs zero setup code.

```revl
service Database {
  fn ping() -> Bool
  emission fn execute(sql: Str) -> Int
}

service Api {
  emission fn query(sql: Str) -> Int
}

component App requires db: Database provides api: Api {
  provide api {
    fn query(sql) {
      emit db.execute(sql)
      return 7
    }
  }
}

lifecycle test "app runs in mock world" {
  load App            // `db` is auto-mocked; nothing else in this document provides it
  let n = call api.query("select 1")
  assert n == 7
  unload App
  assert no_residue
}
```

```
$ revl test app.rvl --mock-requires
auto-mocks — py reference tier (roadmap item 60)
  ...
PASS app runs in mock world [mock world] (1 emission recorded-not-crossed)
    recorded-not-crossed emissions (the mock counted each boundary crossing; it never made one):
      db.execute (Database) — 1 crossing recorded, none made
          would have emitted: sql='select 1'
ran 1 lifecycle test(s) in mock world: 1 passed, 0 failed
```

A lifecycle test against mocks is **stratum-3 unit testing**: the consumer is
booted and driven on a real runtime, while everything it depends on is a
typed fake. The registry (roadmap item 49) compounds it — a component's exit
tests can run against mocks at publish time, so a candidate needs no live
dependencies to earn its dossier.

## 1. The mock, by construction

The mock is **cheap by construction**, and that is the whole design bet: the
service's operation types are already known to the checker, and item 37's
type-derived generators (`src/revl/fault.py`, the `prop test` machinery)
already synthesize a value for any such type. The mock reuses those
generators verbatim — it invents no new value-synthesis machinery — seeding
each operation with a fixed, per-operation seed (`revl-mock:<service>:<key>:
<method>`), so a mocked run is reproducible, and the responses visit the same
typed value space a `prop test` searches (i64 edges, both `Opt` arms, every
ADT constructor, record fields).

The mock is a runtime-constructed cordis component — a `provide` +
`ctx.set`, exactly the shape the py emitter renders for a real provider — so
it plugs, resolves, reverts, and leaves no residue like any provider. It is
in-memory only: nothing here is emitted, so a mock is a test-time provider,
never code that ships.

A return type the generator cannot synthesize (an undeclared record, an
`extern` resource handle) does not crash the mock: it falls back to the
type's structural zero (`None` for `Opt`/scalars, `[]` for `List`, `{}` for
`Map`, `Ok(<inner>)` for `Result`), which is a valid inhabitant of the
declared shape.

## 2. Emissions are recorded, not crossed

A `service` operation classified `emission` crosses a boundary the moment it
runs (docs/backend-ir.md §6.1); a mock that made that crossing would defeat
the point of testing in isolation. So a mocked emission is **recorded, not
crossed**: the mock *counts* the crossing and records the arguments that
*would* have crossed, then returns a generated value like any other op. The
recording is itself an assertion surface — the mock-world report says exactly
what the composition would have emitted, so you can pin down "this activation
emits `db.execute` once, with this SQL" without a real database ever hearing
about it.

`assert no_residue` still means what it always means: a mock left loaded
(because its consumer was left loaded) shows up as residue, exactly like a
real provider's provision — the mock registers as a first-class provision and
is withdrawn only when its last consumer unloads.

## 3. How mock world fills `requires`

For each `load C` in a lifecycle test, every key in `C`'s `requires` that the
composition has not already provided is auto-mocked:

- a **real provider loaded earlier keeps its place** — the mock fills only
  unmet requires, so a test can mix real and mocked dependencies freely;
- two consumers sharing one `requires` key share one mock, and the mock is
  disposed when its last consumer unloads;
- `load` → `unload` → `load` again re-derives the same mock (same seeds, same
  responses), so reload is reproducible.

The test itself still drives the composition *through provided keys* — `call
key.op(…)` is checked against the document's loaded providers (syntax-2.0
§7.1) — so mock world tests the consumer through its own interface, with the
mock answering the calls the consumer makes on its requires.

## 4. Scope

Like `fault test` / `verified effect` / `prop test`, mock world runs on the
**py reference tier** (the only tier that boots a runtime for a lifecycle
test), in memory, runtime-constructed. A missing cordis-py runtime is a
*skip with a reason*, never a pass — the same rule as the fault sweep. The
CLI is `revl test <files> --mock-requires` (a document with no `lifecycle
test` is a no-op with a reason); a document's plain `test` blocks are not
part of mock world.
