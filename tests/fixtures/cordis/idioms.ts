// The idiomatic DSH types the plugin-inventory gateway traffics in, imported by
// the plugin WITH an explicit `.ts` extension (the NodeNext/DSH spelling). It
// combines every layer this importer must now see through:
//   * a branded-string id (`Branded<'…'>` -> `Str`),
//   * a literal-only phase union (`… | null` -> `Opt[<variant>]`),
//   * records that nest both, reached by following the plugin's local import.

export type Branded<T> = string & { readonly __brand: T }
export type PluginEntryId = Branded<'PluginEntryId'>

export type PluginFiberPhase =
  'pending' | 'loading' | 'active' | 'failed' | 'unloading' | null

export interface PluginInventoryEntry {
  id: PluginEntryId
  phase: PluginFiberPhase
  loadedAt: string
}

export interface PluginInventorySnapshot {
  total: bigint
  entries: PluginInventoryEntry[]
  generatedAt: string
}
