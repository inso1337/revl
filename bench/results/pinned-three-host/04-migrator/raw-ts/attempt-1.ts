import type { Context } from 'cordis'
import { host } from './host.ts'

export const plugin = {
  name: 'Migrator',
  inject: ['db'],
  apply(ctx: Context) {
    ctx.effect(async function* () {
      // Self-contained: provide the Database service the Migrator requires.
      const pool = host.Pool.open('postgres://localhost/app', 10)
      yield () => pool.close()

      const db = {
        query: (sql: string) => pool.query(sql),
        execute: (sql: string) => pool.execute(sql),
      }
      yield ctx.provide('db', db)

      // 1. Take a Postgres advisory lock; undone by the unlock query.
      db.query('SELECT pg_advisory_lock(4242)')
      yield () => db.query('SELECT pg_advisory_unlock(4242)')

      // 2. Run migrations.
      const job = host.Job.run('migrations')
      yield () => job.cancel()
      await job

      // 3. Emit the migration_log INSERT, compensated by the matching DELETE.
      db.execute("INSERT INTO migration_log (version, applied_at) VALUES ('001', now())")
      yield () => db.execute("DELETE FROM migration_log WHERE version = '001'")
    }, 'Migrator.body')
  },
}
