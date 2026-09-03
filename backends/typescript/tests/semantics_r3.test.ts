// R3 — withdrawal ordering (docs/backend-ir.md §Required semantics), run against
// an emitted fixture whose undo uses `req`.
//
// Separate from `semantics.test.ts` on purpose (issue #223): this fixture and
// the `golden/user_cache.ts` module that file uses both provide `db`, through
// competing augmentations of cordis' global `Context`. Importing both into one
// file makes `ctx.db` mean two things at once (TS2717). See the note there.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import { Migrator, PgDatabase as R3Provider } from './generated/r3_migrator.ts'
import { hostLog, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

describe('R3 — withdrawal ordering', () => {
  it('dependents fully deactivate before the provider reverts its own effects, and undos may use req', async () => {
    const ctx = new Context()
    const db = await ctx.plugin(R3Provider, { url: 'pg://r3' })
    await ctx.plugin(Migrator)
    expect(hostLog).toContain('pool#1(pg://r3).execute(SELECT pg_advisory_lock(42))')

    const start = hostLog.length
    await db.dispose()

    // The Migrator's undo calls `req db` (committed view) — it must observe a
    // still-functional provider: unlock strictly before pool.close.
    expect(hostLog.slice(start)).toEqual([
      'pool#1(pg://r3).execute(SELECT pg_advisory_unlock(42))',
      'pool#1(pg://r3).close',
    ])
  })

  it('a dependent can call its required service during its own direct unload', async () => {
    const ctx = new Context()
    const db = await ctx.plugin(R3Provider, { url: 'pg://r3b' })
    const mig = await ctx.plugin(Migrator)

    const start = hostLog.length
    await mig.dispose() // provider stays up; committed view must still resolve
    expect(hostLog.slice(start)).toEqual([
      'pool#1(pg://r3b).execute(SELECT pg_advisory_unlock(42))',
    ])
    await db.dispose()
  })
})
