// A leaking Cordis plugin — the exact class of lifecycle bug this probe exists
// to catch. Every leak here is a real, ordinary cordis mistake: attaching work
// to a context that OUTLIVES the plugin's own fiber, so the plugin's dispose
// never reverts it.
//
//  - `ctx.root.on(...)`  — the disposer is collected by the ROOT fiber, which
//    is never disposed, so the listener survives this plugin's teardown.
//    Leaks:  listeners  (a new `internal/info` hook accumulates every cycle)
//            effects    (the root fiber keeps the disposer)
//
//  - `ctx.root.plugin(inner)` — `inner` is mounted as a sibling of the root,
//    not as a child of this fiber, so disposing this fiber leaves it running.
//    Because `inner` PROVIDES a service, its provision lingers too.
//    Leaks:  registry    (inner's plugin runtime stays registered)
//            provisions  (inner's `leaked-svc` stays in the reflect store)
//            effects     (inner's body effect stays live)
//
// Net: this plugin leaves residue in ALL FOUR categories run.py proves clean.

import type { Context } from 'cordis'

const inner = {
  name: 'LeakedInner',
  inject: [],
  provide: ['leaked-svc'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      yield (ctx as unknown as { provide(key: string, value: unknown): () => void }).provide('leaked-svc', {
        tick: () => Date.now(),
      })
    }, 'LeakedInner.body')
  },
}

export const plugin = {
  name: 'LeakyPlugin',
  inject: [],
  apply(ctx: Context) {
    // listener bound to the root scope, not ours → never removed on our dispose
    ctx.root.on('internal/info' as never, () => {})
    // a whole plugin mounted on root → escapes our teardown entirely
    ctx.root.plugin(inner)
  },
}

export const config = undefined
