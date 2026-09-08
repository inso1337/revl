// Host-side setup for the webui-entry reference (roadmap item 459, issue #722).
//
// This is the one small piece of TypeScript that binds a revl component to the
// Cordis WebUI service (`@cordisjs/plugin-webui`, studied in
// docs/design/526-webui-asset-alignment.md). Its job is to INSTALL the ambient
// `webui` service onto the Cordis `Context` so a revl component that declares
// `requires webui: WebUI` reaches it — through Cordis' own `inject`, the typed
// coeffect boundary — rather than through a `globalThis` bridge.
//
// WHAT CHANGED FROM THE ENABLING SLICE. The first slice (#761) had the revl
// program reach this module through a `@ts ref` host-ref door and read the
// Cordis `Context` back out of `globalThis.__revlWebui`, the retained-embedder
// seam `backends/typescript/revl_fs_ts.ts` documents for `globalThis.__revlFs`.
// Design note 530 flagged that as a slice compromise: `webui` is an ambient
// service the host provides, and reaching it through an untyped global is not a
// first-class boundary. The architect approved the coeffect (option 1), so the
// binding now lives where an ambient service belongs — the host `provide`s the
// `webui` key, and the revl artifact names it as a `requires` coeffect. This
// module is EMBEDDER SETUP the host runs once; the emitted revl artifact does
// not import it and contains no `globalThis` reach and no host-ref door.
//
// WHAT IT DOES. Register a `webui` service on the Context whose surface matches
// the revl `service WebUI` contract (`add_entry(dev, prod, routes) -> handle`),
// adapting each call to Cordis WebUI's real `addEntry(files, data)` (per
// `@cordisjs/plugin-webui` base/entry.ts:9 an entry is `{ dev, prod, routes }`
// where `dev`/`prod` point at a dev-mode source and a built Vite manifest and
// `routes` lists the client route patterns). All three are PATHS to external
// asset files a normal Vite/Vue project produces, so browser tooling and source
// maps point at the originals rather than at scripts extracted from a revl
// string. The `data` reactive-state / RPC object is published empty here; the
// typed contract for it is design note 530, folded into item 457.

import type { Context } from 'cordis'

/** The Cordis WebUI entry shape (`@cordisjs/plugin-webui` base/entry.ts). */
export interface WebUIEntryFiles {
  dev: string
  prod: string
  routes: string[]
}

/** The slice of Cordis WebUI this reference drives. Typed loosely (a structural
 * subset of `@cordisjs/plugin-webui`'s service) so the example stands alone
 * without the plugin devDependency installed. */
export interface CordisWebUI {
  addEntry(files: WebUIEntryFiles, data: unknown): unknown
}

/** The revl-facing `WebUI` service surface — the exact shape `service WebUI` in
 * console.rvl declares, and what a revl component reads as `ctx.webui`. */
export interface RevlWebUI {
  add_entry(devSource: string, prodManifest: string, routes: string[]): string
}

/**
 * Install the ambient `webui` service onto `ctx`, adapting the revl `WebUI`
 * contract to Cordis WebUI's `addEntry`. Call this once during host setup,
 * before loading the revl composition; a revl component's `requires webui:
 * WebUI` coeffect then resolves to this service through Cordis' `inject`.
 *
 * `webui` is the real Cordis WebUI service (`ctx.webui` once
 * `@cordisjs/plugin-webui` is loaded). Kept an explicit argument — rather than
 * read back off `ctx` — so the reference has no hidden global and stands alone.
 * Returns the Cordis disposer `ctx.provide` hands back, so the host can reclaim
 * the service on teardown.
 */
export function installRevlWebui(ctx: Context, webui: CordisWebUI): () => void {
  const service: RevlWebUI = {
    add_entry(devSource: string, prodManifest: string, routes: string[]): string {
      const files: WebUIEntryFiles = { dev: devSource, prod: prodManifest, routes }
      // `data` is the reactive-state / RPC object. This slice publishes an empty
      // surface deliberately; the typed contract is design note 530 (item 457).
      webui.addEntry(files, {})
      // Return the dev source as a stable entry handle a revl caller can bind.
      return devSource
    },
  }
  // Ambient service registration: the key `webui` becomes `ctx.webui` for every
  // fiber that injects it. This is the same `ctx.provide(key, impl)` seam a revl
  // `provide` step lowers to — the binding is a declared boundary, not a global.
  return (ctx as unknown as {
    provide(key: string, impl: unknown): () => void
  }).provide('webui', service)
}
