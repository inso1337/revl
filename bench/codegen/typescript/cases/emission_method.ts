// CASE: an emission provide-method containing a witnessed `effect ... undo`.
//
// The emitted `put` allocates TWO closures per call: the `ctx.effect(() =>
// {...})` body and the disposer it returns. The disposer genuinely varies per
// call (it captures `key`), but the body closure captures only `store`, `key`
// and `value`, and its only job is to run one statement and hand back the
// disposer.
//
// Emitter site: backends/typescript/emit.py, `_emit_method_body`'s
// `effect`/`let-effect` branch inside a provide method.
//
// This case exists mainly as a NEGATIVE CONTROL: the hand version cannot drop
// the per-call disposer, so the achievable saving is bounded and the numbers
// below say how bounded.

import { Context } from 'cordis'
import { MemCache } from '../emitted/emission_method.ts'
import { host } from '../runtime.ts'

export const name = 'emission-method-effect'
export const summary =
  'each emission-method call allocates the ctx.effect body closure as well as ' +
  'the disposer; only the body closure is avoidable'

export const provenance = [
  { file: 'emission_method.ts', snippet: 'ctx.effect(() => {' },
  { file: 'emission_method.ts', snippet: 'return () => store.remove(key)' },
]

let emittedCache: any = null
let handCache: any = null

// hand-written: the effect body is a hoisted two-argument function, so only
// the disposer (which must capture `key`) is fresh per call
function handProvide (ctx: any, store: any) {
  const insert = (key: string, value: string) => {
    store.insert(key, value)
    return () => store.remove(key)
  }
  return {
    get (key: string) { return store.get(key) },
    put (key: string, value: string) { ctx.effect(insert.bind(null, key, value)) },
  }
}

const HandCache = {
  name: 'HandCache',
  inject: [],
  provide: ['handcache'],
  apply (ctx: any) {
    ctx.effect(function* () {
      const store = host.Map.new()
      yield () => store.drop()
      yield ctx.provide('handcache', handProvide(ctx, store))
    }, 'HandCache.body')
  },
}

export async function setup () {
  const ctx: any = new Context()
  await ctx.plugin(MemCache as any).await()
  await ctx.plugin(HandCache as any).await()
  emittedCache = ctx.cache
  handCache = ctx.handcache
  emittedCache.put('probe', '1')
  handCache.put('probe', '1')
  if (emittedCache.get('probe') !== handCache.get('probe')) {
    throw new Error('emission_method disagreement')
  }
}

// EXECUTED closure count: intercept the activation's `ctx.effect` and record
// the identity of every function object it is handed, so "one fresh closure per
// call" is a measured fact rather than a reading of the source.
export async function effectCalls () {
  const ctx: any = new Context()
  const seen = new Set<unknown>()
  let calls = 0
  await ctx.plugin({
    name: 'Probe',
    inject: [],
    provide: ['cache'],
    apply (inner: any) {
      const real = inner.effect.bind(inner)
      inner.effect = (fn: any, ...rest: any[]) => { calls++; seen.add(fn); return real(fn, ...rest) }
      ;(MemCache as any).apply(inner)
    },
  }).await()
  calls = 0
  seen.clear()
  const cache = ctx.cache
  for (let i = 0; i < 10; i++) cache.put('p' + i, 'v')
  return `executed: 10 put() calls -> ${calls} ctx.effect calls, ` +
         `${seen.size} distinct body closures (one fresh per call)`
}

let seq = 0
export const opsPerRun = 50
export const emitted = async () => { for (let i = 0; i < 50; i++) emittedCache.put('k' + (seq++), 'v') }
export const hand = async () => { for (let i = 0; i < 50; i++) handCache.put('k' + (seq++), 'v') }
// used ONLY by `run.mjs --timing` on an idle machine
export const emittedHot = async () => { for (let i = 0; i < 400; i++) emittedCache.put('k' + (seq++), 'v') }
export const handHot = async () => { for (let i = 0; i < 400; i++) handCache.put('k' + (seq++), 'v') }
