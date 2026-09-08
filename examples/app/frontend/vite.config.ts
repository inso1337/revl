// Vite build for the notes console (roadmap item 459 / 462).
//
// The build EMITS SOURCE MAPS (`build.sourcemap`) so the shipped bundle points
// back at these originals — `entry.client.ts`, `NotesConsole.vue`, `contract.ts`,
// `notes.client.ts` — which is exactly 459's "source maps pointing at the
// original files". It writes a Vite `manifest.json` under `dist/.vite/`, the file
// the `NotesConsole` component in `../notes.rvl` names as its production entry
// through the `webui` coeffect (design docs/design/526-webui-asset-alignment.md).
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    manifest: true,
    sourcemap: true,
    lib: {
      entry: './entry.client.ts',
      formats: ['es'],
      fileName: 'notes-console',
    },
    // Cordis WebUI provides Vue and its client at runtime; the entry does not
    // bundle them (base/entry.ts treats the entry as a module the shell hosts).
    rollupOptions: {
      external: ['vue', '@cordisjs/client'],
    },
  },
})
