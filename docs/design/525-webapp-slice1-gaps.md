# 525 exemplary web app — slice 1 (boring half): filed gaps

**Roadmap:** item 462 · **Issue:** #725 · **Slice:** 1 of N (the boring CRUD half) · **Status:** FINDINGS, 2026-09-08

`examples/app/notes.rvl` is the first slice of the exemplary web application: a
typed notes CRUD service built the obvious way on the `route` clause (item 457)
over `stdlib/http.rvl` (item 456) with persistence via the model-store pattern
(item 465, `examples/model_store.rvl`). `tests/test_app_notes_725.py` proves it.

525 acceptance bar 2 requires ZERO routing sentinels (`""`/`"::empty::"`) and
ZERO emitter workarounds in the app source, and requires that every place one
would have been needed is filed here rather than absorbed. This note records
what building the slice revealed.

## The routing/HTTP primitives (456, 457) needed no workarounds

The whole point of the exercise: the routed CRUD half was expressible with no
sentinel and no emitter workaround.

- One `route` clause per endpoint derived the router, the per-parameter request
  validation and the `Result[T, ApiError]` status mapping. A bad `?limit=x` is a
  400 naming `limit` before the handler runs; an unmatched path is 404; the wrong
  method on a matched path is 405; the success/`ApiError` outcome is read from the
  return TYPE, never from body prose.
- `stdlib/http.rvl`'s typed `Body`/`Outcome`/`ApiError` meant the handler never
  reached for a `""` or a `"::empty::"` stand-in, and the "no route matched" case
  is the typed `NotHandled` the router owns, not an empty string.

No gap is filed against 456 or 457 for this slice. That is the result 525 wants.

## Gaps filed against the persistence surface (item 465, #752)

Two points of friction surfaced, both in the model-store surface, not in the
routing primitives. Neither is a routing sentinel or an emitter workaround, so
neither is a bar-2 violation; both are ergonomic gaps in the store's CRUD
vocabulary that a real persistence tier (the Minato/Cordis-Database alignment in
docs/design/527-database-persistence-alignment.md) would close.

### G1 — the store owns no primary-key assignment (id minting leaks into the handler)

`NoteStore.create` keys a row by `row.id`, and a `NewNote` from a client carries
no id (the server owns it). With no store verb that assigns a primary key, the
HTTP handler mints one itself:

```revl
let id  = "note-" + store.size().to_str()
let row = { id: id, title: note.title, body: note.body }
emit store.create(row)
```

This compiles and reverts residue-free, but the id logic belongs in the store,
not the handler: minting from `size()` is not safe under concurrent creates
(two creates can read the same size), and a real driver assigns the key itself
(Minato autoinc / `INSERT ... RETURNING`). The model-store surface should grow
an insert that assigns and returns the primary key (e.g. `emission fn create(row:
NewNote) -> Note`, the store filling `id`), so the exemplary app's `create_note`
never hand-rolls an identifier. Filed against **item 465** (persistence /
model store); the design hook is 527's Minato CRUD surface.

Routed around for this slice with the `size()`-based counter above, explicitly
because the slice's job is the routed half; the counter is called out here rather
than presented as the intended shape.

### G2 — the store exposes no `values()` / typed `list`, so listing folds keys-then-get

`GET /notes` returns every row. The host `Map` surface backing the memory driver
is `new/drop/insert/insert_if_absent/remove/get/size/keys` — it has `keys()` but
no `values()` — so `NoteStore.all` lists the VALUES with a keys-then-get fold:

```revl
fn all() {
  var out = []
  for (k of rows.keys()) { out = out.push(rows.get(k)) }
  return out
}
```

This is idiomatic and needs no sentinel, so the app absorbs it. But a typed
model-store surface (527) should expose the list/query directly — a driver-owned
`fn all() -> List[Note]` (or a filtered `query`) — so a consumer never reconstructs
a value list from keys, and a sqlite/postgres driver can answer it with one
statement rather than N `get`s. Filed against **item 465** as a surface gap; low
severity (the fold is correct, just not driver-independent at scale).

## Not gaps (recorded so a later slice does not refile them)

- **Authorization** (`Bearer`/`Principal`, the explicit `auth.validate` step) is
  item 457 Slice 2 and `stdlib/auth.rvl` is not landed yet, so these endpoints are
  public by construction (their handlers reach no principal-taking service). Not a
  gap; it is the documented slice boundary.
- **A component `provide` body admits only effect forms** (`let/effect/emit/fail/
  if/return`); a bare `match`/`for` STATEMENT is refused. This did not block the
  slice: `return match { ... }` (an expression) covers the handler dispatch, and
  `for` IS admitted for the `all()` fold. Recorded only so it is not mistaken for a
  limitation.
- **The host-verb frontier types arguments, not results**, so `rows.get(k)` is
  opaque and the `Opt`/`List` shaping follows the service return type. This is the
  documented host boundary (item 397 / 401), not a gap this app introduces.

## What the next slices add

- Slice 2: `stdlib/auth.rvl`, opaque `Principal`, the admission refusal for a
  dropped `auth.validate`, and a user-scoped store (item 457 Slice 2).
- Later: the differentiated hot-swap half (admission, cancellation, residue-free
  recovery — item 460) and the TS frontend behind a typed boundary (item 459).
- If G1/G2 land in item 465, `create_note`/`list_notes` simplify accordingly and
  this note's G1/G2 are closed.
