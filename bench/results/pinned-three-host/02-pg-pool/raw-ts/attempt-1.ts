import type { Context } from 'cordis'
import { host } from './host.ts'

export const plugin = {
  name: 'PgDatabase',
  provide: ['db'],
  apply(ctx: Context, config: { url: string; pool_size?: number }) {
    ctx.effect(function* () {
      const pool = host.Pool.open(config.url, config.pool_size ?? 10)
      yield () => pool.close()

      yield ctx.provide('db', {
        query: (sql: string) => pool.query(sql),
        execute: (sql: string) => pool.execute(sql),
      })
    }, 'PgDatabase.body')
  },
}
