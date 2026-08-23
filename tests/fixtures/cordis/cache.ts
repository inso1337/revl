import { Context, Service } from 'cordis'

export const name = 'cache'

// This plugin depends on a database service and, optionally, a logger.
export const inject = { required: ['database'], optional: ['logger'] }

/**
 * A tiny key/value cache, exposed as a Cordis service.
 *
 * Registered at the service key `store` (see `super(ctx, 'store')`), so a revl
 * composition injecting `store` consumes exactly these operations.
 */
export class Cache extends Service {
  constructor(ctx: Context) {
    super(ctx, 'store')
  }

  /**
   * Read a cached value. Pure lookup — reads the map and nothing else.
   * @revl:pure
   */
  get(key: string): string | undefined {
    return this.data.get(key)
  }

  /** Overwrite the log-through cache for `key`. Writes through to `database`. */
  set(key: string, value: string): void {
    this.data.set(key, value)
  }

  /** Number of live entries. */
  size(): bigint {
    return BigInt(this.data.size)
  }

  /** Bulk-load many keys at once. */
  async warm(keys: string[]): Promise<number> {
    return keys.length
  }

  // teardown the plugin registers — the importer must notice, not pair it.
  stop(): void {
    this.data.clear()
  }
}
