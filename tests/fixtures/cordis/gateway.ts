import { Context } from 'cordis'
// The base is imported from ANOTHER PACKAGE, not a local file — the real DSH
// gateway extends a protocol base shipped by `@deepseek-ai/dsh-typert-protocol`.
// It follows cordis's `*Service` naming convention, so it is a service root and
// the subclass's own decorated operations must be recovered.
import { TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
// A local import that carries the `.ts` extension (NodeNext/DSH spelling).
import type { PluginInventorySnapshot } from './types.ts'

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
  listPlugins(): PluginInventorySnapshot {
    return { total: 0n, plugins: [], generatedAt: '' }
  }

  /** Forget a plugin by id. */
  @Remote('forget')
  forget(id: string): void {}
}
