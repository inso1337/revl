// item 131 — explicit async/await EFFECT composition, driven end to end
// through a real cordis composition. The ts twin of the reference-tier proof
// (backends/python/tests/test_async_effect_composition.py): the LIFO teardown
// ACROSS an in-flight `effect await` acquisition (design §4), which needs ZERO
// runtime change — the same cordis `ctx.effect` generator + disposer LIFO that
// the sync spelling already uses, now with an awaited acquisition inside it.
//
// The suspension source is a req-target `async fn` (`slow_open`) whose provide
// method parks on `Job.run` (the runtime's controllable async host op, exactly
// `JOB_TICKS` turns in flight — see host_semantics.test.ts), so the test can
// dispose the consumer WHILE the acquisition is in flight and observe the
// inertia + LIFO teardown the two-phase abort contract promises.
//
// The fixture is regenerated from async_effect_composition.ir.json by
// scripts/emit-fixtures.ts before vitest collects (see vitest.config.ts).
import { describe, expect, it, beforeEach } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Consumer, PgDatabase } from './generated/async_effect_composition.ts'
import { hostLog, resetHost, JOB_TICKS } from '../runtime.ts'

/** Advance the microtask queue until `pred` holds or `budget` turns elapse. */
async function until(pred: () => boolean, budget = JOB_TICKS * 6): Promise<boolean> {
  for (let i = 0; i < budget; i++) {
    if (pred()) return true
    await Promise.resolve()
  }
  return pred()
}

/** The activation-body markers (the SQL each acquisition/inverse ran), in the
 *  order the Pool host recorded them — the observable teardown order. */
function markers(): string[] {
  const wanted = ['ACQ A', 'ACQ B', 'UNDO A', 'UNDO B']
  return hostLog
    .map((e) => wanted.find((m) => e.includes(`(${m})`)))
    .filter((m): m is string => m !== undefined)
}

beforeEach(() => {
  resetHost()
})

describe('item 131 — LIFO teardown across an in-flight `effect await` acquisition', () => {
  it('B lands (inertia), then teardown runs B before A (LIFO), no residue', async () => {
    const ctx = new Context()
    const db = ctx.plugin(PgDatabase, { url: 'pg://aec' })
    await db.await()

    // start the consumer but DO NOT await it to ACTIVE: its second acquisition
    // (`effect await db.slow_open`) parks in the provider's `async slow_open`
    // on `Job.run("B")`, so awaiting to ACTIVE would let B land first.
    const consumer = ctx.plugin(Consumer)

    const inflight = await until(
      () =>
        hostLog.some((e) => e.includes('job.run B start')) &&
        !hostLog.some((e) => e.includes('job.run B done')),
    )
    expect(inflight).toBe(true) // B's acquisition reached its in-flight window
    expect(markers()).toEqual(['ACQ A']) // A acquired; B has not landed yet

    // withdraw the component WHILE B is in flight; the withdrawal takes effect
    // at the next boundary — B lands (inertia), its inverse registers
    // boundary-atomically, then the two-phase abort replays the stack
    // newest-first (dispose resolves once teardown completes)
    await consumer.dispose()
    await until(() => markers().length === 4)

    // inertia + LIFO across the suspension + no residue: every acquired
    // effect's inverse ran, UNDO B strictly before UNDO A, none left behind
    expect(markers()).toEqual(['ACQ A', 'ACQ B', 'UNDO B', 'UNDO A'])
    expect(consumer.state).toBe(FiberState.DISPOSED)

    await db.dispose()
  })

  it('clean roundtrip: the awaited acquisition lands, and unload reverts it (close after open)', async () => {
    const ctx = new Context()
    const db = ctx.plugin(PgDatabase, { url: 'pg://aec2' })
    await db.await()

    // awaited to ACTIVE: B is allowed to land this time
    const consumer = ctx.plugin(Consumer)
    await consumer.await()
    expect(consumer.state).toBe(FiberState.ACTIVE)
    expect(markers()).toEqual(['ACQ A', 'ACQ B'])

    await consumer.dispose()
    // R1: every inverse runs, newest-first, after both acquisitions landed
    expect(markers()).toEqual(['ACQ A', 'ACQ B', 'UNDO B', 'UNDO A'])
    expect(consumer.state).toBe(FiberState.DISPOSED)

    await db.dispose()
  })
})
