import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// clean: holder Map + provision fiber-scoped.
export const plugin = {
  name: 'LockManager',
  provide: ['locks'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      const held = host.Map.new()
      yield () => held.drop()
      yield (ctx as unknown as Provider).provide('locks', {
        acquire_lock: (name: string) => { held.insert(name, 'held') },
        holder: (name: string) => held.get(name),
      })
    }, 'LockManager.body')
  },
}
