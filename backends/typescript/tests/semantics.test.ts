// R1, R2, R4 and R5 semantics tests (docs/backend-ir.md §Required semantics),
// run against the checked-in golden module.
//
// R3 lives next door in `semantics_r3.test.ts`, and the split is load-bearing
// rather than cosmetic (issue #223). Its fixture `r3_migrator.ir.json` also
// provides `db`, and every emitted module declares its provisions by
// augmenting cordis' GLOBAL `Context` — so a file importing both this golden
// and that fixture merges two different `Database` types onto `Context.db`,
// and `ctx.db` in it resolves to whichever won. That is TS2717, and it is not
// an emitter defect: it is what the augmentation shape costs. One provider of
// a given service name per test file keeps every `ctx.<key>` here honest.
import { beforeEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { Context, FiberState } from 'cordis'
import { PgDatabase, UserCache } from '../golden/user_cache.ts'
import {
  assertNoResidue,
  fiberStateName,
  hostLog,
  liveResources,
  resetHost,
  snapshotRuntime,
} from '../runtime.ts'

beforeEach(() => resetHost())

describe('R1 — LIFO recovery', () => {
  it('unloads accumulated undos in reverse order, including provide-method effects', async () => {
    const ctx = new Context()
    const db = await ctx.plugin(PgDatabase, { url: 'pg://r1' })
    const cache = await ctx.plugin(UserCache)

    ctx.cache.put('k1', 'v1')
    ctx.cache.put('k2', 'v2')

    // The two put-effects joined the component's accumulator alongside the
    // activation body (coeffect operations are effects).
    const labels = cache.getEffects().map((meta) => meta.label)
    expect(labels).toContain('UserCache.body')
    expect(labels.filter((label) => label === 'anonymous')).toHaveLength(2)

    const start = hostLog.length
    await cache.dispose()

    // Effects were: Map.new, provide cache, insert k1, insert k2.
    // LIFO recovery: remove k2, remove k1, (withdraw cache), drop store.
    expect(hostLog.slice(start)).toEqual([
      'map#1.remove(k2)',
      'map#1.remove(k1)',
      'map#1.drop',
    ])

    await db.dispose()
  })
})

describe('R2 — reactive resolution', () => {
  it('activates only when required keys are provided, deactivates on withdrawal, reactivates on replacement', async () => {
    const ctx = new Context()
    const transitions: string[] = []
    ctx.on('internal/status', (fiber: any, oldState: number) => {
      if (fiber.name !== 'UserCache') return
      transitions.push(`${fiberStateName(oldState)}->${fiberStateName(fiber.state)}`)
    })

    const cache = ctx.plugin(UserCache)
    await cache
    expect(cache.state).toBe(FiberState.PENDING) // requirement missing

    const db1 = await ctx.plugin(PgDatabase, { url: 'pg://one' })
    await cache.await()
    expect(cache.state).toBe(FiberState.ACTIVE)

    await db1.dispose()
    expect(cache.state).toBe(FiberState.PENDING) // withdrawn -> deactivated

    const db2 = await ctx.plugin(PgDatabase, { url: 'pg://two' })
    await cache.await()
    expect(cache.state).toBe(FiberState.ACTIVE) // reactivated against replacement
    expect(hostLog).toContain('map#2.new') // effects ran afresh

    expect(transitions).toEqual([
      'PENDING->LOADING',
      'LOADING->ACTIVE',
      'ACTIVE->UNLOADING',
      'UNLOADING->PENDING',
      'PENDING->LOADING',
      'LOADING->ACTIVE',
    ])

    await cache.dispose()
    await db2.dispose()
  })
})

describe('R4 — no-residue', () => {
  it('after unloading everything the runtime matches its pre-load snapshot', async () => {
    const ctx = new Context()
    const baseline = snapshotRuntime(ctx)

    const db = await ctx.plugin(PgDatabase, { url: 'pg://r4' })
    const cache = await ctx.plugin(UserCache)
    ctx.cache.put('a', '1')

    await cache.dispose()
    await db.dispose()

    expect(() => assertNoResidue(ctx, baseline)).not.toThrow()
    expect(snapshotRuntime(ctx).registrySize).toBe(0)
    expect(Reflect.ownKeys(ctx.reflect.store)).toHaveLength(0)
    expect(liveResources.size).toBe(0)
  })
})

describe('R5 — provision withdrawal is derived', () => {
  it('the emitted code provisions only through the runtime (ctx.provide)', () => {
    const source = readFileSync(new URL('../golden/user_cache.ts', import.meta.url), 'utf-8')
    // Both IR provisions lower to the runtime's revertible provide...
    expect(source.match(/yield ctx\.provide\(/g)).toHaveLength(2)
    // ...and nothing hand-rolls withdrawal (no reflect-store surgery, no
    // manual deletion, no bespoke service bookkeeping).
    expect(source).not.toContain('reflect.store')
    expect(source).not.toContain('delete ')
    expect(source).not.toContain('internal/service')
  })

  it('withdrawal is performed by the runtime disposer on unload', async () => {
    const ctx = new Context()
    const db = await ctx.plugin(PgDatabase, { url: 'pg://r5' })
    expect(ctx.get('db')).toBeDefined()

    await db.dispose()
    expect(ctx.get('db')).toBeUndefined()
    expect(Reflect.ownKeys(ctx.reflect.store)).toHaveLength(0)
  })
})
