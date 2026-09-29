// issue #1511, the ts tier: an extern that DECLARES its own compensation,
//
//   extern emission fn put_let(k: Str) -> Int compensate undo_let() = @ts { .. }
//
// is registered wherever a provide method crosses it, and at an activation-body
// `emit`. Before the fix the ts emitter registered none of them, in any
// position, so an abort after the crossing ran nothing the program had declared
// owed. Measured on the base with this fixture: every "runs" test below saw no
// `comp:` line at all.
//
// Every call to such an extern renders through one path (`_declared_call` in
// emit.py): `frame.declared(<call>, ...)` parks the offset with
// `compensationMethod` once the call has returned. The activation-body statement
// registers through the body's own `frame.compensation`, as a site-spelled one.
//
// Every host body records on `hostLog` (`put:<tag>`, `comp:<tag>`), so the order
// the compensations ran in is observed.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Everything, Positions } from './generated/declared_compensate.ts'
import { frameForCtx, hostLog, resetHost } from '../runtime.ts'

beforeEach(() => {
  resetHost()
})

async function activate(plugin: any): Promise<{ ctx: Context; fiber: any; frame: any }> {
  const ctx = new Context()
  const fiber = ctx.plugin(plugin)
  await fiber.await()
  expect(fiber.state).toBe(FiberState.ACTIVE)
  const frame = frameForCtx(fiber.ctx)
  expect(frame, 'the activation Frame must be reachable via its ctx').toBeTruthy()
  return { ctx, fiber, frame }
}

function compensations(): string[] {
  return hostLog.filter((line) => line.startsWith('comp:')).map((line) => line.slice(5))
}

// one method per position; each crosses `put_<tag>` once
const POSITIONS: Array<[string, string]> = [
  ['statement', 'statement'],
  ['binding', 'let'],
  ['returned', 'return'],
  ['argument', 'argument'],
  ['ifarm', 'ifarm'],
  ['nested', 'nested'],
]

describe('an abort runs the declared compensation in every position', () => {
  for (const [method, tag] of POSITIONS) {
    it(`${tag}: owed after the call, run by the abort`, async () => {
      const { ctx, fiber, frame } = await activate(Positions)
      ;(ctx as any).positions[method]()
      expect(hostLog).toContain(`put:${tag}`)
      expect(compensations(), 'nothing failed yet: owed, not run').toEqual([])
      frame.abort()
      await fiber.dispose()
      expect(compensations()).toEqual([tag])
      expect(frame.report().clean).toBe(true)
    })
  }
})

describe('a clean unload discharges the declared compensation', () => {
  for (const [method, tag] of POSITIONS) {
    it(`${tag}: registered, then discharged by the commit`, async () => {
      const { ctx, fiber, frame } = await activate(Positions)
      ;(ctx as any).positions[method]()
      const owed = frame.descriptors().filter((d: any) => d.entry === 'compensation')
      expect(owed.map((d: any) => d.call.method)).toEqual([`undo_${tag}`])
      await fiber.dispose()
      expect(compensations(), 'a commit must not run the offset').toEqual([])
      expect(frame.report().clean).toBe(true)
    })
  }
})

describe('every position in one call, newest first', () => {
  it('runs the call\'s compensations newest first, then the activation\'s', async () => {
    const { ctx, fiber, frame } = await activate(Everything)
    ;(ctx as any).all.run()
    frame.abort()
    await fiber.dispose()
    // one LIFO order, whichever way each was registered: the site-spelled
    // `undo_site` in its place, the activation body's `undo_act` last
    expect(compensations()).toEqual([
      'return', 'nested', 'site', 'ifarm', 'argument', 'let', 'statement', 'act',
    ])
  })

  it('the activation body\'s declared compensation runs on an abort alone', async () => {
    const { fiber, frame } = await activate(Everything)
    // no call: only the activation body crossed `put_act`
    frame.abort()
    await fiber.dispose()
    expect(compensations()).toEqual(['act'])
  })

  it('and a clean unload discharges it', async () => {
    const { fiber } = await activate(Everything)
    await fiber.dispose()
    expect(compensations()).toEqual([])
  })
})

describe('a crossing that throws registers nothing', () => {
  it('the earlier crossing\'s compensation waits for the verdict and runs once', async () => {
    const { ctx, fiber, frame } = await activate(Positions)
    expect(() => (ctx as any).positions.boom()).toThrow('put:boom failed')
    expect(compensations()).toEqual([])
    const owed = frame.descriptors().filter((d: any) => d.entry === 'compensation')
    expect(owed.map((d: any) => d.call.method)).toEqual(['undo_first'])
    frame.abort()
    await fiber.dispose()
    expect(compensations()).toEqual(['first'])
  })
})
