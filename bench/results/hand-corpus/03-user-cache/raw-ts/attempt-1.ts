import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// LEAK: the cache itself is fiber-scoped and clean, but the audit-log sink is
// mounted with ctx.root.plugin(...) — a sibling of the root that survives THIS
// plugin's teardown. Its registry entry, its 'audit' provision, and its body
// effect all leak. (Should have been ctx.plugin(auditSink).)
const auditSink = {
  name: 'AuditSink',
  provide: ['audit'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      const log = host.Map.new()
      yield () => log.drop()
      yield (ctx as unknown as Provider).provide('audit', {
        record: (sql: string) => { log.insert(String(Date.now()), sql) },
      })
    }, 'AuditSink.body')
  },
}

export const plugin = {
  name: 'UserCache',
  provide: ['cache'],
  apply(ctx: Context) {
    ctx.root.plugin(auditSink) // <-- escapes teardown
    ctx.effect(function* () {
      const store = host.Map.new()
      yield () => store.drop()
      yield (ctx as unknown as Provider).provide('cache', {
        get: (key: string) => store.get(key),
        put: (key: string, value: string) => {
          store.insert(key, value)
          host.Pool.open('postgres://localhost/app', 1)
            .execute(`INSERT INTO cache_log VALUES (${key})`)
        },
      })
    }, 'UserCache.body')
  },
}
