# Frontend assets: a real frontend behind a typed boundary

Roadmap item 459 · issue #722 · design of record
[design/459-frontend-asset-integration.md](design/459-frontend-asset-integration.md),
with [design/526-webui-asset-alignment.md](design/526-webui-asset-alignment.md)
(the Cordis `ctx.webui` study) and
[design/530-webui-entry-surface.md](design/530-webui-entry-surface.md) (the
coeffect decision).

> **Status: the asset model and the typed channel are landed; the remainder is
> named at the end of this page.** Read
> [What is not expressible yet](#what-is-not-expressible-yet) before you plan
> around it.

## The problem it replaces

The old way to ship a browser UI from revl was to carry it *inside revl source*:
HTML, CSS and JavaScript in string literals, with browser tooling extracting the
scripts back out at build time. That shape fails four ways at once, and all four
are consequences of the string boundary:

- **No source maps to an original.** The JS the browser runs has no mapping back
  to a line an author wrote, so a browser stack trace names a string literal.
- **No tooling.** A frontend inside a string cannot be type-checked, bundled,
  tree-shaken or hot-reloaded by the normal Vite/Vue pipeline.
- **No stated call surface.** Nothing declares which server methods the page is
  allowed to call; the boundary is whatever `fetch` the script happens to make.
- **No auditable boundary.** An emission buried in a string is not a
  `component`'s declared requirement, so `revl audit` cannot report it.

The fix is the same one the language uses everywhere else: **name the boundary
as a declared requirement, and hand it external asset paths.** No JavaScript
lives in a revl string.

## The shape

The host runtime provides WebUI as an **ambient service**. A component declares a
first-class coeffect on it and registers its frontend entry at activation,
naming *files only*:

```revl
// The reactive state this entry publishes to its page: an ordinary declared
// record. Its fields are what the browser observes on the synced channel.
type ConsoleState = { title: Str, ready: Bool }

// The RPC surface the browser may call: the component's own provision, so the set
// of server methods the page can reach is enumerable from the source.
service ConsoleRpc {
  fn ping(nonce: Str) -> Str
}

// The ambient WebUI service the host provides as `ctx.webui`. `add_entry` names
// external asset files and the typed reactive channel, and returns a stable entry
// handle; it is an `emission` because registering a frontend is an outward effect
// on the host (G4 upper bound — the shape a `revl import cordis` of
// `@cordisjs/plugin-webui` recovers).
service WebUI {
  emission fn add_entry(
    dev_source: Str,
    prod_manifest: Str,
    routes: List[Str],
    data: ConsoleState,
  ) -> Str
}

// A component contributes a frontend by declaring the coeffect and emitting the
// registration. The binding is the declared coeffect, so `revl audit` reports the
// `webui` requirement (G1), the `webui.add_entry` emission (G8, the trusted host
// boundary) and the `console` provision on this component's surface.
component ConsoleUI requires webui: WebUI provides console: ConsoleRpc {
  emit webui.add_entry(
    "./frontend/entry.client.ts",        // dev-mode source (a Vite/Vue module)
    "./frontend/dist/.vite/manifest.json", // built Vite manifest for production
    ["/console"],                        // client route patterns this entry owns
    { title: "revl console",             // the typed reactive channel
      ready: true },
  )

  provide console {
    fn ping(nonce) = "pong:" + nonce
  }
}
```

The four arguments are:

| Argument | What it is |
|---|---|
| `dev_source` | the frontend's **entry module** as Vite serves it in dev — a real `.ts` file, not a bundle |
| `prod_manifest` | the Vite **build manifest** (`build.manifest: true`), which maps entry names to hashed, emitted assets for production |
| `routes` | the client-side route patterns this entry owns, so the host can mount it |
| `data` | the **typed reactive state** the entry publishes — the object Cordis WebUI broadcasts to the browser, declared as a record instead of an untyped `T` |

## The typed channel: `revl export client --face webui`

Cordis WebUI's `addEntry(files, data)` publishes `data` as a reactive object: the
server mutates it and the delta is broadcast to the browser, and any function on
it becomes an RPC method the browser may call. Raw, that object is untyped. In
revl both halves are declared, once each, and **projected** into the TypeScript
the browser reads:

| Half | Declared as | Projected to |
|---|---|---|
| reactive state | the record type of `add_entry`'s `data` parameter | `readonly` fields of `<Component>State` |
| RPC methods | the services the component `provides` | `async` methods of `<Component>Rpc` |

```bash
revl export client --lang ts --face webui --component ConsoleUI \
  -o frontend/contract.ts console.rvl
```

```ts
// frontend/contract.ts (generated — do not edit)
export interface ConsoleUIState {
  readonly title: string;
  readonly ready: boolean;
}
export interface ConsoleUIRpc {
  ping(nonce: string): Promise<string>;
}
export type ConsoleUIChannel = ConsoleUIState & ConsoleUIRpc;
```

The client extension then reads the channel with the projected type, so the
server's published fields and the browser's `useRpc` type are **one
declaration**:

```ts
fields: () => ({ channel: useRpc<ConsoleUIChannel>() }),
```

Two properties follow from the projection, and neither is available when the
contract is hand-written on the TS side:

- **The published value is checked against the declared state.** The record the
  component `emit`s is type-checked against `data`'s declared type, so a field the
  browser expects that the server does not publish is a compile error in the revl
  source, not a runtime `undefined`.
- **The browser-callable set is exactly the component's provisions.** It is the
  same surface `revl audit` reports as G1, so "which server methods can this page
  call" is answerable from the declaration.

Regenerate `contract.ts` whenever the state record or the provided service
changes; the file carries a `DO NOT EDIT` header because a hand edit is a second,
unchecked declaration. The REST half of the frontend is the sibling projection,
`revl export client --lang ts --service NAME`.

`emit` (not a plain call) is required because registering a frontend is an
**outward effect**: it changes host state that outlives the call. That is the
same reason `add_entry` is declared `emission fn`.

## What `revl audit` reports

The coeffect exists so the contribution is *enumerable* rather than invisible:

- the `webui` requirement is reported as **G1** on `ConsoleUI`'s boundary;
- the `webui.add_entry` emission is reported as **G8**, the trusted host
  boundary.

So a reviewer can answer "which components register frontends, and at which
routes" from the audit output, without reading a single script. This is the
property the string-embedded frontend could never have.

**Why a coeffect and not a `globalThis` bridge.** An earlier slice (#761)
reached Cordis' `Context` through a retained embedder bridge,
`globalThis.__revlWebui` — a `@ts ref` door onto an untyped seam. Design note 530
flagged that as a slice compromise and asked for the first-class surface; the
coeffect was chosen. So `webui` is now a declared `requires` key, the emitted
artifact resolves it through Cordis' own `inject` (the host-provided
`ctx.webui`), and there is no `globalThis` reach and no untyped door.

## The Vite/Vue project

The frontend is an ordinary Vite project — nothing about it is revl-specific
except the two paths above. The pieces that matter:

```ts
// vite.config.ts
export default defineConfig({
  build: {
    manifest: true,   // -> the `prod_manifest` path
    sourcemap: true,  // -> maps the bundle back to your own sources
    lib: {
      entry: './entry.client.ts',   // -> the `dev_source` path
      formats: ['es'],
      fileName: 'notes-console',
    },
  },
  rollupOptions: { external: ['vue', '@cordisjs/client'] },
})
```

`external: ['vue', '@cordisjs/client']` is deliberate: Cordis WebUI provides Vue
at runtime, so the frontend must not bundle a second copy.

**Installing it.** The dependency set resolves without a peer-deps escape
hatch, and `package-lock.json` is committed, so a contributor gets the exact tree
the app was built against:

```bash
npm ci          # from the committed lockfile
npm run typecheck   # vue-tsc over the strict tsconfig
npm run build       # the source-mapped bundle + .vite/manifest.json
```

Keep `@vitejs/plugin-vue` on a major that peers the pinned `vite`: `plugin-vue@5`
peers `vite ^5 || ^6`, so pairing it with `vite ^7` is the `ERESOLVE` this project
used to require `--legacy-peer-deps` for.

See [`examples/app/frontend/`](../examples/app/frontend/) for a working project
(`NotesConsole.vue`, `contract.ts`, `entry.client.ts`, `notes.client.ts`,
`vite.config.ts`, `package.json`, `tsconfig.json`), and
[`examples/webui-entry/console.rvl`](../examples/webui-entry/console.rvl) for the
canonical copy-me shape. Copy it the way `router.rvl` is copied: rename the
component, point the asset paths at your own project, and declare your own state
record and provision.

## Running it: `revl dev`

`revl dev` (roadmap item 724) runs the app and its frontend under one parent
process — Vite serves the frontend, the Cordis driver boots the `.rvl`
composition. `revl run` is the same lifecycle without the frontend. Full flags
are in [commands-reference.md](commands-reference.md#revl-dev); the parts that
matter for the asset model:

- `--frontend DIR` — the Vite directory (default `frontend/` alongside the app
  source). It must exist and contain a `package.json`.
- the app source is compiled and admitted **before** Vite is spawned, so a
  source error reports with its `compile` or `admission` line and no port is
  bound;
- Vite runs with `--strictPort`, so a port already in use exits **3** naming
  that port instead of quietly moving behind a banner that advertises the
  requested one. The same exit 3 covers a frontend whose dependencies are not
  installed;
- `--no-frontend` boots only the app host — useful for diagnosing lifecycle
  failures with Vite out of the way.

The development adapter is a real scoped Cordis provision, not a process-global:
it records the entry the composition registered, **rejects an inline substitute
or a path that escapes the app root**, and is withdrawn during normal LIFO
teardown. That refusal is the enforcement behind "external asset paths only".

## What is not expressible yet

This page documents stage 1. The item's design note names the remainder, and
none of it should be assumed:

- **`dev_source` and `prod_manifest` are bare `Str`, not typed handles.** There
  is no resolution, no jail, and no content pinning through
  [`hostref`](../src/revl/hostref.py) yet, so the compiler cannot check that the
  path exists, stays inside the app root, or refers to the same bytes between
  builds. Design note 459 calls this **F1**, "the next slice of this item".
- **No source map from the *insertion site* to the original asset.** Vite's
  `build.sourcemap: true` maps the bundle to *your* sources, which is a
  different claim from mapping the place revl names the asset back to a line.
  Design note 459 **F2**.
- **No template control flow** (`{{if}}`/`{{for}}`/includes/layouts, **F3**),
  and **tiers** are not extended beyond py/ts (**F6**).

The typed reactive-state / RPC channel (design note 459 **F5**, filed as gap
**G3** in
[design/525-webapp-slice4-frontend.md](design/525-webapp-slice4-frontend.md)) and
the `--face webui` verb (**F7**) are the section above; both are landed.

The item's stated exit is app-gated on roadmap item 462 (the exemplary web
application, issue #725, itself gated on item 461 / issue #724), so it cannot
close before that. See [v2.0-roadmap.md](v2.0-roadmap.md) items 459 and 462.

## Related

- [composition-rows.md](composition-rows.md) — how a component's declared rows
  and its header claim are checked against each other
- [import-cordis.md](import-cordis.md) — recovering an ambient service like
  `WebUI` from a Cordis plugin
- [interchange-format.md](interchange-format.md) — the manifest and the G8 audit
  format the boundary above is reported in
