// Documented upstream cordis v4 lifecycle behavior (see REPORT.md).
//
// These tests assert the CURRENT upstream behavior, not the desired one, so a
// future cordis release that fixes the underlying gaps (the territory of
// cordiverse/cordis#39) will make them fail loudly and prompt a REPORT update.
// The emitted code never relies on either behavior: the emitter's
// one-generator-per-body lowering avoids finding 1, and revl's type system
// makes finding 2 unrepresentable in source (G5).
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Migrator } from './generated/r3_migrator.ts'
import { host, hostLog, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

describe('upstream finding 1 — top-level fiber effects are disposed concurrently', () => {
  it('a naive per-step lowering closes the pool before dependents finish tearing down', async () => {
    // This provider registers pool + provision as TWO top-level fiber
    // effects (the naive lowering of the IR).  Fiber._unload runs top-level
    // disposers via Promise.all, so the pool closes while the provision
    // withdrawal is still awaiting dependents.
    const NaiveProvider = {
      name: 'NaiveProvider',
      apply(ctx: any) {
        let pool: any
        ctx.effect(() => {
          pool = host.Pool.open('pg://naive', 1)
          return () => pool.close()
        })
        ctx.provide('db', {
          query: (sql: any) => pool.query(sql),
          execute: (sql: any) => pool.execute(sql),
        })
      },
    }

    const ctx = new Context()
    const db = await ctx.plugin(NaiveProvider)
    await ctx.plugin(Migrator)
    expect(hostLog).toContain('pool#1(pg://naive).execute(SELECT pg_advisory_lock(42))')

    await db.dispose()

    // The Migrator's undo tried to unlock through its committed view, but the
    // pool was already closed: the unlock never reached the host and the
    // error was swallowed into the fiber logger.  R3 ordering is violated.
    expect(hostLog).toContain('pool#1(pg://naive).close')
    expect(hostLog).not.toContain('pool#1(pg://naive).execute(SELECT pg_advisory_unlock(42))')
  })
})

describe('upstream finding 2 — effects can be registered during teardown (G5 gap)', () => {
  it('an undo that registers a new effect is accepted while UNLOADING and the effect leaks', async () => {
    let leaked = false
    let leakDisposed = false

    const Provider = {
      name: 'Provider',
      apply(ctx: any) {
        ctx.provide('svc', {})
      },
    }
    const Rogue = {
      name: 'Rogue',
      inject: ['svc'],
      apply(ctx: any) {
        ctx.effect(function* () {
          yield () => {
            // Teardown registering a new effect: revl has no syntactic
            // position for this (undo bodies type in teardown mode), but
            // upstream cordis only guards against DISPOSED fibers
            // (assertActive checks uid, not lifecycle state), so a fiber
            // that is merely UNLOADING accepts it.
            ctx.effect(() => {
              leaked = true
              return () => {
                leakDisposed = true
              }
            })
          }
        })
      },
    }

    const ctx = new Context()
    const provider = await ctx.plugin(Provider)
    const rogue = await ctx.plugin(Rogue)

    await provider.dispose() // withdraw svc -> Rogue deactivates -> undo runs

    expect(leaked).toBe(true) // no INACTIVE_EFFECT was raised
    expect(rogue.state).toBe(FiberState.PENDING)
    // The leaked effect is now held by an INACTIVE fiber...
    expect(rogue.getEffects().length).toBeGreaterThan(0)

    // ...and even disposing the fiber never runs its disposer, because the
    // unload already happened: permanent residue.
    await rogue.dispose()
    expect(leakDisposed).toBe(false)
  })
})
