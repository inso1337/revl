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
// `./contract.ts`, which is GENERATED from the `NotesConsole` declaration by
// `revl export client --lang ts --face webui --component NotesConsole`. The
// server's published fields and this type are therefore one declaration, the way
// `./notes.client.ts` is one declaration with `NotesApi`. Note state still flows
// over the typed REST routes; the channel carries the ranker's live surface and
// the two RPC methods the console provides.

import { defineComponent, h } from 'vue'
import { defineExtension, useRpc } from '@cordisjs/client'
import NotesConsole from './NotesConsole.vue'
import type { NotesConsoleChannel } from './contract'

// `useRpc` is a COMPOSABLE: it injects the shell context and reads the entry's
// reactive `data`, so it has to run inside a component's own `setup`, not in the
// registration call. This one-line page wrapper is where it runs, which keeps
// `NotesConsole.vue` a plain props-driven screen that also renders standalone
// (a unit test, a Storybook-style harness) outside a Cordis WebUI host.
const NotesPage = defineComponent({
  name: 'NotesPage',
  setup() {
    const channel = useRpc<NotesConsoleChannel>()
    return () => h(NotesConsole, { channel: channel.value })
  },
})

export default defineExtension((ctx) => {
  ctx.page({
    path: '/notes',
    name: 'Notes',
    component: NotesPage,
  })
})
