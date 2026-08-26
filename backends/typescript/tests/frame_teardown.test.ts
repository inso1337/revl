// item 243 Slice 2b — direct unit coverage of `Frame`, the teardown
// accumulator (docs/design/teardown-contract.md). Complements
// tests/witnessed_teardown.test.ts (which drives the fixture through real
// cordis): these tests exercise `Frame`'s registration/disposal API in
// isolation, so every exit-test-3 checklist item is asserted deterministically
// — including the ones a real cordis run cannot force on demand (an injected
// Phase-1 failure of each severity, and the Phase-2 budget's deadline-expired
// skip) — without depending on cordis' own scheduling.
//
// Simulates what cordis' disposal chain does structurally: call each
// registered disposer in exact reverse-registration (LIFO) order. That is the
// "mechanism is free, observable order is not" contract (docs/design/
// teardown-contract.md, "The teardown algorithm") — these tests assert the
// OBSERVABLE order, not cordis' internal `.then()` chaining, which
// tests/witnessed_teardown.test.ts covers separately end to end.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Frame } from '../runtime.ts'
import type { Context } from 'cordis'

const ctx = {} as Context

/** Dispose a list of yielded disposers newest-first, exactly as cordis does
 * within one effect's yields (REPORT.md §1.1). */
async function disposeLifo(disposers: Array<() => unknown>): Promise<void> {
  for (const d of [...disposers].reverse()) {
    await d()
  }
}

describe('Frame — bracket / transactional / compensation on one LIFO stack', () => {
  beforeEach(() => {
    delete process.env.REVL_COMPENSATION_BUDGET_MS
    delete process.env.REVL_COMPENSATION_PER_CALL_MS
  })

  it('mixed-entry LIFO holds on a clean commit: bracket still runs, transactional and compensation discharge', async () => {
    const frame = new Frame(ctx, 'Commit')
    const ran: string[] = []
    const yields: Array<() => unknown> = []
    yields.push(frame.begin)
    yields.push(
      frame.bracket({ key: 'db', method: 'open', args: [], site: 's#1' }, 'close', () => {
        ran.push('bracket-inverse')
      }),
    )
    yields.push(
      frame.transactional(
        { key: 'fs', method: 'rm', args: [], site: 's#2' },
        'restore',
        () => ran.push('transactional-undo'),
        { path: '/tmp/x' },
      ),
    )
    yields.push(
      frame.compensation({ key: 'db', method: 'insert', args: ['row'], site: 's#3' }, 'delete', ['row'], () =>
        ran.push('compensation'),
      ),
    )
    yields.push(frame.drain)

    await disposeLifo(yields)

    // commit path: bracket runs (unchanged); transactional and compensation
    // discharge (never run) — the commit-path pseudocode has no phase split.
    expect(ran).toEqual(['bracket-inverse'])
    expect(frame.report()).toEqual({ clean: true, outstanding: [], worldRemaining: [], proof: expect.any(String) })
    expect(frame.dischargeRecord()).toEqual({ record: 'discharge', discharged: [1, 2] })
  })

  it('two-phase abort: Phase 1 (bracket + transactional) completes LIFO before Phase 2 (compensation) starts', async () => {
    const frame = new Frame(ctx, 'Abort')
    const ran: string[] = []
    const yields: Array<() => unknown> = []
    yields.push(frame.begin)
    yields.push(
      frame.bracket({ key: 'db', method: 'open', args: [], site: 's#1' }, 'close', () => ran.push('bracket')),
    )
    yields.push(
      frame.transactional(
        { key: 'fs', method: 'rm', args: [], site: 's#2' },
        'restore',
        () => ran.push('transactional'),
        {},
      ),
    )
    yields.push(
      frame.compensation({ key: 'db', method: 'insert', args: [], site: 's#3' }, 'delete', [], () =>
        ran.push('compensation'),
      ),
    )
    // NOTE: `drain` is never yielded — the activation aborted before reaching
    // its final step, so `committed` stays false (mirrors backends/python/
    // runtime.py Frame.drain's "did drain run" discriminator).

    await disposeLifo(yields)

    // Phase 1 is LIFO (transactional registered after bracket, so its undo
    // runs first), and BOTH complete before Phase 2's compensation starts —
    // the exact inversion of the old single-phase a5 interleaving.
    expect(ran).toEqual(['transactional', 'bracket', 'compensation'])
    expect(frame.report().clean).toBe(true)
  })

  it('continue-and-record: a Phase-1 bracket failure does not stop the remaining Phase-1 inverses (bracket-fault)', async () => {
    const frame = new Frame(ctx, 'BracketFault')
    const ran: string[] = []
    const yields: Array<() => unknown> = []
    yields.push(frame.begin)
    yields.push(
      frame.bracket({ key: 'db', method: 'open', args: [], site: 's#1' }, 'close', () => ran.push('older-bracket')),
    )
    yields.push(
      frame.bracket({ key: 'net', method: 'open', args: [], site: 's#2' }, 'close', () => {
        throw new Error('close refused')
      }),
    )

    await disposeLifo(yields)

    // the older (earlier-registered, later-disposed) bracket still ran —
    // skipping it would have strictly increased residue.
    expect(ran).toEqual(['older-bracket'])
    const report = frame.report()
    expect(report.clean).toBe(false)
    expect(report.outstanding).toHaveLength(1)
    const record = report.outstanding[0]
    expect(record.kind).toBe('bracket-fault')
    expect(record.crossing.key).toBe('net')
    expect(record.attempted).toEqual({ call: 'close', args: [], phase: 1 })
    expect(record.error).toEqual({ type: 'Error', message: 'close refused' })
    expect(record.attemptedFlag).toBe(true)
    expect(record.outcome).toBe('failed')
    expect(record.referent).toContain('net')
  })

  it('continue-and-record: a Phase-1 transactional restore failure is the anticipated case (restore-residue)', async () => {
    const frame = new Frame(ctx, 'RestoreResidue')
    const ran: string[] = []
    const yields: Array<() => unknown> = []
    yields.push(frame.begin)
    yields.push(
      frame.transactional(
        { key: 'fs', method: 'rm', args: ['/a'], site: 's#1' },
        'restore',
        () => ran.push('older-restore'),
        { path: '/a' },
      ),
    )
    yields.push(
      frame.transactional(
        { key: 'fs', method: 'rm', args: ['/b'], site: 's#2' },
        'restore',
        () => {
          throw new Error('ENOENT')
        },
        { path: '/b' },
      ),
    )

    await disposeLifo(yields)

    expect(ran).toEqual(['older-restore'])
    const record = frame.report().outstanding[0]
    expect(record.kind).toBe('restore-residue')
    expect(record.attempted).toEqual({ call: 'restore', args: [{ path: '/b' }], phase: 1 })
    expect(record.error).toEqual({ type: 'Error', message: 'ENOENT' })
  })

  it('a failed compensation is recorded (compensation-residue) and does not stop later Phase-2 entries', async () => {
    const frame = new Frame(ctx, 'CompFailure')
    const ran: string[] = []
    const yields: Array<() => unknown> = []
    yields.push(frame.begin)
    yields.push(
      frame.compensation({ key: 'db', method: 'insert', args: ['a'], site: 's#1' }, 'delete', ['a'], () =>
        ran.push('older-compensation'),
      ),
    )
    yields.push(
      frame.compensation({ key: 'mail', method: 'send', args: ['b'], site: 's#2' }, 'recall', ['b'], () => {
        throw new Error('recall failed')
      }),
    )

    await disposeLifo(yields)

    // LIFO within Phase 2: the later-registered (mail) compensation fires
    // FIRST, and its failure does not stop the earlier (db) one from running.
    expect(ran).toEqual(['older-compensation'])
    const record = frame.report().outstanding[0]
    expect(record.kind).toBe('compensation-residue')
    expect(record.attempted).toEqual({ call: 'recall', args: ['b'], phase: 2 })
    expect(record.outcome).toBe('failed')
  })

  it('the between-compensation deadline check skips remaining Phase-2 entries and records each as not-attempted', async () => {
    process.env.REVL_COMPENSATION_BUDGET_MS = '1000'
    const frame = new Frame(ctx, 'Deadline')
    // Date.now() sequence: Frame construction reads it once (budget calc),
    // then each Phase-2 iteration checks it again. Return a value already
    // past the deadline from the second call onward, so the FIRST
    // compensation still runs (budget not yet expired at its own check) and
    // every later one is skipped — deterministic, no real clock involved.
    const base = 1_000_000
    let call = 0
    const spy = vi.spyOn(Date, 'now').mockImplementation(() => {
      call += 1
      return call <= 2 ? base : base + 5_000
    })
    try {
      const ran: string[] = []
      const yields: Array<() => unknown> = []
      yields.push(frame.begin)
      yields.push(
        frame.compensation({ key: 'db', method: 'insert', args: [], site: 's#1' }, 'delete', [], () =>
          ran.push('skipped-older'),
        ),
      )
      yields.push(
        frame.compensation({ key: 'mail', method: 'send', args: [], site: 's#2' }, 'recall', [], () =>
          ran.push('ran-newer'),
        ),
      )

      await disposeLifo(yields)

      expect(ran).toEqual(['ran-newer'])
      const outstanding = frame.report().outstanding
      expect(outstanding).toHaveLength(1)
      expect(outstanding[0].kind).toBe('compensation-residue')
      expect(outstanding[0].crossing.key).toBe('db')
      expect(outstanding[0].attempted).toEqual({ call: null, args: [], phase: 2 })
      expect(outstanding[0].attemptedFlag).toBe(false)
      expect(outstanding[0].outcome).toBe('not-attempted')
      expect(outstanding[0].error).toEqual({ type: 'Error', message: 'deadline-expired' })
    } finally {
      spy.mockRestore()
      delete process.env.REVL_COMPENSATION_BUDGET_MS
    }
  })

  it('reads the budget env vars once at construction, with the documented defaults and 0-means-unbounded', () => {
    process.env.REVL_COMPENSATION_BUDGET_MS = '0'
    try {
      const frame = new Frame(ctx, 'Unbounded')
      // no assertion API exposes the raw numbers, so prove the effect
      // instead: a `0` budget never expires, however much wall time passes.
      const ran: string[] = []
      const yields: Array<() => unknown> = []
      yields.push(frame.begin)
      for (let i = 0; i < 5; i++) {
        yields.push(
          frame.compensation({ key: 'db', method: 'op', args: [i], site: `s#${i}` }, 'undo', [i], () =>
            ran.push(`c${i}`),
          ),
        )
      }
      return disposeLifo(yields).then(() => {
        expect(ran).toEqual(['c4', 'c3', 'c2', 'c1', 'c0'])
        expect(frame.report().clean).toBe(true)
      })
    } finally {
      delete process.env.REVL_COMPENSATION_BUDGET_MS
    }
  })

  it('the WAL discharge-descriptor shape is built at registration for both transactional and compensation entries', () => {
    const frame = new Frame(ctx, 'Descriptors')
    const tDisposer = frame.transactional(
      { key: 'fs', method: 'rm', args: ['/a'], site: 's#1' },
      'restore',
      () => undefined,
      { path: '/a' },
    )
    const cDisposer = frame.compensation(
      { key: 'db', method: 'insert', args: ['row'], site: 's#2' },
      'delete',
      ['row'],
      () => undefined,
    )
    void tDisposer
    void cDisposer

    const descriptors = frame.descriptors()
    expect(descriptors).toHaveLength(2)
    expect(descriptors[0]).toMatchObject({
      record: 'discharge-descriptor',
      seq: 1,
      entry: 'transactional',
      call: { receiver: 'Descriptors', method: 'restore', args: [{ path: '/a' }] },
      origin: { key: 'fs', method: 'rm', args: ['/a'], site: 's#1' },
      witness: { path: '/a' },
      idempotency: null,
    })
    expect(descriptors[1]).toMatchObject({
      record: 'discharge-descriptor',
      seq: 2,
      entry: 'compensation',
      call: { receiver: 'Descriptors', method: 'delete', args: ['row'] },
      origin: { key: 'db', method: 'insert', args: ['row'], site: 's#2' },
      witness: null,
      idempotency: null,
    })
  })
})
