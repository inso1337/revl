// Documented cordis v4 lifecycle behavior (see REPORT.md).
//
// Finding 1 asserts the CURRENT upstream behavior: cordis disposes a fiber's
// top-level effects concurrently, which a future release fixing the gap (the
// territory of cordiverse/cordis#39) will make this test fail loudly and
// prompt a REPORT update.
//
// Finding 2 was the same class of gap — an undo could register an effect
// while the fiber was merely UNLOADING (assertActive checked uid, not
// lifecycle state), leaving permanent residue. It is FIXED in the pinned
// fork (inso1337/cordis@harden-assert-active, see package.json): the test
// below now pins the FIXED behavior (a red-on-fix characterization test
// flipped green), so it fails loudly if the pin ever drifts back to the
// upstream rc.8 guard.
//
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

describe('upstream finding 2 — effects registered during teardown are refused (G5 gap, fixed in the pinned fork)', () => {
  it('an undo that registers a new effect is refused while UNLOADING, so nothing leaks', async () => {
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
            // upstream cordis only guarded against DISPOSED fibers
            // (assertActive checked uid, not lifecycle state). The pinned
            // fork now refuses it: assertActive also checks the UNLOADING
            // state, so this ctx.effect throws INACTIVE_EFFECT (swallowed
            // into the fiber logger by the unload pass) instead of landing
            // a disposer after the unload snapshot.
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

    expect(leaked).toBe(false) // INACTIVE_EFFECT was raised; nothing executed
    expect(rogue.state).toBe(FiberState.PENDING)
    // No effect was accepted by the INACTIVE fiber: no residue to leak.
    expect(rogue.getEffects().length).toBe(0)

    // Nothing to dispose: the unload snapshot is clean.
    await rogue.dispose()
    expect(leakDisposed).toBe(false)
  })
})
