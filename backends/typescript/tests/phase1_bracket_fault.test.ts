// Phase-1 continue-and-record on the ts tier (docs/design/teardown-contract.md).
//
// The contract: *"A failed inverse never skips the remaining Phase-1 inverses.
// Skipping strictly increases residue ... catch, record into the merged residue
// schema, continue."* A failed BRACKET inverse carries the contract-grade
// `bracket-fault` severity.
//
// `Frame.bracket` implemented this correctly from the start and
// frame_teardown.test.ts pinned it — against the HAND-BUILT API. The emitter
// never called it: an ordinary bracket emitted a bare `yield () => <undo>`, so
// every EMITTED ts program had the hole the unit test claimed was closed. One
// throw broke cordis' sequential disposal chain and every earlier-registered
// (later-disposed) inverse was starved, with nothing recorded and a clean
// resolve. These tests drive the EMITTED module, which is what a user runs.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import { Clean, Faulting } from './generated/phase1_bracket_fault.ts'
import { frameForCtx, hostLog, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

describe('Phase-1 bracket fault (emitted program)', () => {
  it('a raising bracket inverse does not skip the remaining Phase-1 inverses', async () => {
    const ctx = new Context()
    const fiber = await ctx.plugin(Faulting)
    const frame = frameForCtx(fiber.ctx)!

    const start = hostLog.length
    await fiber.dispose()

    // LIFO to completion: C closes, B's inverse throws, and A STILL closes.
    // Before the fix the log stopped dead at `pool#3(C).close`.
    expect(hostLog.slice(start)).toEqual(['pool#3(C).close', 'pool#1(A).close'])

    // and the failure is recorded, not silent — the verdict is not clean.
    expect(frame).toBeDefined()
    const report = frame.report()
    expect(report.clean).toBe(false)
    expect(report.outstanding).toHaveLength(1)
    const record = report.outstanding[0]
    expect(record.kind).toBe('bracket-fault')
    expect(record.crossing.key).toBe('b')
    expect(record.attempted).toEqual({ call: 'blow', args: [], phase: 1 })
    expect(record.error).toEqual({ type: 'Error', message: 'undo exploded' })
    expect(record.attemptedFlag).toBe(true)
    expect(record.outcome).toBe('failed')
    expect(report.worldRemaining).toHaveLength(1)
  })

  it('the happy path is unchanged and records no residue', async () => {
    const ctx = new Context()
    const fiber = await ctx.plugin(Clean)
    const frame = frameForCtx(fiber.ctx)!

    const start = hostLog.length
    await fiber.dispose()

    expect(hostLog.slice(start)).toEqual(['pool#2(E).close', 'pool#1(D).close'])
    expect(frame).toBeDefined()
    const report = frame.report()
    expect(report.clean).toBe(true)
    expect(report.outstanding).toEqual([])
  })
})
