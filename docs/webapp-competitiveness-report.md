# Where revl is already competitive, and where friction remains

**Roadmap:** item 462 (acceptance bar 5) · **Issue:** #725 · **App:**
[`examples/app/notes.rvl`](../examples/app/notes.rvl) · **Status:** FINDINGS,
2026-09-13

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
(the TS frontend), [525-webapp-slice5-one-command.md](design/525-webapp-slice5-one-command.md)
(the one dev command, run with the frontend attached).

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
HTML/CSS/JS blob**. The old shape, ~4,800 lines of HTML/CSS/JS inside revl
strings, is what this replaces.

The source-map claim is proven by building, not by reading the config: the real
Vite build emits a bundle whose map names `entry.client.ts`, `NotesConsole.vue`
and `notes.client.ts`, plus the `.vite/manifest.json` keyed by the same
`entry.client.ts` path the coeffect declares, which is how a production host
resolves the dev source to the built asset. `vue-tsc` over the strict tsconfig
compiles the assets against the real `@cordisjs/client` types.

### 7. One command, both processes

`revl dev --once --port P examples/app/notes.rvl` boots Vite on `P`, loads the
composition, registers the console entry with its typed channel, and tears down
residue-free. Compile and admission run *before* Vite is spawned, so a source
error names its `.rvl` line and the failing stage with no port bound. The two
processes a contributor used to assemble by hand are one command, asserted with
the frontend attached rather than with `--no-frontend`.

## Part 2 — Where friction remains

Five gaps have been filed across the slices; G3 has since closed. None is a
sentinel or an emitter workaround, so none violates bar 2. Four are ergonomic,
expressiveness or process gaps a later item owns, and one (G1) has a correctness
edge.

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

### G3 (CLOSED 2026-09-13) — the webui coeffect carried no typed reactive-state / RPC channel *(item 457 slice S4)*

Cordis WebUI's `addEntry(files, data)` publishes a reactive `data` object: the
server mutates it, the delta is broadcast to the browser, and each function on it
is an RPC method. 459's typed-boundary requirement is that this be a contract
*shared* between the revl component and the TS client — the browser's
`useRpc<T>()` type and the server's published fields as one declaration.

**It now expresses it.** `add_entry` takes a fourth `data` parameter whose
declared record type is the reactive state, the RPC surface is the component's
declared provision, and
`revl export client --lang ts --face webui --component NotesConsole` projects both
into `NotesConsoleState`, `NotesConsoleRpc` and `NotesConsoleChannel`.
`examples/app/frontend/contract.ts` is that generated artifact, regenerated
rather than edited, with byte-identity asserted, so a field the browser expects
that the
server does not publish is a compile error in the revl source. `revl dev` prints
the channel the composition opened (`channel state signals, strategy`) next to
the asset paths.

What the channel still does not claim: a MUTATION path driven from revl. The
component publishes the initial record; later deltas are Cordis' own
`Entry.mutate` on the host side, and note state stays on the typed REST routes.

### G4 — the generated TS client does not compile under a strict tsconfig *(filed against item 457)*

`revl export client --lang ts --service NotesApi` emits a routed client whose
constructor always declares a fallback transport:

```ts
constructor(private readonly base: string, private readonly transport: Transport = ...) {}
```

When every operation is routed, which is the point of the `route` clause,
`transport` is never read, and `vue-tsc` over the app's `strict` + `noUnusedLocals` tsconfig
reports `notes.client.ts(66,63): error TS6138`. The app cannot fix it: the file is
a regenerated artifact whose byte-identity with the projection is asserted. The
generator should omit the parameter, or not declare it `private`, when no
operation is unrouted. **Severity: low, but it lands in the one artifact a user
is told not to edit**, which is the worst place for a diagnostic.

### G5 — no CI leg builds or typechecks the frontend *(filed against item 462 / 461)*

The build and typecheck legs skip unless a contributor has run `npm ci` in
`examples/app/frontend`; the required checks are the six backend matrices plus
lint, none of which install node deps for this example. That is how a frontend
that did not compile at all shipped and sat: until this pass, `entry.client.ts`
passed a `fields` option `@cordisjs/client`'s `ctx.page` does not accept, and ran
`useRpc` outside a component `setup` where its injection is absent. Both were
invisible to a file-shape scan and obvious to `vue-tsc`
(docs/design/525-webapp-slice5-one-command.md D1). **Severity: process, and the
highest-leverage item on this page**: a typed boundary is only typed once
something compiles it.

The two new legs are themselves the shape
`tests/test_env_gated_skips_run_somewhere.py` was written to warn about: a
toolchain probe whose skip is the same colour as a pass. That file's own docstring
names the residual it does not cover (a probe that does not go through a
`revl.test` tier runner), and these are in it. Closing G5 means a required job
that runs `npm ci && npm run typecheck && npm run build` for this example, not a
stronger assertion inside the suite.

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
- **An ambient/host-provided `requires` with no provider leaves the component
  PENDING rather than refused** (item 186 / design note 530 Decision A).
  `revl run examples/app/notes.rvl --once` with no ambient `webui` prints
  `note | NotesConsole | PENDING — unmet requirement` and still exits 0 with no
  residue; `revl dev`, which provides the scoped `webui`, activates it. So the app
  IS composed against a live host today and 186 is about answering at admission
  time what is currently a load-time note. **`host` rows for
  `ctx.server`/`ctx.webui`** (item 457 S3) are the production-composition surface
  and are likewise not what the dev command needs.

## Part 3 — Acceptance bar scorecard

| # | Bar | State | Evidence |
|---|---|---|---|
| 1 | Runs under **one** dev command; an induced error names the `.rvl` line and failing stage | ✅ | `revl dev` (item 461, #724 **closed**), proven **with Vite attached** in `test_app_notes_725.py::test_dev_runs_the_app_and_its_vite_frontend_under_one_command`; compile and admission run before Vite spawns, so a source error names its line and stage with no port bound |
| 2 | **Zero** routing sentinels, **zero** emitter workarounds | ✅ | `test_app_notes_725.py::test_zero_sentinels_and_no_emitter_workarounds` over every slice's added lines |
| 3 | Outcome from the typed HTTP contract, never from prose | ✅ | `Result[T, ApiError]` → status mapping; the TS client decodes the same union |
| 4 | Hot-swap demonstrates admission → cancellation → residue-free recovery | ✅ | all seven lifecycle rows asserted in `test_app_hotswap_725.py` |
| 5 | Ships with the competitiveness/friction report | ✅ | this document |

Every bar is met, so the milestone's exit is met. What would DEEPEN the app
rather than complete it: item 457 Slice 2 (`stdlib/auth.rvl`, a user-scoped
store), the persistence surface behind G1/G2 (item 465 / design 527), and a CI
leg for the frontend toolchain (G5).

## Part 4 — What the friction actually costs

Ranked by what a real user would feel, not by severity label:

1. **G1 is the only friction with a correctness edge.** Hand-rolled id minting is
   unsafe under concurrent creates, and it is exactly the kind of thing a
   "boring" framework should own. It is the strongest argument for the item-465 /
   527 Minato CRUD surface.
2. **G5 costs the most and is not a language gap at all.** Nothing in CI
   compiled the frontend, so the typed boundary this app exists to demonstrate
   shipped in a state where it could not have worked in a browser: a page option
   Cordis does not accept, and a composable called where its injection is absent.
   Both fell out of `vue-tsc` in one run. The lesson generalises past this app:
   the parts of a revl system that live outside `.rvl` need the same gate the
   `.rvl` half gets.
3. **Item 186 is a diagnostics gap, not a structural blocker.** The earlier
   consolidation read it as one; running the command disproved that. A missing
   ambient provider today is a PENDING fiber and an exit 0, which is a weaker
   answer than an admission-time refusal but does not stop the app composing
   against a live host.
4. **G4 is small and badly placed**: a diagnostic in the one file a user is told
   to regenerate rather than edit.
5. **G2 is the cheapest to close** and would remove N-round-trip listing.

The honest summary: **revl's typed-boundary claim held under a real app** on the
revl side of the boundary, and the friction that remains is concentrated in the
persistence tier (465/527), the generated-artifact polish (G4), and the toolchain
gating around the non-revl half (G5), not in routing, validation, serialization,
the client contract, or the swap/recovery guarantees, which is where the app spent
its budget. The one claim this report previously overstated was item 186's: it is
corrected above.

## Related

- [lifecycle-contract.md](lifecycle-contract.md) — the service lifecycle contract
  the hot-swap half is observed through
- [frontend-assets.md](frontend-assets.md) — the `webui` coeffect, `add_entry`,
  and the G1/G8 audit surface
- [commands-reference.md](commands-reference.md#revl-dev) — `revl dev`, the one
  development command
- [v2.0-roadmap.md](v2.0-roadmap.md) — items 456–462, 465, 527, 186
