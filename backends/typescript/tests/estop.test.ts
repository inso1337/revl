// The operator E-Stop on the ts tier — roadmap item 443, issue #122.
//
// Item 443 landed the halt on the py reference tier: a latch file, a crossing
// seam that refuses once it is armed, and an in-flight inventory. The five
// non-py tiers kept their cooperative teardown and had NO E-Stop, so a
// placement halt SIGKILLed a node child and reported its residue UNKNOWN.
//
// This suite pins the first half of the node tier honoring the latch:
//
//   * the latch READER reads a malformed latch as HALTED and an absent one as
//     not-halted, byte-for-byte the rule `src/revl/estop.py::read_latch` and
//     `backends/python/runtime.py::_latch_record` already apply, so the two
//     tiers cannot drift on what an operator's armed — or corrupted — latch
//     means;
//   * the ACCEPT seam (`bridge.ts::serve`) stops ACCEPTING new crossings the
//     instant the latch is armed: the request is refused with an error reply
//     and the service method is NOT invoked (slice 1);
//   * the DISPATCH seam (`bridge.ts::makeProxy`) stops DISPATCHING new
//     crossings the instant the latch is armed: an outgoing proxy call is
//     refused before it leaves the process — no child is spawned, nothing
//     crosses the boundary (slice 2). A node process is a consumer as well as a
//     provider, and the py design puts `_estop_check` at every point that
//     dispatches OR accepts a crossing, so both directions honor the latch.
//
// What is deliberately NOT here (issue #122 remainder): the conductor still
// SIGKILLs the node child and reports it as no-seam, because `node` is not yet
// in `src/revl/estop.py::TIERS_WITH_ESTOP` and the runner
// (`placement_runner.ts`) does not yet print its inventory. Flipping that, and
// the idle-process watcher, is a later slice.
import { afterEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { makeProxy, serve } from '../bridge.ts'
import { estopEngaged, latchPath, readLatch } from '../estop.ts'

const dirs: string[] = []
const servers: net.Server[] = []
const closers: Array<() => void> = []
const priorEnv = process.env.REVL_ESTOP_LATCH

function tmpdir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'revl_estop_test_'))
  dirs.push(dir)
  return dir
}

afterEach(() => {
  for (const close of closers.splice(0)) close()
  for (const server of servers.splice(0)) server.close()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
  if (priorEnv === undefined) delete process.env.REVL_ESTOP_LATCH
  else process.env.REVL_ESTOP_LATCH = priorEnv
})

/** One request, one reply — the newline-delimited JSON a proxy speaks. */
function call(sock: string, request: unknown): Promise<any> {
  return new Promise((resolve, reject) => {
    const client = net.connect(sock)
    let buf = ''
    client.on('connect', () => client.write(JSON.stringify(request) + '\n'))
    client.on('data', (chunk) => {
      buf += chunk
      const nl = buf.indexOf('\n')
      if (nl >= 0) {
        client.end()
        resolve(JSON.parse(buf.slice(0, nl)))
      }
    })
    client.on('error', reject)
  })
}

describe('the ts E-Stop latch reader (item 443)', () => {
  it('reads an absent latch as not halted', () => {
    const missing = path.join(tmpdir(), 'nope.estop')
    expect(readLatch(missing)).toBeNull()
    expect(estopEngaged(missing)).toBe(false)
    expect(readLatch(null)).toBeNull()
  })

  it('reads an armed latch as halted and carries its fields', () => {
    const latch = path.join(tmpdir(), 'halt.estop')
    fs.writeFileSync(latch, JSON.stringify({ halted: true, reason: 'runaway loop', operator: 'ops@example' }))
    const record = readLatch(latch)
    expect(record).not.toBeNull()
    expect(record!.reason).toBe('runaway loop')
    expect(record!.operator).toBe('ops@example')
    expect(estopEngaged(latch)).toBe(true)
  })

  it('FAILS CLOSED: a malformed latch still halts', () => {
    // The one failure mode this feature exists to prevent. A corrupted
    // emergency stop reads as HALTED, matching estop.py::read_latch.
    const latch = path.join(tmpdir(), 'garbage.estop')
    fs.writeFileSync(latch, '{ this is not json')
    const record = readLatch(latch)
    expect(record).not.toBeNull()
    expect(record!.halted).toBe(true)
    expect(estopEngaged(latch)).toBe(true)

    // A JSON value that is not an object (a bare array/number) also halts.
    const arr = path.join(tmpdir(), 'arr.estop')
    fs.writeFileSync(arr, '[1, 2, 3]')
    expect(estopEngaged(arr)).toBe(true)
  })

  it('derives the latch path: explicit, then <wal>.estop, then the env', () => {
    expect(latchPath('/a/b.estop')).toBe('/a/b.estop')
    expect(latchPath(null, '/run/session.wal')).toBe('/run/session.wal.estop')
    delete process.env.REVL_ESTOP_LATCH
    expect(latchPath()).toBeNull()
    process.env.REVL_ESTOP_LATCH = '/from/env.estop'
    expect(latchPath()).toBe('/from/env.estop')
  })
})

describe('the ts E-Stop crossing seam (item 443)', () => {
  function fakeCtx(): { ctx: any; calls: string[] } {
    const calls: string[] = []
    const ctx = { db: { query: (sql: string) => { calls.push(sql); return [{ id: 1 }] } } }
    return { ctx, calls }
  }

  it('dispatches while UNARMED, then refuses every crossing once the latch is armed', async () => {
    const dir = tmpdir()
    const sock = path.join(dir, 'provider.sock')
    const latch = path.join(dir, 'halt.estop')
    process.env.REVL_ESTOP_LATCH = latch

    const { ctx, calls } = fakeCtx()
    const server = await serve(ctx, { db: ['query'] }, sock)
    servers.push(server)

    // Before the button: an ordinary crossing lands.
    const before = await call(sock, { key: 'db', method: 'query', args: ['SELECT 1'] })
    expect(before.ok).toBe(true)
    expect(calls).toEqual(['SELECT 1'])

    // The operator hits the button.
    fs.writeFileSync(latch, JSON.stringify({ halted: true, reason: 'runaway loop', operator: 'ops@example' }))

    // After the button: the crossing is REFUSED and the service method is NOT
    // invoked — nothing new crossed the boundary.
    const after = await call(sock, { key: 'db', method: 'query', args: ['SELECT 2'] })
    expect(after.ok).toBe(false)
    expect(String(after.error)).toContain('E-Stop engaged')
    expect(calls).toEqual(['SELECT 1']) // 'SELECT 2' never ran
  })
})

describe('the ts E-Stop DISPATCH seam — outgoing proxy (item 443, slice 2)', () => {
  // `makeProxy`'s seam is a SYNCHRONOUS `execFileSync` child, so a provider in
  // THIS process would deadlock (the child dials an in-process server the
  // blocked event loop cannot answer — a same-process artifact that never
  // arises in production, where the provider is a separate process; the real
  // cross-process round-trip is verified with plain node, as slice 1 was). So
  // this suite proves the DISPATCH GATE without a same-process provider: point
  // the proxy at a socket with no provider and a short deadline, so an
  // attempted dispatch fails FAST on the wire, and contrast the two verdicts.
  //   * UNARMED: the gate is open — the call goes to the wire and comes back a
  //     seam DEADLINE (no provider), never the refusal. Dispatch was attempted.
  //   * ARMED: the gate is shut — the call throws the dispatch-side E-Stop
  //     refusal IMMEDIATELY, never reaching the wire.

  it('attempts dispatch while UNARMED (reaches the wire), then refuses to dispatch once armed', () => {
    const dir = tmpdir()
    const sock = path.join(dir, 'no-provider.sock') // nothing listens here
    const latch = path.join(dir, 'halt.estop')
    process.env.REVL_ESTOP_LATCH = latch

    // A short per-call deadline so the UNARMED attempt fails fast on the wire
    // instead of retrying a missing provider for seconds.
    const { proxy, close } = makeProxy('db', ['query'], sock, 150)
    closers.push(close)

    // Before the button: the gate is OPEN, so the proxy DISPATCHES. With no
    // provider the crossing breaches its deadline — a wire outcome, proving the
    // call left the process. It is NOT the E-Stop refusal.
    let unarmedError: unknown
    try { proxy.query('SELECT 1') } catch (e) { unarmedError = e }
    expect(unarmedError).toBeInstanceOf(Error)
    expect(String((unarmedError as Error).message)).not.toContain('E-Stop')
    expect(String((unarmedError as Error).message)).toMatch(/deadline/)

    // The operator hits the button.
    fs.writeFileSync(latch, JSON.stringify({ halted: true, reason: 'runaway loop', operator: 'ops@example' }))

    // After the button: the gate is SHUT. The proxy throws the dispatch-side
    // E-Stop refusal — before any child seam call is spawned, so nothing new
    // crosses the boundary. (It would also be instant rather than wait out the
    // deadline, since it never reaches the wire.)
    expect(() => proxy.query('SELECT 2')).toThrowError(/E-Stop engaged.*refuses to dispatch/)
  })
})
