import type { Context } from 'cordis'
import { host } from './host.ts'

export const plugin = {
  name: 'MemKv',
  provide: ['kv'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      const map = host.Map.new()
      yield () => map.drop()

      const keys: string[] = []

      yield ctx.provide('kv', {
        get: (key: string) => map.get(key),
        put: (key: string, value: string) => {
          map.insert(key, value)
          keys.push(key)
        },
      })

      // matching undo: every put performed is reverted on teardown by
      // removing its key; runs before map.drop() (LIFO disposers).
      yield () => {
        for (const key of keys) {
          map.remove(key)
        }
      }
    }, 'MemKv.body')
  },
}
