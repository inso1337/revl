import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// LEAK: the limiter map is fiber-scoped and fine, but the periodic-sweep
// listener is bound with ctx.root.on(...) — its removal is collected by the
// ROOT fiber, which is never disposed, so the listener accumulates every mount.
// (Should have been ctx.on(...).)
export const plugin = {
  name: 'RateLimiter',
  provide: ['limiter'],
  apply(ctx: Context) {
    ctx.root.on('internal/info' as never, () => {}) // <-- root-scoped listener
    ctx.effect(function* () {
      const seen = host.Map.new()
      yield () => seen.drop()
      yield (ctx as unknown as Provider).provide('limiter', {
        allow: (key: string) => seen.get(key),
        record: (key: string) => { seen.insert(key, '1') },
      })
    }, 'RateLimiter.body')
  },
}
