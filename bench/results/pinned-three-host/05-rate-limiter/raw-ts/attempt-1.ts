import type { Context } from 'cordis'
import { host } from './host.ts'

export const plugin = {
  name: 'RateLimiter',
  provide: ['limiter'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      const map = host.Map.new()
      yield () => map.drop()

      yield ctx.provide('limiter', {
        allow: (key: string) => map.get(key),
        record: (key: string) => {
          map.insert(key, '1')
        },
      })
    }, 'RateLimiter.body')
  },
}
