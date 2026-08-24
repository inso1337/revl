import { Context, Service } from 'cordis'

// DSH's REAL formatting for a literal-only union: one member per line, each with
// a leading `|` (and a leading `|` on the whole union too). This is the exact
// shape of DSH's `PluginFiberPhase`. The importer must read the union across all
// its lines — not cut it at the newline after the first member — and drop the
// leading empty fragment the `|` split produces, so it synthesizes the same
// `variant` as the inline form. `| null` still wraps the variant in `Opt`.
export type PluginFiberPhase =
  | 'pending'
  | 'loading'
  | 'active'
  | 'failed'
  | 'unloading'
  | null

// The `=`-line-then-first-member-without-a-leading-`|` shape (the older bug that
// silently collapsed the alias to just its first member -> `Str`). All members
// must still be recovered.
export type PluginLoadKind =
  'eager'
  | 'lazy'

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
