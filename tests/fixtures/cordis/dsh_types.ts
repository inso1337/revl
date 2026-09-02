// The DSH plugin-inventory types, in the exact spelling the real gateway uses.
// `idioms.ts` covers the same ground with the friendlier spellings; this file
// takes every hard variant at once, so the whole item 116/134/137/138 chain is
// exercised by one import:
//   * `Branded` arrives from ANOTHER PACKAGE, so the alias body is not in reach
//     and the branded id collapses to `Str` through the documented fallback
//     rather than by following a local alias (idioms.ts defines `Branded`
//     locally, which resolves by a different path);
//   * the phase union is written one member per line, each with a LEADING `|`
//     (DSH's real formatting), and closes with `| null`;
//   * the load-kind union takes the `=`-line-then-bare-first-member shape, which
//     used to collapse silently to just its first member;
//   * the records nest both and are reached only by following the plugin's
//     `.ts`-extension local import.
import { Branded } from '@deepseek-ai/dsh-brand'

export type PluginEntryId = Branded<'PluginEntryId'>

export type PluginFiberPhase =
  | 'pending'
  | 'loading'
  | 'active'
  | 'failed'
  | 'unloading'
  | null

export type PluginLoadKind =
  'eager'
  | 'lazy'

export interface PluginInventoryEntry {
  readonly entryId: PluginEntryId
  readonly phase: PluginFiberPhase
  readonly kind: PluginLoadKind
  readonly enabled: boolean
}

export interface PluginInventorySnapshot {
  readonly total: bigint
  readonly entries: readonly PluginInventoryEntry[]
  readonly generatedAt: string
}
