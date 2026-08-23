import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// LEAK: Auditor needs kv, so it brings up its own MemKv — but mounts it on the
// ROOT (ctx.root.plugin), so MemKv (and its 'kv' provision) outlive the mesh's
// teardown. Auditor's own 'audited' provision is fiber-scoped and clean; the
// leak is entirely the escaped dependency. (Should have been ctx.plugin(MemKv).)
const MemKv = {
  name: 'MemKv',
  provide: ['kv'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      const store = host.Map.new()
      yield () => store.drop()
      yield (ctx as unknown as Provider).provide('kv', {
        get: (key: string) => store.get(key),
        put: (key: string, value: string) => { store.insert(key, value) },
      })
    }, 'MemKv.body')
  },
}

export const plugin = {
  name: 'Auditor',
  provide: ['audited'],
  apply(ctx: Context) {
    ctx.root.plugin(MemKv) // <-- dependency escapes teardown
    ctx.effect(function* () {
      yield (ctx as unknown as Provider).provide('audited', {
        put_audited: (key: string, value: string) => {
          host.Pool.open('mailer://audit', 1).execute(`send audit ${key}`)
        },
      })
    }, 'Auditor.body')
  },
}
