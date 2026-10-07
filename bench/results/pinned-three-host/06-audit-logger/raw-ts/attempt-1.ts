import type { Context } from 'cordis'
import { host } from './host.ts'

type Row = Record<string, unknown>

export const plugin = {
  name: 'AuditLogger',
  provide: ['db', 'audit'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      // AuditLogger requires db: Database. Since the plugin must be
      // self-contained on a bare Context, install a Database provider
      // backed by a host pool and scope its release to this fiber.
      const pool = host.Pool.open('sqlite://audit.db', 10)
      yield () => pool.close()

      yield ctx.provide('db', {
        query: (sql: string) => pool.query(sql) as Row[],
        execute: (sql: string) => pool.execute(sql),
      })

      // Provide the Audit service. log is an emission: it performs an
      // irreversible INSERT of the event into audit_log.
      yield ctx.provide('audit', {
        log: (event: string) => {
          const sql = `INSERT INTO audit_log (event) VALUES ('${event}')`
          pool.execute(sql)
        },
      })
    }, 'AuditLogger.body')
  },
}
