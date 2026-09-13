# 525 exemplary web app — slice 5 (the one dev command, proven): findings

**Roadmap:** item 462 · **Issue:** #725 · **Slice:** 5 of N (acceptance bar 1,
run with the frontend attached) · **Status:** FINDINGS, 2026-09-13

Slices 1, 3 and 4 built the boring CRUD half, the hot-swap half and the TS
frontend. Each proved its own boundary without ever running the two halves
together: every `revl dev` test used `--no-frontend`, and the frontend's
"source maps pointing at the original files" was asserted by reading
`vite.config.ts` rather than by building. This slice runs it.

## What running it revealed

Building the project for the first time found two defects and one obsolete
workaround. All three were invisible to a file-shape scan, which is the point:
acceptance bar 1 is a claim about a command, not about a checked-in tree.

### D1 — the client extension passed an option Cordis' `ctx.page` does not accept

`entry.client.ts` registered the page as

```ts
ctx.page({ path: '/notes', name: 'Notes', component: NotesConsole,
           fields: () => ({ channel: useRpc<NotesConsoleChannel>() }) })
```

`Activity.Options` (`@cordisjs/client/client/plugins/router.ts:29`) has no
`fields`, so the channel would never have reached the screen's `channel` prop,
and `useRpc`, a composable that reads `inject(kContext)`, ran outside any
component `setup`, where the injection is absent and the call throws. Two
independent defects in four lines of the typed boundary the slice was about.

Fixed by registering a one-line page wrapper whose own `setup` is where the
composable runs:

```ts
const NotesPage = defineComponent({
  name: 'NotesPage',
  setup() {
    const channel = useRpc<NotesConsoleChannel>()
    return () => h(NotesConsole, { channel: channel.value })
  },
})
```

`NotesConsole.vue` stays a plain props-driven screen, so it still renders
standalone outside a Cordis host.

**Why it was not caught:** nothing typechecked the frontend. The suite asserted
that strings appeared in the assets; it never compiled them against the real
`@cordisjs/client` types. A typed boundary is only typed once something compiles
it, and `tests/test_app_frontend_725.py` now does
(`test_the_frontend_typechecks_against_the_real_cordis_client`).

### D2 — `npm install` needed `--legacy-peer-deps`, and the conflict was ours

`package.json` pinned `vite ^7` next to `@vitejs/plugin-vue ^5.2.0`, whose peer
range is `vite ^5 || ^6`. Documented as "the `npm install` trap" in
`docs/frontend-assets.md` and worked around with `--legacy-peer-deps`, including
in `revl dev`'s own boot diagnostic. It was not a Cordis constraint: bumping the
plugin to `^6` (peer `vite ^5 || ^6 || ^7 || ^8`) makes plain `npm install`
resolve, and `npm ci` now installs from a committed `package-lock.json`. The
frontend half of the one dev command is reproducible rather than
escape-hatched, which is item 461's toolchain-resolution clause.

### D3 — the build was never run, so the source-map claim was untested

It holds. `npm run build` emits `dist/notes-console.js` with a map whose
`sources` are `../entry.client.ts`, `../NotesConsole.vue` and
`../notes.client.ts`, and writes `dist/.vite/manifest.json` keyed by
`entry.client.ts`, the same path `NotesConsole` names through
`webui.add_entry`, which is how a production host resolves the dev source to the
built asset. Asserted by building, in
`test_the_frontend_really_builds_and_maps_to_the_originals`.

## Acceptance bar 1, with the frontend attached

`revl dev --once --port P examples/app/notes.rvl` boots Vite on `P`, loads the
composition, registers the console entry through the `webui` coeffect with the
typed channel, and tears down residue-free: one command, both processes.
Pinned by
`tests/test_app_notes_725.py::test_dev_runs_the_app_and_its_vite_frontend_under_one_command`
(previously only the `--no-frontend` path was covered).

## New gaps filed

### G4 — the generated TS client does not compile under a strict tsconfig *(item 457, `export_client`)*

`revl export client --lang ts --service NotesApi` emits

```ts
constructor(private readonly base: string, private readonly transport: Transport = ...) {}
```

unconditionally for a routed service. When every operation is routed, which is
the whole point of the `route` clause, `transport` is never read, and
`vue-tsc` over the app's `strict` + `noUnusedLocals` tsconfig reports
`notes.client.ts(66,63): error TS6138`. The artifact is regenerated, not edited
(`test_frontend_consumes_the_notes_typed_routes` asserts byte-identity), so the
app cannot fix it; the generator should omit the transport parameter, or not
declare it `private`, when no operation is unrouted. Filed against **item 457**
(`src/revl/export_client.py`, the routed-client constructor). Filtered by name in
the typecheck test, so closing it needs no test change.

### G5 — no CI leg builds or typechecks the frontend

The two toolchain legs skip unless a contributor has run `npm ci` in
`examples/app/frontend`; the required checks are the six backend matrices plus
lint, none of which install node deps for this example. So D1, a frontend that
did not compile at all, could land again. Closing it means a CI leg that runs
`npm ci && npm run typecheck && npm run build` for the example, which is a CI
decision rather than an app change. Filed against **item 462/461** and recorded
in `docs/webapp-competitiveness-report.md`.

## Not gaps

- **An ambient `requires` with no host provider leaves the component PENDING,
  not refused.** `revl run examples/app/notes.rvl --once` (no ambient `webui`)
  prints `note | NotesConsole | PENDING — unmet requirement` and still exits 0
  with no residue; `revl dev`, which provides the scoped `webui`, activates it.
  So the app IS composed against a live host today, and the item-186 composition
  gate refinement design note 530 Decision A defers is about turning that note
  into an admission-time answer, not about whether the app can run. Recorded
  here because the earlier consolidation read it as a structural blocker.
- **`dist/` is build output**, now gitignored; `package-lock.json` is committed
  next to it deliberately (D2).
