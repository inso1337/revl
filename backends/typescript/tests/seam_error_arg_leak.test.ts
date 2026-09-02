// The seam ERROR reply must not carry the caller's argument values back
// (roadmap item 421 F5), on this tier as on python's.
//
// `serve`'s dispatch caught a provider-side throw and replied with
// `String(error)` verbatim. Host error text quotes the value that failed:
// a map miss, a validation message, a `JSON.parse` complaint. So a failing
// call handed the consumer back the very argument it was called with, across
// a trust boundary. The checker admits the forward crossing into a declared
// `Secret[T]` receiver and refuses the reverse one; the error channel was
// performing the refused reverse crossing unanalysed.
//
// Each test asserts BOTH halves: the canary absent AND the marker present, so
// none of them can pass because the reply was empty. The last two are the
// false-positive half: a diagnostic that quotes no argument is unchanged, and
// a record's field names survive, so the reply stays worth reading.
import { afterEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { REDACTED_ARG, seamFailure, serve } from '../bridge.ts'

const CANARY = 'SEKRIT-CANARY-421-F5F6'

const dirs: string[] = []
const servers: net.Server[] = []

function socketPath(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'revl_seam_leak_'))
  dirs.push(dir)
  return path.join(dir, 'provider.sock')
}

afterEach(() => {
  for (const server of servers.splice(0)) server.close()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

/** One request, one reply, over a real socket. */
function callOnce(sock: string, request: unknown): Promise<any> {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(sock, () => {
      client.write(JSON.stringify(request) + '\n')
    })
    let buf = ''
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

/** A provider whose failure quotes its argument, with no interpolation of the
 *  author's own beyond the lookup itself. */
async function failingSeam(): Promise<string> {
  const store = new Map<string, string>()
  const ctx: any = {
    store: {
      lookup(token: string) {
        if (!store.has(token)) throw new Error(`no entry for ${token}`)
        return store.get(token)
      },
    },
  }
  const sock = socketPath()
  servers.push(await serve(ctx, { store: ['lookup'] }, sock))
  return sock
}

describe('seamFailure, the unit', () => {
  it('scrubs the caller\'s own argument out of host error text', () => {
    const text = seamFailure(new Error(`no entry for ${CANARY}`), [CANARY])
    expect(text).not.toContain(CANARY)
    expect(text).toContain(REDACTED_ARG)
  })

  it('keeps the failure worth reading', () => {
    const text = seamFailure(new TypeError(`bad token ${CANARY} at row 3`), [CANARY])
    expect(text).toContain('TypeError')
    expect(text).toContain('bad token')
    expect(text).toContain('at row 3')
    expect(text).not.toContain(CANARY)
  })

  it('finds an argument nested in a record or a list', () => {
    const nested = seamFailure(new Error(`rejected ${CANARY}`), [{ token: CANARY }])
    expect(nested).not.toContain(CANARY)
    expect(nested).toContain(REDACTED_ARG)
    const listed = seamFailure(new Error(`rejected ${CANARY}`), [[['a', CANARY]]])
    expect(listed).not.toContain(CANARY)
    expect(listed).toContain(REDACTED_ARG)
  })

  it('scrubs a numeric argument', () => {
    const text = seamFailure(new Error('account 8675309 is closed'), [8675309])
    expect(text).not.toContain('8675309')
    expect(text).toContain(REDACTED_ARG)
  })

  it('leaves a diagnostic that quotes no argument alone', () => {
    expect(seamFailure(new Error('pool exhausted'), [CANARY]))
      .toBe('Error: pool exhausted')
  })

  it('does not erase a record\'s field names', () => {
    const text = seamFailure(new Error('field token is malformed'), [{ token: CANARY }])
    expect(text).toContain('field token is malformed')
  })
})

describe('the seam reply, end to end', () => {
  it('does not return the failing call\'s argument to the consumer', async () => {
    const sock = await failingSeam()
    const reply = await callOnce(sock, {
      key: 'store', method: 'lookup', args: [CANARY],
    })
    expect(reply.ok).toBe(false)
    expect(reply.error).not.toContain(CANARY)
    expect(reply.error).toContain(REDACTED_ARG)
  })

  it('still names the failure', async () => {
    const sock = await failingSeam()
    const reply = await callOnce(sock, {
      key: 'store', method: 'lookup', args: ['alice'],
    })
    expect(reply.error).toContain('Error')
    expect(reply.error).toContain('no entry for')
  })

  it('leaves the refusal replies (unknown key / method) unchanged', async () => {
    const sock = await failingSeam()
    const unknownMethod = await callOnce(sock, {
      key: 'store', method: 'wipe', args: [CANARY],
    })
    expect(unknownMethod.error).toContain('method wipe is not exported for key store')
    const unknownKey = await callOnce(sock, { key: 'vault', method: 'lookup', args: [] })
    expect(unknownKey.error).toContain('is not exported by this process')
  })
})
