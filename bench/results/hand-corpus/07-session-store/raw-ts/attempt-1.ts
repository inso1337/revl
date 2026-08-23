import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// LEAK: publishes the service with ctx.root.provide(...) instead of yielding
// ctx.provide from its own effect. The provision lands in the root reflect
// store and its withdrawal is never tied to this fiber, so 'sessions' lingers
// after unmount. The backing Map IS fiber-scoped, so only provisions leak.
export const plugin = {
  name: 'SessionStore',
  provide: ['sessions'],
  apply(ctx: Context, config: { ttl?: number } = {}) {
    ctx.effect(function* () {
      const store = host.Map.new()
      yield () => store.drop()
      // mistake: provide on root, disposer discarded
      ;(ctx.root as unknown as Provider).provide('sessions', {
        get: (sid: string) => store.get(sid),
        put: (sid: string, user: string) => { store.insert(sid, user) },
        drop_session: (sid: string) => { store.remove(sid) },
      })
    }, 'SessionStore.body')
  },
}
