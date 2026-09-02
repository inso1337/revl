// DSH's real `PluginInventoryGateway` shape, end to end: the whole item
// 116/134/137/138 chain in one plugin. Each landed item closed one refusal and
// the next verification found the following one, so the value of this fixture is
// the COMBINATION: every earlier fixture takes the hard spelling of one layer
// and the friendly spelling of the rest.
//   * item 116: the operations are decorated (`@Remote(...)`), and the record
//     types live across a local import;
//   * item 134: the base `TypertRemoteService` ships from another package, and
//     the local import carries an explicit `.ts` extension;
//   * item 137: the ids are branded strings and the phase is a literal-only
//     union closing in `null`;
//   * item 138: that union is formatted leading-pipe-per-line, and the load-kind
//     alias uses the bare-first-member multiline shape.
import { Context } from 'cordis'
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol'
import type {
  PluginInventorySnapshot,
  PluginEntryId,
  PluginLoadKind,
} from './dsh_types.ts'

export const name = 'plugin-inventory'

export const inject = { required: ['database'], optional: ['logger'] }

export class PluginInventoryGateway extends TypertRemoteService {
  constructor(ctx: Context) {
    super(ctx, 'pluginInventory')
  }

  /**
   * The current inventory snapshot.
   * @revl:pure
   */
  @Remote('list')
  list(): PluginInventorySnapshot {
    return { total: 0n, entries: [], generatedAt: '' }
  }

  /** Schedule a plugin by its branded id, under a load kind. */
  @Remote('schedule')
  schedule(id: PluginEntryId, kind: PluginLoadKind): boolean {
    return true
  }
}
