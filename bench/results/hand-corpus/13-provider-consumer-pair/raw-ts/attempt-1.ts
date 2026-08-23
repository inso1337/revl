import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// clean: self-contained mesh. MemKv is mounted as a CHILD of this fiber
// (ctx.plugin, not ctx.root.plugin), so it disposes when we do; NameBook
// provides on its own fiber.
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
  name: 'NameBookMesh',
  apply(ctx: Context) {
    ctx.plugin(MemKv)
    ctx.inject(['kv'], (scope: Context) => {
      scope.effect(function* () {
        yield (scope as unknown as Provider).provide('names', {
          remember: (name: string) => { (scope as any).kv.put(name, name) },
          recall: (name: string) => (scope as any).kv.get(name),
        })
      }, 'NameBook.body')
    })
  },
}
