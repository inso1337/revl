// The frontend entry ASSET for the exemplary notes app (roadmap item 459 / 462).
//
// A normal Cordis WebUI client extension (`@cordisjs/plugin-webui`,
// client/plugins/loader.ts:15 `defineExtension`): a real Vite/Vue source file
// compiled with source maps to THIS original. The revl program never carries this
// code as a string — the `NotesConsole` component in `../notes.rvl` names this
// file BY PATH through its `webui` coeffect, which is exactly the property 459's
// exit test asks for ("no JS extracted from revl strings and source maps pointing
// at the original files", design docs/design/526-webui-asset-alignment.md).
//
// It registers one page at `/notes` (the route the coeffect declares) rendering
// the `NotesConsole.vue` screen, and reads the server's synced reactive state /
// RPC surface through `useRpc<NotesConsoleChannel>()` — the typed contract in
// `./contract.ts`. Until the server projects that surface (457 S4, the filed gap
// in `./contract.ts`), `useRpc` resolves to an empty object and the page drives
// note state through the typed REST routes instead; the type is already the
// one-declaration shape so no rewrite is needed when the projection lands.

import { defineExtension, useRpc } from '@cordisjs/client'
import NotesConsole from './NotesConsole.vue'
import type { NotesConsoleChannel } from './contract'

export default defineExtension((ctx) => {
  ctx.page({
    path: '/notes',
    name: 'Notes',
    component: NotesConsole,
    // The synced server channel, typed against the shared contract. `useRpc`
    // returns a ref onto the reactive `data` the server published through
    // `webui.add_entry`; the console binds it as the `channel` prop.
    fields: () => ({ channel: useRpc<NotesConsoleChannel>() }),
  })
})
