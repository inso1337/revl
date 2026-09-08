// The typed server-to-client contract for the notes console (roadmap item 459 /
// 462, design docs/design/526-webui-asset-alignment.md §"typed boundary" and
// docs/design/530-webui-entry-surface.md Decision B).
//
// Cordis WebUI's `addEntry(files, data)` publishes a reactive `data` object: the
// server mutates it and the delta is broadcast to the browser over a WebSocket,
// and any function on it becomes an RPC method the browser can call (526). 459's
// typed-boundary requirement is that this surface be a TYPED CONTRACT shared
// between the server component and the TS client, so the browser's `useRpc<T>()`
// type and the server's published fields are ONE declaration.
//
// FILED GAP (docs/design/525-webapp-slice4-frontend.md §G3, against 459/457).
// revl cannot yet PROJECT this contract from the `NotesApi` service declaration:
// the landed webui coeffect's `add_entry(dev, prod, routes)` has no `data`
// parameter, so the server publishes an empty reactive surface and this file is
// authored on the TS side rather than derived. Decision B (530) folds the typed
// `data`/RPC projection into item 457 as `revl export client --lang ts --face
// webui` (457 slice S4), which is not landed. Until it is, the console drives all
// note state through the TYPED REST ROUTES below (the landed typed contract, 457
// artifact 5), and this `NotesConsoleState` is the shape that projection will
// emit — kept here so the client is written against the eventual one-declaration
// surface, not a second hand-rolled one.

// The route wire types are the single source of truth: they come from the client
// `revl export client` derives from the `NotesApi` declaration, so the console
// never restates a field the model does not declare.
export type { Note, NewNote } from './notes.client'

/**
 * The reactive state the server publishes to the console over Cordis WebUI's
 * `data` channel. This is the shape `revl export client --face webui` (457 S4)
 * will project from the `Ranker` service in `notes.rvl`; until that lands the
 * server publishes an empty surface (the filed gap above) and the fields here are
 * populated by the client from the typed REST routes.
 */
export interface NotesConsoleState {
  /** Which hot-swappable scoring build is live (`Ranker.strategy`). */
  readonly strategy: string
  /** How many engagement signals are recorded (`Ranker.signals`). */
  readonly signals: number
}

/**
 * The RPC method surface the browser may call on the server, the enumerable
 * boundary 526 argues revl adds over Cordis' untyped `data: T`: exactly the
 * component's declared provisions, nothing more. Mirrors `Ranker` in `notes.rvl`.
 */
export interface NotesConsoleRpc {
  /** Record one engagement signal for a note (`Ranker.bump`). */
  bump(id: string): Promise<void>
  /** The note's score under the live strategy (`Ranker.score`). */
  score(id: string): Promise<number>
}

/** The full typed channel `useRpc<NotesConsoleChannel>()` reads in the client. */
export type NotesConsoleChannel = NotesConsoleState & NotesConsoleRpc
