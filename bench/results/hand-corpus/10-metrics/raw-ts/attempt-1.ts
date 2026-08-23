import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// clean: scratch Map + provision fiber-scoped.
export const plugin = {
  name: 'Metrics',
  provide: ['metrics'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      const store = host.Map.new()
      yield () => store.drop()
      yield (ctx as unknown as Provider).provide('metrics', {
        read: (name: string) => store.get(name),
        bump: (name: string, v: string) => { store.insert(name, v) },
      })
    }, 'Metrics.body')
  },
}
