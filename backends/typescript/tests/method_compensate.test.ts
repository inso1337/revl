// item 247 (method-body compensate remainder) — the method-body-compensation soundness fix on the TS tier, the
// mirror of tests/test_provide_method_compensate.py. A `emit ... compensate ...`
// fired from a PROVIDE-METHOD, PER TOOL CALL, driven end to end through a real
// cordis composition.
//
// Item 247 made an activation-body `emit ... compensate ...` a first-class
// COMPENSATION on the frame (`Frame.compensation`): abort-only, discharged on a
// clean commit, drained in PHASE 2 after every proof inverse, guarded. But the
// METHOD-body site was left on the PLACEHOLDER lowering: a bare
// `ctx.effect(() => { <emit>; return () => <offset> })` bracket. A bare bracket
// is disposed by cordis BEFORE the body `drain`, so it fires the offset on a
// CLEAN unload (destroying the deliverable), interleaves with the proof
// inverses, and is unguarded.
//
// This suite proves the method-body site now routes through
// `Frame.compensationMethod` — the compensation analog of item 318/324's
// `transactionalMethod` — so a per-tool-call `emit ... compensate ...`:
//
//   * DISCHARGES on a clean unload (commit): the offset never runs, the emission
//     is the deliverable and survives;
//   * FIRES on `frame.abort()` + unload, in PHASE 2, strictly after the method's
//     transactional proof inverse, guarded and residue-collected.
//
// The method body carries BOTH entry kinds in one call (a witnessed `stash` and
// the emit/compensate), and both append to the shared `hostLog`, so the phase
// ordering ('unstash' before 'compensate:<msg>') is OBSERVED, not inferred.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Agent } from './generated/method_compensate.ts'
import { frameForCtx, resetHost, hostLog } from '../runtime.ts'

type World = Record<string, string | undefined>

function world(): World {
  return (globalThis as any).__revlFsWorld as World
}

const TARGET = '/artifact.txt'

beforeEach(() => {
  resetHost()
  ;(globalThis as any).__revlFsWorld = { [TARGET]: 'deliverable' }
})

/** The witnessed stash ran: original gone, backup present. */
function mutated(path: string): boolean {
  const w = world()
  return w[path] === undefined && w[path + '.bak'] !== undefined
}

/** The world is as it started: original present, no backup residue. */
function pristine(path: string): boolean {
  const w = world()
  return w[path] !== undefined && w[path + '.bak'] === undefined
}

async function activate(): Promise<{ ctx: Context; fiber: any }> {
  const ctx = new Context()
  const fiber = ctx.plugin(Agent)
  await fiber.await()
  expect(fiber.state).toBe(FiberState.ACTIVE)
  return { ctx, fiber }
}

// ---------------------------------------------------------------------------
// 1. clean unload (commit): the compensation DISCHARGES — the offset never runs,
//    the emission is the deliverable and survives. (RED under the placeholder
//    bracket lowering, which fires the offset on clean teardown.)
// ---------------------------------------------------------------------------

describe('method-body compensation — discharge on clean unload', () => {
  it('the offset never runs on a clean commit; the deliverable persists', async () => {
    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!
    expect(frame, 'the activation Frame must be reachable via its ctx').toBeTruthy()

    ;(ctx as any).ops.run(TARGET, 'go')
    expect(mutated(TARGET), 'the witnessed mutation did not apply on the call').toBe(true)

    // one compensation descriptor enumerated the instant it registered
    const descriptors = frame.descriptors()
    expect(descriptors.some((d) => d.entry === 'compensation' && d.call.method === 'offset')).toBe(true)

    await fiber.dispose() // clean unload == implicit commit

    // DISCHARGED: the offset never ran, so the log has NO 'compensate' entry.
    expect(hostLog).not.toContain('compensate:go')
    expect(mutated(TARGET), 'clean unload wrongly reverted the deliverable').toBe(true)
    expect(frame.report().clean).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 2. abort: the compensation FIRES in PHASE 2, strictly after the method's
//    transactional proof inverse, and the witnessed mutation reverts.
// ---------------------------------------------------------------------------

describe('method-body compensation — fire in Phase 2 on abort', () => {
  it('the offset runs after the proof inverse; the witnessed mutation reverts', async () => {
    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!

    ;(ctx as any).ops.run(TARGET, 'go')
    expect(mutated(TARGET)).toBe(true)

    frame.abort() // item 245's reject seam
    await fiber.dispose()

    // the phase-order proof: the transactional inverse's 'unstash' precedes the
    // compensation's 'compensate:go' — Phase 1 completed before Phase 2 started.
    expect(hostLog).toEqual(['unstash', 'compensate:go'])
    expect(pristine(TARGET), 'abort did not revert the witnessed mutation').toBe(true)
    expect(frame.report().clean).toBe(true)
    // an abort writes no discharge record (the inverses replayed, not committed)
    expect(frame.dischargeRecord()).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 3. a FAILING compensation: continue-and-record — the abort still succeeds and
//    the proof inverse still ran to completion (guarded, best-effort Phase 2).
// ---------------------------------------------------------------------------

describe('method-body compensation — a failing offset is best-effort', () => {
  it('lands as residue; the proof inverse is unaffected and the abort still succeeds', async () => {
    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!

    ;(ctx as any).ops.run_fails(TARGET, 'go')

    frame.abort()
    await fiber.dispose() // must not throw

    // the proof inverse ran BEFORE the failing offset and is unaffected by its
    // throw; the offset was attempted (its line landed) then recorded.
    expect(hostLog).toEqual(['unstash', 'compensate:go'])
    expect(pristine(TARGET), 'the guarded offset failure corrupted the abort').toBe(true)

    const report = frame.report()
    expect(report.clean).toBe(false)
    expect(report.outstanding.some((r) => r.kind === 'compensation-residue')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 4. the soundness hazard, pinned: a method-body compensation must NOT be a
//    cordis bracket disposer that fires before `drain`. A bracket would fire
//    the offset on a CLEAN unload; this is the direct observation it does not.
// ---------------------------------------------------------------------------

describe('method-body compensation — disposal-ordering hazard', () => {
  it('a clean unload never fires the offset (no premature compensation)', async () => {
    const { ctx, fiber } = await activate()
    ;(ctx as any).ops.run(TARGET, 'go')

    await fiber.dispose()
    // a bracket-disposer implementation would have run 'compensate:go' here.
    expect(hostLog, 'the offset fired on a clean unload — the bracket-disposer hazard').not.toContain('compensate:go')
    expect(mutated(TARGET)).toBe(true)
  })
})
