import { Context, Service } from 'cordis'

export const name = 'subclassed'

// A common real-plugin shape: a project-local base class sits between the
// plugin and cordis's `Service`. The service surface must be recovered through
// the base chain, not just a literal `extends Service`.
export abstract class BaseStore extends Service {
  constructor(ctx: Context, key: string) {
    super(ctx, key)
  }

  /** Number of live entries — defined on the shared base. */
  size(): bigint {
    return 0n
  }
}

export class Sessions extends BaseStore {
  constructor(ctx: Context) {
    super(ctx, 'sessions')
  }

  /** Look up a session token by id. */
  lookup(id: string): string | undefined {
    return undefined
  }
}
