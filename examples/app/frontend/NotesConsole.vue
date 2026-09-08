<!--
  The notes console screen (roadmap item 459 / 462). A normal Vue 3 single-file
  component that Vite compiles with source maps to THIS original. It consumes the
  notes app's TYPED ROUTES through `NotesApiClient`, the client
  `revl export client --lang ts --service NotesApi` derives from the `NotesApi`
  declaration in `../notes.rvl` (item 457 artifact 5): one declaration is both the
  server's router and this browser call surface, so a field the `Note` model does
  not declare is a compile error here, not a runtime surprise.

  No revl string carries any of this markup: it is an external asset the console
  component names by path through its `webui` coeffect (see `../notes.rvl`).
-->
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NotesApiClient, type Note, type NewNote } from './notes.client'
import type { NotesConsoleChannel } from './contract'

// The typed REST client onto `revl serve --http`. `base` is the routed prefix the
// server mounts `NotesApi` under; the client builds each path from the route
// table and decodes the canonical `Result[T, ApiError]` into the union below.
const props = defineProps<{
  base?: string
  // The synced reactive state + RPC surface Cordis WebUI hands the page through
  // `useRpc<NotesConsoleChannel>()` (see `./entry.client.ts`). Optional because
  // the server publishes an empty reactive surface until 457 S4 lands the typed
  // `--face webui` projection (see `./contract.ts` filed gap).
  channel?: NotesConsoleChannel
}>()

const api = new NotesApiClient(props.base ?? '/notes-api')

const notes = ref<Note[]>([])
const draft = ref<NewNote>({ title: '', body: '' })
const error = ref<string | null>(null)
const busy = ref(false)

async function refresh(): Promise<void> {
  busy.value = true
  error.value = null
  const res = await api.list_notes(null)
  if (res.$kind === 'Ok') {
    notes.value = res.$value
  } else {
    // Outcome read from the TYPED contract (the `Err(ApiError)` arm), never from
    // parsing body prose (525 acceptance bar 3).
    error.value = `${res.$value.code}: ${res.$value.message}`
  }
  busy.value = false
}

async function create(): Promise<void> {
  if (!draft.value.title) {
    error.value = 'title is required'
    return
  }
  const res = await api.create_note({ title: draft.value.title, body: draft.value.body })
  if (res.$kind === 'Ok') {
    draft.value = { title: '', body: '' }
    await refresh()
  } else {
    error.value = `${res.$value.code}: ${res.$value.message}`
  }
}

onMounted(refresh)
</script>

<template>
  <section class="notes-console">
    <header>
      <h1>Notes</h1>
      <p v-if="props.channel" class="ranker">
        ranking: {{ props.channel.strategy }} ({{ props.channel.signals }} signals)
      </p>
    </header>

    <form @submit.prevent="create">
      <input v-model="draft.title" placeholder="title" aria-label="title" />
      <textarea v-model="draft.body" placeholder="body" aria-label="body"></textarea>
      <button type="submit" :disabled="busy">Add note</button>
    </form>

    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <ul>
      <li v-for="note in notes" :key="note.id">
        <strong>{{ note.title }}</strong>
        <span>{{ note.body }}</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.notes-console { max-width: 40rem; margin: 0 auto; font-family: system-ui, sans-serif; }
.notes-console form { display: grid; gap: 0.5rem; margin-block: 1rem; }
.notes-console .error { color: #b00020; }
.notes-console .ranker { color: #555; font-size: 0.9rem; }
</style>
