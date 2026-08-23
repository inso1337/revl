import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// clean: pool acquired and released inside the fiber; db provided from it.
export const plugin = {
  name: 'PgDatabase',
  provide: ['db'],
  apply(ctx: Context, config: { url?: string; pool_size?: number } = {}) {
    const url = config.url ?? 'postgres://localhost/app'
    const size = config.pool_size ?? 10
    ctx.effect(function* () {
      const pool = host.Pool.open(url, size)
      yield () => pool.close()
      yield (ctx as unknown as Provider).provide('db', {
        query: (sql: string) => pool.query(sql),
        execute: (sql: string) => pool.execute(sql),
      })
    }, 'PgDatabase.body')
  },
}
