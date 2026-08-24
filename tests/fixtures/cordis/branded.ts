import { Context, Service } from 'cordis'

// DSH's branded-string idiom: `Branded<T>` is a `string` tagged with a phantom
// brand so two id kinds don't interchange. `Branded<T>` is an *unmapped* generic
// to the importer — but its alias resolves, through the intersection, to `string`,
// and the outer alias `PluginEntryId` resolves through it, so a field typed
// `PluginEntryId` imports as `Str`.
export type Branded<T> = string & { readonly __brand: T }
export type PluginEntryId = Branded<'PluginEntryId'>

export const name = 'entries'

export class Entries extends Service {
  constructor(ctx: Context) {
    super(ctx, 'entries')
  }

  /** Look up an entry by its branded id. */
  get(id: PluginEntryId): void {}
}
