// The host shim for the webui-entry reference (roadmap item 459, issue #722).
//
// This is the one small piece of TypeScript that binds a revl component to the
// Cordis WebUI service (`@cordisjs/plugin-webui`, studied in
// docs/design/526-webui-asset-alignment.md). It exists as a REAL FILE, imported
// by the revl program through the item-396/410 host-ref door
// (`= @ts ref webuiAddEntry from "./webui_host.ts"`), exactly the way
// stdlib/fs.rvl reaches backends/typescript/revl_fs_ts.ts. That is the whole
// point of 459: the frontend binding is a normal, type-checked, source-mapped
// module, NOT a JavaScript string carried inside revl source.
//
// What it does: hand Cordis a WebUI ENTRY. Per Cordis (`@cordisjs/plugin-webui`
// base/entry.ts:9) an entry is `{ dev, prod, routes }` where `dev` points at a
// dev-mode `.ts`/`.vue` source, `prod` at a built Vite `manifest.json`, and
// `routes` lists the client-side route patterns. All three are PATHS to external
// asset files that a normal Vite/Vue project produces; the entry is a real
// bundled module, so browser tooling and source maps point at the originals
// rather than at scripts extracted back out of a revl string.
//
// Reaching `ctx`: this shim reads the Cordis `Context` from the embedder bridge
// `globalThis.__revlWebui`, the same retained-embedder-seam convention
// backends/typescript/revl_fs_ts.ts documents for `globalThis.__revlFs`. Making
// the ambient `ctx.webui` binding FIRST-CLASS (a declared coeffect a revl
// component `requires`, so the entry's published RPC surface is an enumerable,
// typed contract rather than an untyped `data: T`) is the architect-gated
// follow-up recorded in docs/design/530-webui-entry-surface.md. This shim is the
// enabling slice: it proves the frontend boundary lives in files, off the
// JS-in-revl-strings anti-pattern, using only reviewed language surface.

import type { Context } from 'cordis'

/** The Cordis WebUI entry shape (`@cordisjs/plugin-webui` base/entry.ts). */
interface WebUIEntryFiles {
  dev: string
  prod: string
  routes: string[]
}

/** The Cordis `Context`, reached through the retained embedder bridge. Kept a
 * private helper so the single failure mode (no bridge installed) is one loud
 * message rather than an undefined-property throw deep in Cordis. */
function revlWebuiCtx(): Context {
  const ctx = (globalThis as { __revlWebui?: Context }).__revlWebui
  if (!ctx) {
    throw new Error(
      'revl webui: no Cordis ctx bridge installed. The embedder must set ' +
      'globalThis.__revlWebui to the plugin Context before the webui_add_entry ' +
      'extern is first called (docs/design/530-webui-entry-surface.md).',
    )
  }
  return ctx
}

/**
 * Register a frontend entry with Cordis WebUI. `devSource` and `prodManifest`
 * are paths to EXTERNAL asset files (a real Vite/Vue project); `routes` are the
 * client route patterns the entry claims. Returns `devSource` as a stable entry
 * handle so a revl caller can bind it. No HTML/CSS/JS is embedded here; the
 * assets are referenced by path.
 */
export function webuiAddEntry(
  devSource: string,
  prodManifest: string,
  routes: string[],
): string {
  const ctx = revlWebuiCtx()
  const files: WebUIEntryFiles = { dev: devSource, prod: prodManifest, routes }
  // `data` is the reactive-state / RPC object. The enabling slice publishes an
  // empty surface; the typed-contract version is design note 530.
  ;(ctx as unknown as { webui: { addEntry(f: WebUIEntryFiles, d: unknown): unknown } })
    .webui.addEntry(files, {})
  return devSource
}
