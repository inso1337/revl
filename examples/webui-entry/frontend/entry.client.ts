// The frontend entry ASSET for the webui-entry reference (roadmap item 459).
//
// This is a normal Cordis WebUI client extension (`@cordisjs/plugin-webui`
// client/plugins/loader.ts:15, `defineExtension`). It is a REAL source file that
// a Vite/Vue build compiles with source maps; in a full 462 app it would import
// `.vue` single-file components and register pages through
// `ctx.client.router.page(...)`. The revl program never contains this code as a
// string: it references this file BY PATH through webui_host.ts's `addEntry`
// call, which is exactly the property 459's exit test asks for ("no JS extracted
// from revl strings and source maps pointing at the original files").
//
// Kept deliberately tiny: the exemplary frontend is 462's deliverable, gated on
// the app that does not exist yet. This file exists so the reference is a
// runnable shape rather than a dangling path.

// The Cordis client context type lives in `@cordisjs/plugin-webui`'s client
// entry; typed loosely here so the example stands alone without the client
// devDependency installed.
export default function (ctx: { page?: (opts: unknown) => void }): void {
  // A real entry registers pages/slots here, e.g.
  //   ctx.page?.({ path: '/console', name: 'Console', component: Console })
  // and reads synced server state through `useRpc<T>()`. The typed contract for
  // that server-to-client surface is docs/design/530-webui-entry-surface.md.
  void ctx
}
