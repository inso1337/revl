# 526 — aligning revl frontend assets with Cordis WebUI

**Roadmap:** item 459 (issue #722), part of the 462 web-platform arc · **Source:** cordiverse/webui study, 2026-09-08 · **Status:** DESIGN DRAFT

## Purpose

Item 459 wants first-class frontend asset integration: external asset files, reusable templates, source maps, and normal JS/TS tooling, with revl sitting behind a clean typed boundary next to an existing frontend stack rather than replacing it. Cordis already ships that stack as `@cordisjs/plugin-webui`. This note records what it provides so revl binds to it instead of inventing a parallel console framework. The goal for 462 is that the console page stops being HTML/CSS/JS carried inside revl strings and becomes a normal Vite/Vue frontend that a revl component feeds through a typed contract.

## What Cordis WebUI provides

The service is `ctx.webui`, a Cordis `Service` named `webui`.

- `packages/webui/src/base/index.ts:25` declares `abstract class WebUI extends Service` with `static name = 'webui'` and augments `interface Context { webui: WebUI }` (line 13). So a plugin reaches it as `ctx.webui`.
- A plugin contributes UI by calling `ctx.webui.addEntry(files, data)` (`base/index.ts:69`). `files` (`base/entry.ts:9`) points at a dev-mode `source` (`.ts`) and a prod-mode Vite `manifest.json`, plus a `routes: string[]` list of client-side route patterns the entry registers. There is no HTML-in-string path; the entry is a real bundled module.
- The second argument `data` is a reactive object. `Entry.mutate(fn)` (`base/entry.ts:166`) diffs the object and broadcasts a delta over WebSocket (`entry:delta`); any function on `data` becomes an RPC method the browser can call (`base/index.ts:39`, the `rpc:request` listener that does `Reflect.apply(fn, entry, args)`). This is the server-to-client boundary: the server exposes typed reactive state plus named methods; the client never reaches into server internals.
- The client is itself a Cordis `Context` running in the browser (`packages/client/client/context.ts`). A frontend extension is `defineExtension((ctx) => { ... })` (`client/plugins/loader.ts:15`); it registers pages and slots through `ctx.client.router.page(...)` / `.slot(...)` (`client/plugins/router.ts:319`, `:306`) and reads the synced server state through `useRpc<T>()` (`context.ts:30`).
- Tooling is fixed and conventional: Vue 3 + Vite (`packages/client/package.json` depends on `vue@^3.5`, `vite@^7`, `@vitejs/plugin-vue`). The prod manifest the server loads is a `vite.Manifest` (`base/entry.ts:37`).

## What revl reuses vs. adds

Reuse, do not reinvent:

- Bind a revl service to `ctx.webui` and express page contribution as `addEntry`. The `files`/`routes` shape and the Vite manifest are the integration surface; revl points at asset files, it does not embed them.
- Keep Vue/Vite and the `defineExtension` client model as is. revl stays on the server side of the WebSocket boundary. The developer explicitly wants to retain JS/TS tooling (525, open questions), and this is where it lives.

Where revl's typed boundary adds value over the raw JS API:

- `addEntry`'s `data` is untyped `T` on the wire. revl can make the reactive-state object and its RPC method set a **typed contract** shared between the revl component and the TS client, so the browser's `useRpc<T>()` type and the server's published fields are one declaration (this is the 457 "one definition" idea applied to the console channel, not just REST).
- revl's confinement (G1/G8) makes the entry's exposed method set an enumerable boundary: the methods a console entry publishes over RPC are exactly the component's declared provisions, so an entry cannot quietly expose an undeclared server capability to the browser. That is the property the 4,800-line string console cannot state about itself.

## Exit alignment (feeds 459 / #722)

462's UI is a real Vite/Vue frontend registered through `ctx.webui.addEntry` with external asset files and source maps, and the server half is a revl component whose published reactive state and RPC methods are a typed contract, with zero JS extracted from revl strings.
