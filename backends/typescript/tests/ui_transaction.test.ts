// issue #1369 (item 522 slice 3), the ts tier: the UI transaction unit. The
// mirror of tests/test_ui_transaction_runtime_1369.py, the py tier's suite.
//
// A provide method that crosses a computer-use verb is one unit, the unit
// `revl.ui_transaction.method_plan` reads. If its call fails, the unit settles
// the entries this call registered, witnessed inverses first and compensations
// second, each newest first and continue-and-record, and the failure
// propagates unchanged. The entries leave the frame's deferred lists first, so
// the clean unload after the failed call does not discharge them and a later
// abort does not run them twice.
//
// Measured on the base with this fixture: a failed call ran no compensation at
// all. Every declared compensation stayed parked on the activation frame, and
// the clean unload after the failed call DISCHARGED them, so the transaction
// stopped half way and kept what it typed; a crossing that threw registered
// nothing, and `uiTransactionRuns` did not exist.
//
// Every host body records what it ran on `hostLog` (`observe`, `locate:<name>`,
// `type_amount`, `compensate:<fn>`), so the order is observed, not inferred.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Agent, AsyncAgent, TailAgent } from './generated/ui_transaction.ts'
import { frameForCtx, hostLog, resetHost } from '../runtime.ts'

const g = globalThis as any

function fail(line: string | undefined): void {
  g.__revlUiFail = line
}

beforeEach(() => {
  resetHost()
  g.__revlUiSeen = {}
  g.__revlUiFail = undefined
  g.__revlUiCompFail = undefined
})

afterEach(() => {
  g.__revlUiFail = undefined
  g.__revlUiCompFail = undefined
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
  return hostLog.filter((line) => line.startsWith('compensate:')).map((line) => line.slice(11))
}

/** The postcondition read that checks `actuate`: the second resolution of
 * `Approve`. Its failure is how the transaction learns the click did not take. */
const POSTCONDITION_UNMET = 'locate:Approve#2'

/** The forward crossings the five-step program makes before that read fails. */
const FORWARD_TO_THE_CHECK = [
  'observe', 'locate:Amount', 'type_amount',
  'observe', 'locate:Memo', 'type_memo',
  'observe', 'locate:Approve', 'actuate',
  'observe', 'locate:Approve',
]

describe('a failed call settles its unit, then the clean unload runs nothing more', () => {
  it('the five-step oracle: steps two and one are compensated, in that order', async () => {
    fail(POSTCONDITION_UNMET)
    const { ctx, fiber, frame } = await activate(Agent)
    expect(() => (ctx as any).ops.run('r')).toThrow('could not confirm locate:Approve')
    // performed at the failure, not at the unload
    expect(hostLog).toEqual([...FORWARD_TO_THE_CHECK,
      'compensate:clear_memo', 'compensate:clear_amount'])
    const [run] = frame.uiTransactionRuns
    expect(run.unit).toBe('ops.run')
    expect(run.failedStep).toBe('locate')
    expect(run.ran.map((e: any) => e.step)).toEqual(['type_memo', 'type_amount'])
    expect(run.ran.map((e: any) => e.compensation)).toEqual(['clear_memo', 'clear_amount'])
    expect(run.crossed.at(-1)).toBe('locate')
    expect(run.crossed).not.toContain('type_note')
    expect(run.crossed).not.toContain('fetch_receipt')
    expect(run.residue).toEqual([])
    // the clean unload after the failed call: nothing is discharged twice and
    // nothing runs again
    await fiber.dispose()
    expect(compensations()).toEqual(['clear_memo', 'clear_amount'])
    expect(frame.report().clean).toBe(true)
  })

  it('the control: a call that completes runs no compensation, and the unload discharges', async () => {
    const { ctx, fiber, frame } = await activate(Agent)
    expect((ctx as any).ops.run('r')).toBe(1n)
    expect(compensations()).toEqual([])
    expect(frame.uiTransactionRuns).toEqual([])
    const owed = frame.descriptors().filter((d: any) => d.entry === 'compensation')
    expect(owed.map((d: any) => d.call.method)).toEqual(['clear_amount', 'clear_memo', 'clear_note'])
    await fiber.dispose()
    expect(compensations()).toEqual([])
  })
})

describe('the rules the run keeps', () => {
  it('the failing crossing\'s own compensation runs first', async () => {
    fail('type_memo')
    const { ctx, fiber, frame } = await activate(Agent)
    expect(() => (ctx as any).ops.run('r')).toThrow('could not confirm type_memo')
    expect(compensations()).toEqual(['clear_memo', 'clear_amount'])
    const [run] = frame.uiTransactionRuns
    expect(run.failedStep).toBe('type_memo')
    expect(run.ran.map((e: any) => e.step)).toEqual(['type_memo', 'type_amount'])
    await fiber.dispose()
    expect(compensations()).toEqual(['clear_memo', 'clear_amount'])
  })

  it('a failure before any compensatable step runs nothing', async () => {
    fail('locate:Amount')
    const { ctx, fiber, frame } = await activate(Agent)
    expect(() => (ctx as any).ops.run('r')).toThrow()
    expect(hostLog).toEqual(['observe', 'locate:Amount'])
    expect(frame.uiTransactionRuns[0].ran).toEqual([])
    await fiber.dispose()
    expect(compensations()).toEqual([])
  })

  it('a failing compensation is residue and the older one still runs', async () => {
    fail(POSTCONDITION_UNMET)
    g.__revlUiCompFail = true
    const { ctx, fiber, frame } = await activate(Agent)
    // the substrate's error propagates, not the compensation's
    expect(() => (ctx as any).ops.run('r')).toThrow('could not confirm locate:Approve')
    expect(compensations()).toEqual(['clear_memo', 'clear_amount'])
    const [run] = frame.uiTransactionRuns
    expect(run.ran.map((e: any) => [e.step, e.failed])).toEqual([
      ['type_memo', true], ['type_amount', false]])
    const [residue] = run.residue
    expect(residue.kind).toBe('compensation-residue')
    expect(residue.outcome).toBe('failed')
    expect(residue.error.message).toBe('the memo field is gone')
    await fiber.dispose()
  })

  it('a later abort does not run a settled unit twice', async () => {
    fail(POSTCONDITION_UNMET)
    const { ctx, fiber, frame } = await activate(Agent)
    expect(() => (ctx as any).ops.run('r')).toThrow()
    expect(compensations()).toEqual(['clear_memo', 'clear_amount'])
    fail(undefined)
    expect((ctx as any).ops.run('r')).toBe(1n)
    frame.abort()
    await fiber.dispose()
    expect(compensations()).toEqual([
      'clear_memo', 'clear_amount',                 // the failed call's unit
      'clear_note', 'clear_memo', 'clear_amount'])  // the abort, LIFO
  })
})

describe('a crossing wherever it is written', () => {
  it('a let-bound crossing registers, and a tail crossing\'s failure is the unit\'s', async () => {
    fail('actuate')
    const { ctx, fiber, frame } = await activate(TailAgent)
    expect(() => (ctx as any).tail.run('r')).toThrow('could not confirm actuate')
    expect(hostLog).toEqual(['observe', 'locate:Amount', 'type_amount',
      'locate:Approve', 'actuate', 'compensate:clear_amount'])
    const [run] = frame.uiTransactionRuns
    expect(run.failedStep).toBe('actuate')
    expect(run.ran.map((e: any) => e.step)).toEqual(['type_amount'])
    await fiber.dispose()
    expect(compensations()).toEqual(['clear_amount'])
  })

  it('an async method and async crossings are one unit', async () => {
    fail('actuate')
    const { ctx, fiber, frame } = await activate(AsyncAgent)
    await expect((ctx as any).async_ops.run('r')).rejects.toThrow('could not confirm actuate')
    expect(hostLog).toEqual(['locate:Amount', 'type_amount', 'locate:Approve',
      'actuate', 'compensate:clear_amount'])
    const [run] = frame.uiTransactionRuns
    // the step is the extern the method crossed
    expect(run.failedStep).toBe('actuate_async')
    expect(run.ran.map((e: any) => e.step)).toEqual(['type_amount'])
    await fiber.dispose()
    expect(compensations()).toEqual(['clear_amount'])
  })
})
