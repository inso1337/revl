import { Context } from 'cordis'
// The base ships from another package and follows cordis's `*Service` naming
// convention, so it is a service root and the subclass's own decorated
// operations are recovered (item 134).
import { TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
// A local import carrying the `.ts` extension (NodeNext/DSH spelling); the
// idiomatic types it names are branded strings and literal-only unions (item 137).
import type { PluginInventorySnapshot, PluginEntryId } from './idioms.ts'

export const name = 'plugin-inventory'

export const inject = { required: ['database'], optional: ['logger'] }

export class PluginInventoryGateway extends TypertRemoteService {
  constructor(ctx: Context) {
    super(ctx, 'pluginInventory')
  }

  /**
   * List every plugin the gateway knows about.
   * @revl:pure
   */
  @Remote('list')
  list(): PluginInventorySnapshot {
    return { total: 0n, entries: [], generatedAt: '' }
  }

  /** Forget a plugin by its branded id. */
  @Remote('forget')
  forget(id: PluginEntryId): void {}
}
