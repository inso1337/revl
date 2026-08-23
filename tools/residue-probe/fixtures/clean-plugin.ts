// A well-behaved Cordis plugin: everything it acquires is tied to its own fiber
// scope, so unmounting reverts all four contract categories to baseline.
//
//  - the listener goes through `ctx.on` (collected by THIS fiber's scope, so
//    dispose removes it),
//  - the service goes through `ctx.provide` yielded from `ctx.effect` — the
//    exact shape revl emits (backends/typescript/golden/user_cache.ts), whose
//    withdrawal disposer the runtime runs on teardown,
//  - the extra `ctx.effect` disposer is likewise fiber-scoped.
//
// The probe must report 0 leaks for this plugin over any number of cycles.

import type { Context } from 'cordis'

export const plugin = {
  name: 'CleanPlugin',
  inject: [],
  provide: ['greeter'],
  apply(ctx: Context) {
    // fiber-scoped listener — auto-removed on dispose
    ctx.on('internal/info' as never, () => {})

    ctx.effect(function* () {
      const state = { greetings: 0 }
      // a plain fiber-scoped disposer
      yield () => {
        state.greetings = 0
      }
      // the provision: withdrawal is the runtime's own disposer
      yield (ctx as unknown as { provide(key: string, value: unknown): () => void }).provide('greeter', {
        greet(who: string) {
          state.greetings++
          return `hello, ${who}`
        },
      })
    }, 'CleanPlugin.body')
  },
}

export const config = undefined
