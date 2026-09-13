# 525 exemplary web app — slice 4 (the TS frontend): wiring and filed gap

**Roadmap:** item 462 · **Issue:** #725 · **Slice:** 4 of N (the TS frontend
behind a typed boundary, item 459) · **Status:** FINDINGS, 2026-09-08 · **G3 CLOSED 2026-09-13** (the typed `data` channel is declared and projected)

Slice 1 built the boring CRUD half (`docs/design/525-webapp-slice1-gaps.md`) and
slice 3 the differentiated hot-swap half (`docs/design/525-webapp-slice3-differentiated.md`).
This slice adds 525's UI requirement — "a UI built from external assets + a TS
frontend behind a typed boundary (item 459)" — on the merged webui coeffect
(#772, design notes 526 and 530). The frontend is a real Vite/Vue project under
`examples/app/frontend/`; `tests/test_app_frontend_725.py` proves the boundary.

## The frontend wiring

- `examples/app/notes.rvl` grows the ambient `service WebUI` and a
  `component NotesConsole requires webui: WebUI, ranking: Ranker provides console: NotesConsoleRpc`
  that, at activation, emits
  `webui.add_entry("./frontend/entry.client.ts", "./frontend/dist/.vite/manifest.json", ["/notes"], { strategy: ranking.strategy(), signals: ranking.signals() })`.
  The binding is a first-class coeffect on reviewed surface only (`service` +
  `requires`), so `revl audit` reports the `webui` requirement (G1) and the
  `webui.add_entry` emission (the trusted host boundary, G8) on `NotesConsole`,
  with no extern door and no `globalThis` reach — exactly the property design
  note 530 Decision A shipped for the reference `examples/webui-entry/`.
- `examples/app/frontend/` is a normal Vite/Vue project (Vue 3 + Vite 7 +
  `@vitejs/plugin-vue`, the fixed Cordis WebUI tooling per 526): `entry.client.ts`
  is a `defineExtension` client extension registering the `/notes` page,
  `NotesConsole.vue` is the screen, `contract.ts` is the PROJECTED typed channel
  (generated, see below), and
  `notes.client.ts` is the typed REST client. `vite.config.ts` sets
  `build.sourcemap` so the shipped bundle points at these originals (459's
  "source maps pointing at the original files"), and `build.manifest` so it
  writes the `dist/.vite/manifest.json` the coeffect names as the production
  entry. No JavaScript lives in a revl string.
- The console consumes the notes app's TYPED ROUTES through `NotesApiClient`,
  which `revl export client --lang ts --service NotesApi` derives from the
  `NotesApi` declaration (item 457 artifact 5). `notes.client.ts` is that
  generated artifact checked in as an asset, so the `Note`/`NewNote` wire types
  and the `get_note`/`list_notes`/`create_note` call surface are one declaration
  with the server's router. Outcome is read from the typed `Result[T, ApiError]`
  union the client decodes, never from body prose (525 acceptance bar 3).

## 525 discipline: zero sentinels, zero emitter workarounds

The added revl source (`service WebUI` + `component NotesConsole`) needed no
routing sentinel (`""`/`"::empty::"`) and no emitter workaround
(`maybe_run`/`maybe_ship`); the whole-file scan in
`tests/test_app_notes_725.py::test_zero_sentinels_and_no_emitter_workarounds_in_the_app_source`
already covers the added lines, and `tests/test_app_frontend_725.py` asserts the
emitted TS carries zero inline HTML/CSS/JS blob.

## The reactive channel: filed as G3, now closed

Cordis WebUI's `addEntry(files, data)` publishes a reactive `data` object: the
server mutates it and the delta is broadcast to the browser, and each function on
it is an RPC method the browser can call (526). 459's typed-boundary requirement
(526 §"Where revl's typed boundary adds value") is that this surface be a typed
contract shared between the revl component and the TS client — the browser's
`useRpc<T>()` type and the server's published fields one declaration, the "one
definition" idea (457) applied to the console channel.

**G3 was that the landed coeffect could not express it:** `add_entry` had no
`data` parameter, so the entry published an empty reactive surface and
`examples/app/frontend/contract.ts` was authored by hand on the TS side rather
than PROJECTED. Design note 530 Decision B folded the projection into item 457 as
slice S4; that slice is now landed, and G3 is closed. The shape, each half
declared exactly once on reviewed language surface:

- **the reactive state** is the record type of `add_entry`'s new `data` parameter
  (`type NotesConsoleState` in `notes.rvl`). `NotesConsole` publishes a record
  literal built from the `ranking` service it requires, and the compiler checks
  that literal against the declared parameter type — so a field the browser
  expects that the server does not publish is a compile error in the revl source;
- **the RPC method set** is the component's declared provision
  (`provides console: NotesConsoleRpc`), which is 526's "an entry's exposed
  methods are exactly the component's declared provisions" and the same surface
  `revl audit` reports as G1. The console's provision delegates to `ranking`, so
  the page reaches the ranker only through the declared boundary.

`revl export client --lang ts --face webui --component NotesConsole` projects both
into `NotesConsoleState`, `NotesConsoleRpc` and `NotesConsoleChannel`.
`contract.ts` is now that generated artifact — like `notes.client.ts`, regenerated
rather than edited, and `tests/test_app_frontend_725.py` asserts the checked-in
file is byte-identical to the projection. `revl dev`'s `DevWebUI` adapter records
the published channel alongside the asset paths, and
`examples/webui-entry/webui_host.ts` shows the Cordis adaptation (state through as
the reactive object, the provision's methods attached to it).

What the channel still does not claim: a MUTATION path driven from revl. The
component publishes the initial record; later deltas are Cordis' own
`Entry.mutate` on the host side. The REST half remains the path for note state.

## Not gaps (recorded so a later slice does not refile them)

- **The composition GATE does not yet admit an ambient/host-provided `requires`
  without a revl `provides`.** `NotesConsole requires webui: WebUI` compiles and
  audits (the coeffect is an outward reach `audit` already reports), but a full
  composition that loads it against a real host `webui` provider needs the
  checker refinement design note 530 Decision A defers to item 186/462. This
  slice does not compose a live host (no cordis-py/Cordis runtime is required to
  prove the boundary), so it is not blocked; recorded as the same 462-gated item
  530 already names, not a new gap.
- **`host` rows for `ctx.server`/`ctx.webui`** (457 §"The Cordis `ctx.server`
  binding") are the composition-side surface that wires the app to a live Cordis
  runtime (457 slice S3). The frontend boundary this slice proves — external
  assets, coeffect binding, source maps, no inline blob — does not need them; the
  one-command dev runner (item 461) that composes the live host is the remaining
  462 slice.

## What the next slices add

- ~~The one-command dev runner (item 461) composing the live Cordis host.~~
  LANDED. `revl dev` provides the scoped `webui` and activates `NotesConsole`;
  slice 5 (`525-webapp-slice5-one-command.md`) runs it with Vite attached and
  records what building the project for the first time revealed, including a
  client-extension defect this slice's file-shape assertions could not see. The
  `host` rows (457 S3) remain the production-composition surface, and item 186 is
  about answering at admission time what is today a load-time PENDING note.
- ~~`revl export client --face webui` (457 S4), which closes G3 and makes
  `contract.ts` a projected artifact.~~ LANDED, see the section above.
- ~~The remaining 525 acceptance bars (1, 5: the one-command run and the
  competitiveness/friction report).~~ LANDED:
  `docs/webapp-competitiveness-report.md`.
