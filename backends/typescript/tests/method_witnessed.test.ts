// item 318 -> 324, THE H1 GATE on the TS tier — a witnessed fs mutation fired
// from a PROVIDE-METHOD, PER TOOL CALL, driven end to end through a real cordis
// composition. The TS mirror of tests/test_provide_method_witnessed.py.
//
// 243/244/Slice-2b proved a witnessed effect in the component ACTIVATION body
// (runs once at load). The real agent use case is a fs mutation that fires from
// a provide-METHOD, per tool call, AFTER activation. This suite proves that
// closed loop:
//
//   * a component provides a service whose method does a witnessed fs mutation;
//   * the method is called PER REQUEST, each call registering a transactional
//     inverse into the component's activation frame
//     (`Frame.transactionalMethod` — parked, disposed by `drain`, NOT adopted
//     as a sibling `ctx.effect`, which on this cordis-style tier would dispose
//     BEFORE the body's `drain` and wrongly revert the deliverable on a CLEAN
//     unload — the soundness hazard item 318 found on py, verified here);
//   * on a clean unload the mutations PERSIST (discharged, the deliverable);
//   * on an ABORT (`frame.abort()` — the seam item 245's commit/abort UX will
//     drive) every per-call mutation REVERTS, residue-free;
//   * the residue is ENUMERABLE: the discharge-descriptors name every crossing,
//     a clean commit writes a discharge record over their seqs, an abort none.
//
// The witnessed extern is a rename-with-a-data-witness stand-in over an
// in-memory "file world" (the hermetic style tests/witnessed_teardown.test.ts
// uses for its box), parameterised by path so each per-call invocation mutates
// a distinct entry — the shape of an agent calling one fs tool repeatedly.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Agent } from './generated/method_witnessed.ts'
import { frameForCtx, resetHost } from '../runtime.ts'

type World = Record<string, string | undefined>

function world(): World {
  return (globalThis as any).__revlFsWorld as World
}

const PATHS = ['/artifact_0.txt', '/artifact_1.txt', '/artifact_2.txt']

beforeEach(() => {
  resetHost()
  const w: World = {}
  PATHS.forEach((p, i) => {
    w[p] = `deliverable ${i}`
  })
  ;(globalThis as any).__revlFsWorld = w
})

/** The witnessed rename ran: original gone, backup present. */
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
// 1. per-tool-call witnessed mutation PERSISTS on a clean unload (commit)
// ---------------------------------------------------------------------------

describe('per-tool-call H1 — persist on clean unload', () => {
  it('each tool call registers one deferred inverse; a clean unload discharges them and the mutations persist', async () => {
    const { ctx, fiber } = await activate()

    // activation did nothing; the frame is empty until a tool call fires
    const frame = frameForCtx(fiber.ctx)!
    expect(frame, 'the activation Frame must be reachable via its ctx').toBeTruthy()
    expect(frame.deferredEntries()).toHaveLength(0)

    // each tool call runs the provide-method, registering ONE transactional
    // inverse into the component's activation frame (per-tool-call H1)
    for (const path of PATHS) {
      ;(ctx as any).ops.touch(path)
      expect(mutated(path), 'the witnessed mutation did not apply on the call').toBe(true)
    }

    const entries = frame.deferredEntries()
    expect(entries).toHaveLength(PATHS.length)
    expect(entries.every((e) => !e.discharged && !e.replayed)).toBe(true)

    await fiber.dispose() // clean unload == implicit commit

    // the deliverable persists on every path; the inverses discharged + GC'd
    for (const path of PATHS) {
      expect(mutated(path), 'clean unload wrongly reverted a per-call mutation').toBe(true)
    }
    for (const e of entries) {
      expect(e.discharged).toBe(true)
      expect(e.replayed).toBe(false)
      expect(e.witness).toBeNull()
      expect(e.undo).toBeNull()
    }
    // no inverse ever ran on the commit path
    expect(frame.report().clean).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 2. per-tool-call witnessed mutation REVERTS on abort, residue-free
// ---------------------------------------------------------------------------

describe('per-tool-call H1 — revert on abort', () => {
  it('frame.abort() before unload replays every per-call inverse, residue-free', async () => {
    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!

    for (const path of PATHS) {
      ;(ctx as any).ops.touch(path)
      expect(mutated(path)).toBe(true)
    }
    const entries = frame.deferredEntries()

    // abort the session's work (item 245's reject drives this seam): the next
    // teardown reverts instead of committing
    frame.abort()
    await fiber.dispose()

    // every per-call mutation reverted, and the teardown left no residue
    for (const path of PATHS) {
      expect(pristine(path), 'abort did not revert a per-call mutation').toBe(true)
    }
    for (const e of entries) {
      expect(e.replayed).toBe(true)
      expect(e.discharged).toBe(false)
      expect(e.witness).toBeNull()
      expect(e.undo).toBeNull()
    }
    const report = frame.report()
    expect(report.clean, `abort left teardown residue: ${JSON.stringify(report.outstanding)}`).toBe(true)
  })

  it('is all-or-nothing across independent per-call mutations (one abort reverts every call, not just the last)', async () => {
    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!
    for (const path of PATHS) (ctx as any).ops.touch(path)

    frame.abort()
    await fiber.dispose()

    // all three, in one abort — the activation frame is the shared accumulator
    expect(PATHS.every((p) => pristine(p))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 3. the residue is ENUMERABLE: the discharge-descriptors name every crossing,
//    a commit writes a discharge over their seqs, an abort writes none.
// ---------------------------------------------------------------------------

describe('per-tool-call H1 — enumerable residue', () => {
  it('every per-call crossing is enumerated the instant it registers, and a clean commit discharges all their seqs', async () => {
    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!
    for (const path of PATHS) (ctx as any).ops.touch(path)

    // one transactional discharge-descriptor per tool call, well before commit
    const descriptors = frame.descriptors()
    expect(descriptors).toHaveLength(PATHS.length)
    expect(descriptors.every((d) => d.entry === 'transactional')).toBe(true)
    expect(descriptors.every((d) => d.call.method === 'unstash')).toBe(true)
    const seqs = descriptors.map((d) => d.seq)

    await fiber.dispose() // clean commit

    // the commit writes ONE discharge record naming every crossing's seq:
    // recover would SKIP them (committed, the mutation is the deliverable)
    const discharge = frame.dischargeRecord()
    expect(discharge, 'clean commit wrote no discharge record').not.toBeNull()
    for (const seq of seqs) expect(discharge!.discharged).toContain(seq)
  })

  it('an aborted teardown enumerates the crossings but writes NO discharge record', async () => {
    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!
    for (const path of PATHS) (ctx as any).ops.touch(path)

    frame.abort()
    await fiber.dispose()

    // the descriptors still enumerate the crossings (residue is enumerable), but
    // no discharge record — the inverses were replayed, not committed
    expect(frame.descriptors()).toHaveLength(PATHS.length)
    expect(frame.dischargeRecord()).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 4. the soundness hazard, pinned: a per-call witnessed effect must NOT be a
//    cordis disposer that fires before `drain`. If it were adopted as a sibling
//    effect, cordis would dispose it BEFORE the body's `drain` on a CLEAN
//    unload — `committed` still false — and revert the deliverable. This test
//    is the direct observation that a clean unload does NOT do that.
// ---------------------------------------------------------------------------

describe('per-tool-call H1 — disposal-ordering hazard', () => {
  it('a clean unload never observes an un-committed frame for the per-call inverse (no premature revert)', async () => {
    const { ctx, fiber } = await activate()
    for (const path of PATHS) (ctx as any).ops.touch(path)

    // clean unload: the ONLY correct outcome is persistence. A sibling-effect
    // implementation would revert here.
    await fiber.dispose()
    expect(PATHS.every((p) => mutated(p)), 'the deliverable was reverted on a clean unload — the sibling-effect hazard').toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 5. item 369: OVERLAPPING per-tool-call ops must replay LIFO on abort.
//    The suites above use DISJOINT paths, so FIFO and LIFO drain are
//    indistinguishable — which is exactly how the bug hid. Two per-call `mv`
//    ops on a shared path expose it: `mv a b ; mv b c ; abort` must land on
//    `a` (LIFO: unmove(c->b) then unmove(b->a)). A FIFO drain replays
//    unmove(b->a) first (a no-op — `b` is absent), then unmove(c->b), leaving
//    the WRONG `b`. `frame.abort()` replays through the deferred-drain, the
//    exact seam item 369 fixed (`deferredList` drained newest-first).
// ---------------------------------------------------------------------------

describe('item 369 — overlapping per-tool-call ops replay LIFO on abort', () => {
  it('mv a b ; mv b c ; abort lands on the ORIGINAL name (not the FIFO intermediate)', async () => {
    const w = world()
    w['/a'] = 'A'

    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!

    ;(ctx as any).ops.mv('/a', '/b')
    ;(ctx as any).ops.mv('/b', '/c')
    expect(w['/c']).toBe('A')

    frame.abort()
    await fiber.dispose()

    expect(w['/a'], 'abort landed on the wrong name (FIFO deferred replay)').toBe('A')
    expect(w['/b']).toBeUndefined()
    expect(w['/c']).toBeUndefined()
    expect(frame.report().clean, 'abort left teardown residue').toBe(true)
  })

  it('a deeper overlapping chain (a->b->c->d) reverts to the original on abort', async () => {
    const w = world()
    w['/a'] = 'DEEP'

    const { ctx, fiber } = await activate()
    const frame = frameForCtx(fiber.ctx)!

    ;(ctx as any).ops.mv('/a', '/b')
    ;(ctx as any).ops.mv('/b', '/c')
    ;(ctx as any).ops.mv('/c', '/d')

    frame.abort()
    await fiber.dispose()

    expect(w['/a']).toBe('DEEP')
    for (const p of ['/b', '/c', '/d']) expect(w[p]).toBeUndefined()
    expect(frame.report().clean).toBe(true)
  })
})
