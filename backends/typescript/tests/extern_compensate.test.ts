// Issue #1592: an emission extern that DECLARES its own `compensate` (item 254)
// registers that compensation at every site it is emitted from, on the ts tier,
// with the py reference's semantics: registered after the emission fires,
// DISCHARGED on a clean unload (commit), RUN in Phase 2 on an abort.
//
// Before the fix the ts emitter rendered only the forward call at every site,
// so an abort left `put ...` in the host trace with no `restore`.
//
// `put_row` and `restore_row` record onto the shared `hostLog`
// (fixtures/_gen_extern_compensate.py), so the test observes which calls ran.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Agent, Beat, ExternAbort, ExternOk, SiteAbort } from './generated/extern_compensate.ts'
import { Clock, frameForCtx, hostLog, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

/** The extern calls in the host trace, without the clock's own entries. */
function calls(): string[] {
  return hostLog.filter((line) => /^(put|note) /.test(line) || line === 'restore')
}

async function activate(plugin: any): Promise<{ ctx: Context; fiber: any }> {
  const ctx = new Context()
  const fiber = ctx.plugin(plugin)
  await fiber.await()
  expect(fiber.state).toBe(FiberState.ACTIVE)
  return { ctx, fiber }
}

async function failedActivation(plugin: any): Promise<void> {
  const ctx = new Context()
  const fiber = ctx.plugin(plugin)
  await fiber.await().catch(() => {})
  await new Promise((resolve) => setTimeout(resolve, 20))
}

describe('the site-spelled control', () => {
  it('runs on abort, before and after the fix', async () => {
    await failedActivation(SiteAbort)
    expect(calls()).toEqual(['note s', 'restore'])
  })
})

describe('activation-body site', () => {
  it('discharges the declared compensation on a clean unload', async () => {
    const { fiber } = await activate(ExternOk)
    const frame = frameForCtx(fiber.ctx)!
    expect(frame.descriptors().some((d) => d.entry === 'compensation' && d.call.method === 'restore_row')).toBe(true)
    await fiber.dispose()
    expect(calls()).toEqual(['put y'])
  })

  it('runs the declared compensation when the activation aborts', async () => {
    await failedActivation(ExternAbort)
    expect(calls()).toEqual(['put y', 'restore'])
  })
})

describe('provide-method site', () => {
  it('discharges on a clean unload', async () => {
    const { ctx, fiber } = await activate(Agent)
    ;(ctx as any).ops.run('m')
    await fiber.dispose()
    expect(calls()).toEqual(['put m'])
  })

  it('runs on abort', async () => {
    const { ctx, fiber } = await activate(Agent)
    ;(ctx as any).ops.run('m')
    frameForCtx(fiber.ctx)!.abort()
    await fiber.dispose()
    expect(calls()).toEqual(['put m', 'restore'])
  })
})

describe('timer firing site', () => {
  it('discharges every firing on a clean unload', async () => {
    const { fiber } = await activate(Beat)
    Clock.advance(25_000) // fires at 10s and 20s
    expect(calls()).toEqual(['put x', 'put x'])
    await fiber.dispose()
    expect(calls()).toEqual(['put x', 'put x'])
  })

  it('runs one compensation per firing on abort', async () => {
    const { fiber } = await activate(Beat)
    Clock.advance(25_000)
    const frame = frameForCtx(fiber.ctx)!
    expect(frame.descriptors().filter((d) => d.entry === 'compensation').length).toBe(2)
    frame.abort()
    await fiber.dispose()
    expect(calls()).toEqual(['put x', 'put x', 'restore', 'restore'])
  })
})
