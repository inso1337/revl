// Pool / Job / trace semantics — the host runtime itself, not the emitted code.
//
// The rules these assert are shared by all four tiers and written down once, in
// backends/python/runtime.py under `.. _pool-job-semantics:`.  The python suite
// (backends/python/tests/test_host_semantics.py) asserts the same behaviours
// against the same contract; keep the two in step.
import { beforeEach, describe, expect, it } from 'vitest'
import {
  JOB_TICKS,
  JobCancelledError,
  MapHandle,
  hostLog,
  host,
  onHostEvent,
  pendingJobs,
  resetHost,
  resolvedConfig,
} from '../runtime.ts'

beforeEach(() => resetHost())

describe('Pool — real bounded capacity', () => {
  it('starts with `size` idle connections', () => {
    const pool = host.Pool.open('pg://x', 3)
    expect([pool.capacity(), pool.inUse(), pool.available()]).toEqual([3, 0, 3])
    expect(hostLog).toEqual(['pool#1(pg://x).open size=3'])
  })

  it('hands out the lowest idle connection, deterministically', () => {
    const pool = host.Pool.open('pg://x', 3)
    expect([pool.acquire(), pool.acquire(), pool.acquire()]).toEqual([1, 2, 3])
    pool.release(2)
    expect(pool.acquire()).toBe(2)
  })

  it('traces an explicit acquire/release with the accounting', () => {
    const pool = host.Pool.open('pg://x', 2)
    const conn = pool.acquire()
    pool.release(conn)
    expect(hostLog.slice(1)).toEqual([
      'pool#1(pg://x).acquire conn=1 1/2',
      'pool#1(pg://x).release conn=1 0/2',
    ])
  })

  it('refuses to hand out more connections than it has', () => {
    const pool = host.Pool.open('pg://x', 2)
    pool.acquire()
    pool.acquire()
    expect(() => pool.acquire()).toThrow('exhausted (size=2, in_use=2)')
    // statements borrow a connection too, so they refuse as well
    expect(() => pool.query('SELECT 1')).toThrow('query exhausted')
    expect(() => pool.execute('INSERT')).toThrow('execute exhausted')
  })

  it('borrows silently for a statement, so existing traces are unchanged', () => {
    const pool = host.Pool.open('pg://x', 1)
    expect(pool.query('SELECT 1')).toEqual([])
    expect(pool.execute('INSERT')).toBe(1)
    expect([pool.inUse(), pool.available()]).toEqual([0, 1])
    expect(hostLog).toEqual([
      'pool#1(pg://x).open size=1',
      'pool#1(pg://x).query(SELECT 1)',
      'pool#1(pg://x).execute(INSERT)',
    ])
  })

  it('refuses to release a connection that is not checked out', () => {
    const pool = host.Pool.open('pg://x', 2)
    expect(() => pool.release(1)).toThrow('is not checked out')
    const conn = pool.acquire()
    pool.release(conn)
    expect(() => pool.release(conn)).toThrow('is not checked out')
  })

  it('close releases everything and makes every later use an error', () => {
    const pool = host.Pool.open('pg://x', 2)
    pool.acquire()
    pool.acquire()
    pool.close()
    expect([pool.capacity(), pool.inUse(), pool.available()]).toEqual([0, 0, 0])
    expect(() => pool.query('SELECT 1')).toThrow('after close')
    expect(() => pool.execute('INSERT')).toThrow('after close')
    expect(() => pool.acquire()).toThrow('after close')
    expect(() => pool.release(1)).toThrow('after close')
    expect(() => pool.close()).toThrow('after close')
  })

  it('refuses a size below one', () => {
    expect(() => host.Pool.open('pg://x', 0)).toThrow('pool size must be an integer >= 1')
    expect(() => host.Pool.open('pg://x', -1)).toThrow('pool size must be an integer >= 1')
    expect(() => host.Pool.open('pg://x', 1.5)).toThrow('pool size must be an integer >= 1')
  })

  it('keeps the A8 refusal hook ahead of the size check', () => {
    expect(() => host.Pool.open('boom://nope', 0)).toThrow('refused to open')
    expect(hostLog).toEqual(['pool.open refused boom://nope'])
  })

  it('holds in_use + available == capacity through a workload', () => {
    const pool = host.Pool.open('pg://x', 4)
    const held = [pool.acquire(), pool.acquire()]
    pool.query('SELECT 1')
    expect(pool.inUse() + pool.available()).toBe(pool.capacity())
    for (const conn of held) pool.release(conn)
    expect(pool.inUse() + pool.available()).toBe(4)
  })
})

describe('Job — real, cancellable async', () => {
  it('completes after exactly JOB_TICKS turns and is in flight until then', async () => {
    expect(JOB_TICKS).toBe(5)
    const job = host.Job.run('migrations')
    expect(job.state()).toBe('pending')
    expect(pendingJobs()).toBe(1)
    expect(hostLog).toEqual(['job.run migrations start'])

    const settled = Promise.resolve(job)
    for (let i = 0; i < JOB_TICKS; i++) {
      expect(job.state()).toBe('pending')
      await Promise.resolve()
    }
    expect(await settled).toBe('migrations')
    expect(job.state()).toBe('done')
    expect(pendingJobs()).toBe(0)
    expect(hostLog).toEqual(['job.run migrations start', 'job.run migrations done'])
  })

  it('cancelling a pending job makes the await fail', async () => {
    const job = host.Job.run('settle')
    expect(job.cancel()).toBe(true)
    expect(job.cancel()).toBe(false) // idempotent
    expect(job.state()).toBe('cancelled')
    await expect(Promise.resolve(job)).rejects.toThrow(JobCancelledError)
    await expect(Promise.resolve(job)).rejects.toThrow('job "settle" cancelled')
    expect(hostLog).toEqual(['job.run settle start', 'job.run settle cancelled'])
  })

  it('cancelling mid-flight stops the job before it lands', async () => {
    const job = host.Job.run('warmup')
    const settled = Promise.resolve(job)
    await Promise.resolve()
    await Promise.resolve()
    expect(job.remaining).toBeLessThan(JOB_TICKS) // work actually progressed
    job.cancel()
    await expect(settled).rejects.toThrow('cancelled')
    expect(hostLog).not.toContain('job.run warmup done')
    expect(hostLog).toContain('job.run warmup cancelled')
  })

  it('cancelling a finished job is a no-op', async () => {
    const job = host.Job.run('done-already')
    expect(await Promise.resolve(job)).toBe('done-already')
    expect(job.cancel()).toBe(false)
    expect(job.state()).toBe('done')
  })

  it('awaiting a finished job again does not re-record', async () => {
    const job = host.Job.run('once')
    expect(await Promise.resolve(job)).toBe('once')
    expect(await Promise.resolve(job)).toBe('once')
    expect(hostLog.filter((entry) => entry === 'job.run once done')).toHaveLength(1)
  })

  it('counts abandoned jobs as residue', () => {
    host.Job.run('abandoned')
    expect(pendingJobs()).toBe(1)
    expect(host.Job.pending()).toBe(1)
  })
})

describe('tracing — multi-observer', () => {
  it('lets two observers coexist and unsubscribe independently', () => {
    const first: string[] = []
    const second: string[] = []
    const offFirst = onHostEvent((entry) => first.push(entry))
    const offSecond = onHostEvent((entry) => second.push(entry))
    try {
      new MapHandle().drop()
      expect(first).toEqual(second)
      expect(first).toHaveLength(2)
      offSecond()
      new MapHandle().drop()
      expect(first).toHaveLength(4)
      expect(second).toHaveLength(2)
    } finally {
      offFirst()
      offSecond()
    }
  })
})

describe('resolved config in the trace', () => {
  it('records what a component actually ran with, after defaults', () => {
    const config = host.applyConfigDefaults('PgDatabase', { url: 'pg://x' }, {
      url: { required: true },
      pool_size: { default: 10 },
    })
    expect(config).toEqual({ url: 'pg://x', pool_size: 10 })
    expect(hostLog).toEqual([
      'PgDatabase.config {pool_size=10, url="pg://x"} [defaults: pool_size]',
    ])
    expect(resolvedConfig.get('PgDatabase')).toEqual(config)
  })

  it('omits the defaults suffix when the host supplied everything', () => {
    host.applyConfigDefaults('Db', { url: 'pg://x' }, { url: { required: true } })
    expect(hostLog).toEqual(['Db.config {url="pg://x"}'])
  })
})
