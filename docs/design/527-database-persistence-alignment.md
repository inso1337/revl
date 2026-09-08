# 527 — aligning revl persistence with Cordis Database (Minato)

**Roadmap:** the persistence tier of item 462 (proposes a new item, see below); milestone issue #725 · **Source:** cordiverse/database study, 2026-09-08 · **Status:** DESIGN DRAFT

## Purpose

525 leaves the persistence tier open ("a witnessed file store vs. an embedded DB behind an effect"). Cordis already provides a typed, driver-independent ORM (`@cordisjs/plugin-database`, "Minato"). This note records its surface so the exemplary app's store is a bound Cordis service rather than an ad-hoc file store, and so persistence gets its own roadmap item with an app-shaped exit test.

## What Cordis Database provides

Two aliases for one `Service`: `ctx.model` and `ctx.database` (`packages/core/src/index.ts:18`, both typed `Database`; `packages/core/src/database.ts:89` is `class Database extends Service`).

- Tables are declared with `ctx.model.extend(name, fields, config)` (`database.ts:142`). `fields` maps column names to field defs (`string(255)`, `unsigned(8)`, `timestamp`, `{ type, length, nullable }`, relations); `config` carries `primary`, `autoInc`, `unique`, and `foreign` keys. The SSO plugin is a worked example: `sso.user` / `sso.identity` / `sso.session` are declared this way (`cordis-sso/packages/core/src/index.ts:348-378`).
- CRUD/query is a JavaScript API, not SQL strings: `get`, `set`, `create`, `upsert`, `remove`, `select`, `eval`, `join` (`database.ts:464,482,538,556,533,320,478,426`). Queries are typed objects over the declared model; there is no `query(sql: Str)`.
- Transactions: `withTransaction` / `transact(callback)` (`database.ts:594,598`) run a batch atomically; SSO uses `ctx.database.transact(...)` to create a user, identity, and session together.
- Drivers are separate packages behind one API: `memory`, `sqlite`, `mongo`, `mysql`, `postgres` (repo `readme.md` table, `packages/*`). The app picks a driver at composition time without touching model or query code.

The design pillar this satisfies is DESIGN.md pillar 5: "Don't build a runtime. revl compiles to an existing, hardened Cordis runtime." DESIGN.md §3.1 already sketches `service Database { fn query(sql: Str) ... }`, but that raw-SQL sketch predates Minato and should not be the exemplary app's persistence surface.

## What revl reuses vs. adds

Reuse:

- Bind revl's persistence `service` to `ctx.model`/`ctx.database` and its typed model API. Table declarations map to a revl typed-model boundary (see the existing typed-model work in docs/design/257-typed-model-boundary.md); query/mutation are the `get/set/create/upsert/remove/select` verbs, not string SQL. Drivers stay a composition-time choice.

Where revl's static gate adds value over the raw ORM:

- **G4 (every mutation carries an inverse).** Minato's writes (`create`/`remove`, `set`/unset, `upsert`) are natural inverse pairs. revl can require each persistence effect to declare its `undo`, and lower a component's accumulated writes onto `database.transact` so activation-time inserts are reverted in LIFO order on teardown or divert. That is the residue-free-recovery story 462 wants, expressed over a real store rather than a file.
- **G1/G8 (declared access, enumerable surface).** A component `requires db: Database` and declares which tables/fields it reaches; "undeclared access is a type error" then means a component cannot touch a table it did not name, and the set of tables a component can reach is statically enumerable. Minato itself has no compile-time reach bound; revl supplies it.
- **Typed queries at the language layer.** Minato is well-typed in TypeScript; revl carries that typing across its own front-end so a query against a field the model does not declare is a revl type error before emission.

## Proposed roadmap item

Persistence has no dedicated item today (462 only lists it as an open question). Propose adding it adjacent to 462, exit test shaped by the app: the exemplary app persists through a revl service bound to `ctx.model`, declaring its tables via the typed-model boundary, with every persistence mutation carrying a derived inverse that reverts within `database.transact` on teardown/divert, and a driver swap (memory to sqlite) requiring no change to model or query code. Exact proposed text is in the PR report for the architect to place.
