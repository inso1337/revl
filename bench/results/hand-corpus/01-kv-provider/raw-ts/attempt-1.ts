import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// clean: Map + provision both live inside the plugin's own ctx.effect fiber.
export const plugin = {
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
