// Bridge stub tests (docs/interop-bridge.md §3, "Trust model"): what the
// provider side of a seam will and will not dispatch.
//
// revl's claim about a cross-process seam is that it is *enumerable and
// checked* (G8): the operations a `service` declares are exactly the ones that
// cross. The proxy side has always enumerated them; these assert the stub side
// does too — an unknown method is refused with an error reply rather than
// looked up on the provided object.
import { afterEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { serve } from '../bridge.ts'

const dirs: string[] = []
const servers: net.Server[] = []

function socketPath(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'revl_bridge_test_'))
  dirs.push(dir)
  return path.join(dir, 'provider.sock')
}

afterEach(() => {
  for (const server of servers.splice(0)) server.close()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

/** The provided object carries one declared method and one undeclared one. */
function fakeCtx(): { ctx: any; calls: string[] } {
  const calls: string[] = []
  const ctx = {
    db: {
      query: (sql: string) => {
        calls.push(`query:${sql}`)
        return [{ id: 1 }]
      },
      wipe: (table: string) => {
        calls.push(`wipe:${table}`)
        return 'wiped'
      },
    },
  }
  return { ctx, calls }
}

/** One request, one reply — the same newline-delimited JSON a proxy speaks. */
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

describe('bridge stub — the served surface is the declared one', () => {
  it('dispatches a method the service declares', async () => {
    const sock = socketPath()
    const { ctx, calls } = fakeCtx()
    servers.push(await serve(ctx, { db: ['query', 'execute'] }, sock))

    const reply = await call(sock, { key: 'db', method: 'query', args: ['SELECT 1'] })
    expect(reply).toEqual({ ok: true, value: [{ id: 1 }] })
    expect(calls).toEqual(['query:SELECT 1'])
  })

  it('refuses a method the service does not declare, without calling it', async () => {
    const sock = socketPath()
    const { ctx, calls } = fakeCtx()
    servers.push(await serve(ctx, { db: ['query', 'execute'] }, sock))

    const reply = await call(sock, { key: 'db', method: 'wipe', args: ['users'] })
    expect(reply.ok).toBe(false)
    expect(reply.error).toContain('method wipe is not exported for key db')
    expect(reply.error).toContain('execute, query')
    expect(calls).toEqual([]) // refused before dispatch, not after
  })

  it('refuses object-plumbing dressed up as a call', async () => {
    const sock = socketPath()
    const { ctx } = fakeCtx()
    servers.push(await serve(ctx, { db: ['query'] }, sock))

    for (const method of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
      const reply = await call(sock, { key: 'db', method, args: [] })
      expect(reply.ok, `${method} must be refused`).toBe(false)
      expect(reply.error).toContain('is not exported for key db')
    }
  })

  it('still refuses a key this process does not export', async () => {
    const sock = socketPath()
    const { ctx } = fakeCtx()
    servers.push(await serve(ctx, { db: ['query'] }, sock))

    const reply = await call(sock, { key: 'secrets', method: 'query', args: [] })
    expect(reply.ok).toBe(false)
    expect(reply.error).toContain('is not exported by this process')
  })

  it('derives an allowlist for the legacy key-only export form', async () => {
    // `serve(ctx, ['db'], sock)` has no declared list (demo/bridge_ts_adt.mts),
    // so the allowlist comes off the object: weaker, but not unchecked.
    const sock = socketPath()
    const { ctx } = fakeCtx()
    servers.push(await serve(ctx, ['db'], sock))

    expect((await call(sock, { key: 'db', method: 'query', args: ['x'] })).ok).toBe(true)
    const reply = await call(sock, { key: 'db', method: '__proto__', args: [] })
    expect(reply.ok).toBe(false)
    expect(reply.error).toContain('is not exported for key db')
  })
})
