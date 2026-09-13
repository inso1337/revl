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
// string.
//
// THE `data` CHANNEL. `add_entry`'s fourth argument is the typed reactive state
// the revl component publishes (`type ConsoleState` in console.rvl), and the RPC
// half is the component's declared provision (`service ConsoleRpc`). This adapter
// passes the state straight through to Cordis as the reactive object and attaches
// the provision's methods to it, which is what makes them RPC methods the browser
// may call (`@cordisjs/plugin-webui` base/index.ts:39). Both halves are PROJECTED
// into the browser's type by `revl export client --lang ts --face webui
// --component ConsoleUI`, so nothing about the channel is restated by hand here
// or in the frontend (item 457 slice S4, design note 530 Decision B).

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
 * console.rvl declares, and what a revl component reads as `ctx.webui`. `data` is
 * the typed reactive state the component publishes; its type on the revl side is
 * the record `add_entry`'s `data` parameter declares, and the same declaration is
 * what `revl export client --face webui` projects for the browser. */
export interface RevlWebUI {
  add_entry(
    devSource: string,
    prodManifest: string,
    routes: string[],
    data: Record<string, unknown>,
  ): string
}

/** How the host finds the RPC half of the channel: the composition's provision
 * key whose methods become the entry's browser-callable RPC surface. Optional —
 * an entry that publishes state only needs no key. */
export interface RevlWebuiOptions {
  /** The revl `provides` key to expose as RPC (e.g. `console`). */
  rpcKey?: string
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
export function installRevlWebui(
  ctx: Context,
  webui: CordisWebUI,
  options: RevlWebuiOptions = {},
): () => void {
  const service: RevlWebUI = {
    add_entry(
      devSource: string,
      prodManifest: string,
      routes: string[],
      data: Record<string, unknown>,
    ): string {
      const files: WebUIEntryFiles = { dev: devSource, prod: prodManifest, routes }
      // The reactive object Cordis broadcasts: the component's typed state, plus
      // the methods of its declared provision. Cordis turns each function on this
      // object into an RPC method (`base/index.ts:39`), so the browser-callable
      // set is exactly that provision — nothing this adapter invents.
      webui.addEntry(files, { ...data, ...rpcMethods(ctx, options.rpcKey) })
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

/**
 * The RPC half of the channel: every method of the composition's `rpcKey`
 * provision, bound to it. Returns nothing when no key is configured or the key is
 * not on the Context yet, so an entry that publishes state only stays state-only.
 *
 * This reads the provision the revl component declared with `provides` — the same
 * `ctx[key]` seam a revl `requires` resolves through — so the exposed set is the
 * declared one and this adapter adds no method of its own.
 */
function rpcMethods(ctx: Context, rpcKey?: string): Record<string, unknown> {
  if (!rpcKey) return {}
  const provision = (ctx as unknown as Record<string, unknown>)[rpcKey]
  if (!provision || typeof provision !== 'object') return {}
  const out: Record<string, unknown> = {}
  const source = provision as Record<string, unknown>
  for (const name of Object.keys(source)) {
    const value = source[name]
    if (typeof value === 'function') {
      out[name] = (...args: unknown[]) => (value as (...a: unknown[]) => unknown)
        .apply(source, args)
    }
  }
  return out
}
