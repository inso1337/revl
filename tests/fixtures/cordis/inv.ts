import { Context, Service } from 'cordis'
// NodeNext spells a relative import to a `.ts` source with a `.js` extension.
import type { PluginInventorySnapshot } from './types.js'

export class Inv extends Service {
  constructor(ctx: Context) {
    super(ctx, 'inv')
  }

  snapshot(): PluginInventorySnapshot {
    return { total: 0n, plugins: [], generatedAt: '' }
  }
}
