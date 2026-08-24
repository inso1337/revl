// The record type the gateway's operations traffic in. Imported by the plugin
// WITH an explicit `.ts` extension (`import type { … } from './types.ts'`), the
// NodeNext/DSH spelling — the importer must resolve the extension to this file.

export interface PluginInventorySnapshot {
  total: bigint
  plugins: string[]
  generatedAt: string
}
