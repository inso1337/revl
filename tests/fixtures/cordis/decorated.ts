import { Context, Service } from 'cordis'

export const name = 'decorated'

export class Metrics extends Service {
  constructor(ctx: Context) {
    super(ctx, 'metrics')
  }

  /**
   * Read the current counter. A pure read behind a caching decorator.
   * @revl:pure
   */
  @cache
  read(key: string): bigint {
    return 0n
  }

  @throttle(100)
  @audit.log
  record(key: string, value: bigint): void {}
}
