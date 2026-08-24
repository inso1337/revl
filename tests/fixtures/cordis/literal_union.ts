import { Context, Service } from 'cordis'

// DSH's literal-only union idiom: a closed set of string tags. Refused today as
// "a sum type with no tag" — but for a literal-only union the string literals ARE
// the tags, so the importer synthesizes a named `variant`. A `| null` member wraps
// the whole variant in `Opt`; a union with no `null` maps to the bare variant.
export type PluginFiberPhase =
  'pending' | 'loading' | 'active' | 'failed' | 'unloading' | null

export type PluginLoadKind = 'eager' | 'lazy'

export const name = 'fibers'

export class Fibers extends Service {
  constructor(ctx: Context) {
    super(ctx, 'fibers')
  }

  /** The fiber's current phase, or null before it has loaded. */
  phase(): PluginFiberPhase {
    return null
  }

  /** Schedule a fiber with the given load kind. */
  schedule(kind: PluginLoadKind): void {}
}
