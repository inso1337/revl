# Where revl is already competitive, and where friction remains

**Roadmap:** item 462 (acceptance bar 5) · **Issue:** #725 · **App:**
[`examples/app/notes.rvl`](../examples/app/notes.rvl) · **Status:** FINDINGS,
2026-09-12

Item 462's exit criterion has two clauses: the exemplary app "runs under one dev
command (461) with zero sentinels/workarounds, and ships with a written *where
revl is already competitive vs. where friction remains* report." This is that
report — acceptance bar 5.

It is a consolidation, not new research. Every claim below is grounded in the
slice notes that produced the app, and every friction point is a **filed gap**
rather than a workaround the app absorbed. That distinction is the deliverable:
525's non-goals say "if a section needs a workaround, that workaround is a bug
filed against 456–461, not something the app absorbs."

Sources: [525-exemplary-web-app.md](design/525-exemplary-web-app.md) (the
design), [525-webapp-slice1-gaps.md](design/525-webapp-slice1-gaps.md) (the
boring half), [525-webapp-slice3-differentiated.md](design/525-webapp-slice3-differentiated.md)
(the hot-swap half), [525-webapp-slice4-frontend.md](design/525-webapp-slice4-frontend.md)
(the TS frontend).

## Part 1 — Where revl is already competitive

The headline result is negative in the way that matters: **the boring half of
web development was boring.** No routing sentinel (`""` / `"::empty::"`) and no
emitter workaround (`maybe_run` / `maybe_ship`) was needed anywhere in the app
source — not in the routed CRUD half, not in the hot-swap half, not in the
frontend wiring. The whole-file scan in
`tests/test_app_notes_725.py::test_zero_sentinels_and_no_emitter_workarounds_in_the_app_source`
enforces it over the added lines of every slice.

### 1. One declaration derives the whole HTTP surface

A single `route` clause per endpoint (item 457) derives **four** things that are
normally four separate pieces of hand-written code:

| Derived from the declaration | Normally hand-written |
|---|---|
| the router | route table / framework config |
| per-parameter request validation | a validation layer |
| the `Result[T, ApiError]` → status-code mapping | error-handling middleware |
| OpenAPI + the typed TS client | a spec and a client |

The behaviour is what a typed boundary buys: a bad `?limit=x` is a **400 naming
`limit` before the handler runs**; an unmatched path is 404; the wrong method on
a matched path is 405. No route handler inspects a request to decide what to
validate, and nothing infers outcome from prose.

### 2. Outcome comes from the return type, never from body prose

This is 525 acceptance bar 3, and it is the sharpest contrast with the app this
replaces. The legacy console reconstructed status by *parsing its own output*.
Here the success/`ApiError` outcome is read from the declared return TYPE, and
the browser client decodes the same `Result[T, ApiError]` union. There is no
place in the app where a string is interpreted to learn whether a call
succeeded.

### 3. One declaration is both the server router and the browser's call surface

`revl export client --lang ts --service NotesApi` derives `notes.client.ts` from
the same `NotesApi` declaration the server routes on. The `Note`/`NewNote` wire
types and the `get_note`/`list_notes`/`create_note` call surface cannot drift
from the server, because they are not written twice. This is item 457's "one
definition" idea applied to a real client.

### 4. The differentiator: a hot-swap that keeps its state, and a refusal that keeps serving

The complexity budget is spent on exactly one guarantee, end to end. A
`Ranker` component scores notes from recorded engagement signals; the scoring
strategy is the swap axis:

- a `recency` build (each signal weighs 1) is proposed, admitted, and swapped for
  a `weighted` build (each weighs 3) **without losing the recorded signals** —
  the state crosses the swap because `TrendingRanker` declares
  `handoff ranking: Map[Str, Str]` (item 53), and `strategy()`/`score()` prove
  the new code is live;
- a successor whose declared `handoff` shape cannot hold the running one, or
  whose activation is not healthy, is **refused with the running build left
  serving** — a documented refusal with retained ownership, never a silent state
  drop.

That second bullet is the part that has no ordinary equivalent. A failed
deployment that leaves the previous version serving *and* provably holds no
orphaned state is not something a typical web stack states, let alone checks.

### 5. The lifecycle is observable from the contract's own surface

Admission, cancellation and recovery are read off the runtime, not asserted in
prose (item 460, [lifecycle-contract.md](lifecycle-contract.md)):

| Contract state | Observed through |
|---|---|
| serving | `Session.state()` — fiber `ACTIVE`, `providedKeys`, calls answered |
| admitted swap | `Session.swap` return — handoff report `migrated: True` |
| refused swap, retained ownership | admission `RevlError` + swap `SessionError`, gen N still serving |
| cancel-requested vs completed | `aclose()` + `teardown_disposition()` |
| owned-after-failure | `aclose()` + `strand_teardown()`, resource named owed |
| recovery-resume | `recover(wal)` — `activation-complete` present → rolled forward |
| residue-free roll-back | `recover(wal)` — `activation-complete` absent → `residue.clean` |

Every row is asserted on the real cordis-py runtime in
`tests/test_app_hotswap_725.py`.

### 6. The frontend boundary is auditable, and carries no JS in a revl string

The UI is a real Vite/Vue project behind a declared coeffect. `revl audit`
reports the `webui` requirement (**G1**) and the `webui.add_entry` emission
(**G8**, the trusted host boundary) on `NotesConsole`'s surface, with no extern
door and no `globalThis` reach.
`tests/test_app_frontend_725.py` asserts the emitted TS carries **zero inline
HTML/CSS/JS blob**, and `build.sourcemap` points the shipped bundle at the real
originals. The old shape — ~4,800 lines of HTML/CSS/JS inside revl strings — is
what this replaces.

## Part 2 — Where friction remains

Three gaps were filed. None is a sentinel or an emitter workaround, so none
violates bar 2; all three are ergonomic or expressiveness gaps that a later item
owns.

### G1 — the store owns no primary-key assignment *(filed against item 465, #752)*

`NoteStore.create` keys a row by `row.id`, but a `NewNote` from a client carries
no id — the server owns it. With no store verb that assigns a primary key, the
**HTTP handler mints the id itself**:

```revl fragment
let id  = "note-" + store.size().to_str()
let row = { id: id, title: note.title, body: note.body }
emit store.create(row)
```

This compiles and reverts residue-free, but the id logic belongs in the store:
minting from `size()` is **not safe under concurrent creates** (two creates can
read the same size), and a real driver assigns the key itself (Minato autoinc /
`INSERT ... RETURNING`). The store surface should grow
`emission fn create(row: NewNote) -> Note` with the store filling `id`. The
app's `create_note` should never hand-roll an identifier. **Severity: correctness
under concurrency, not merely ergonomics.**

### G2 — the store exposes no `values()` / typed `list` *(filed against item 465)*

`GET /notes` returns every row. The host `Map` surface backing the memory driver
has `keys()` but no `values()`, so `NoteStore.all` folds keys-then-get:

```revl fragment
fn all() {
  var out = []
  for (k of rows.keys()) { out = out.push(rows.get(k)) }
  return out
}
```

Idiomatic and sentinel-free, so the app absorbs it — but a typed model-store
surface should expose the list directly (`fn all() -> List[Note]`), so a
sqlite/postgres driver can answer it with **one statement instead of N `get`s**.
**Severity: low** — correct, just not driver-independent at scale.

### G3 — the webui coeffect carries no typed reactive-state / RPC channel *(filed against item 459 / 457)*

Cordis WebUI's `addEntry(files, data)` publishes a reactive `data` object: the
server mutates it, the delta is broadcast to the browser, and each function on it
is an RPC method. 459's typed-boundary requirement is that this be a contract
*shared* between the revl component and the TS client — the browser's
`useRpc<T>()` type and the server's published fields as one declaration.

The landed coeffect cannot express it: `add_entry(dev_source, prod_manifest,
routes) -> Str` has no `data` parameter, and the host publishes an **empty**
reactive surface deliberately (design note 530 Decision B). So
`examples/app/frontend/contract.ts` is **hand-authored TS** rather than projected
from the service declaration.

This is explicitly *not* a workaround absorbed by the app: the console drives
**all** note state through the typed REST routes, which need no `data` channel,
and `contract.ts` is written against the shape the projection will emit so no
client rewrite is needed when it lands. The closing slice is
`revl export client --lang ts --face webui` (457 S4), after which `contract.ts`
becomes a generated artifact like `notes.client.ts`.

### Friction that is real but was deliberately *not* filed

Recorded in the slice notes so a later pass does not refile them as bugs:

- **A component `provide` body admits only effect forms** (`let`/`effect`/`emit`/
  `fail`/`if`/`return`); a bare `match`/`for` *statement* is refused. This did not
  block the app — `return match { … }` (an expression) covers dispatch and `for`
  *is* admitted for the `all()` fold.
- **The host-verb frontier types arguments, not results** (items 397/401), so a
  raw host `Map`'s `size()` is opaque and `size().to_str()` is refused (G8).
  Routed idiomatically through a template literal for the row key.
- **Authorization is item 457 Slice 2** (`stdlib/auth.rvl`, opaque `Principal`)
  and is not landed, so the endpoints are public *by construction* — the
  documented slice boundary, not an omission.
- **A `lifecycle test` cannot drive a swap**, because swap is a `Session`/MCP
  verb. The in-`.rvl` test proves serving + residue-free teardown; the hot-swap
  legs live in the Python conformance test. That is the documented boundary
  between the two harnesses.
- **The composition gate does not yet admit an ambient/host-provided `requires`
  without a revl `provides`** (item 186 / design note 530 Decision A), and
  **`host` rows for `ctx.server`/`ctx.webui`** are item 457 S3. The slice does not
  compose a live host, so neither blocked it.

## Part 3 — Acceptance bar scorecard

| # | Bar | State | Evidence |
|---|---|---|---|
| 1 | Runs under **one** dev command; an induced error names the `.rvl` line and failing stage | ✅ | `revl dev` (item 461, #724 **closed**); the app source is compiled and admitted before Vite spawns |
| 2 | **Zero** routing sentinels, **zero** emitter workarounds | ✅ | `test_app_notes_725.py::test_zero_sentinels_and_no_emitter_workarounds` over every slice's added lines |
| 3 | Outcome from the typed HTTP contract, never from prose | ✅ | `Result[T, ApiError]` → status mapping; the TS client decodes the same union |
| 4 | Hot-swap demonstrates admission → cancellation → residue-free recovery | ✅ | all seven lifecycle rows asserted in `test_app_hotswap_725.py` |
| 5 | Ships with the competitiveness/friction report | ✅ | this document |

The two slices that would deepen the app rather than complete it are item 457
Slice 2 (`stdlib/auth.rvl`, user-scoped store) and 457 S4 (the webui face, which
closes G3).

## Part 4 — What the friction actually costs

Ranked by what a real user would feel, not by severity label:

1. **G1 is the only friction with a correctness edge.** Hand-rolled id minting is
   unsafe under concurrent creates, and it is exactly the kind of thing a
   "boring" framework should own. It is the strongest argument for the item-465 /
   527 Minato CRUD surface.
2. **Item 186 is the one structural blocker.** Until the composition gate admits
   an ambient `requires` without a revl `provides`, a revl app cannot be
   *composed against a live host* the way this app's frontend boundary implies.
   Everything else on this page is an ergonomic or expressiveness gap.
3. **G3 is the largest expressiveness gap**, but it is contained: the REST half of
   the boundary is real and typed today, and the app is written so the reactive
   half drops in without a client rewrite.
4. **G2 is the cheapest to close** and would remove N-round-trip listing.

The honest summary: **revl's typed-boundary claim held under a real app**, and
the friction that remains is concentrated in the persistence tier (465/527) and
the ambient-composition checker (186) — not in routing, validation,
serialization, the client contract, or the swap/recovery guarantees, which is
where the app spent its budget.

## Related

- [lifecycle-contract.md](lifecycle-contract.md) — the service lifecycle contract
  the hot-swap half is observed through
- [frontend-assets.md](frontend-assets.md) — the `webui` coeffect, `add_entry`,
  and the G1/G8 audit surface
- [v2.0-roadmap.md](v2.0-roadmap.md) — items 456–462, 465, 527, 186
