// A seam failure must not carry a HELD secret back either, not only the
// caller's own arguments (roadmap item 421 F5, the second stage).
//
// `seamFailure` scrubbed exactly one thing: the values THIS call was made with.
// That answers "did the error channel hand the consumer back what it sent?" and
// nothing else. A credential the provider is holding rather than being handed —
// a `Secret[T]` config field, a token an extern minted earlier in the
// activation — is not among this call's arguments, so the argument scrub never
// saw it, and a provider message quoting it crossed the trust boundary intact.
//
// The python tier has answered both questions since F5: `redact_call_text`
// scrubs the arguments and then hands the text to `redact_text`. This tier had
// only the first half. The second is not a new mechanism — item 421 F6 landed
// the value registry that the host trace already funnels through — so the fix
// is to READ that registry here rather than to keep a second one. One marking,
// two sinks.
//
// The two markers stay distinct on purpose: `<redacted:arg>` says a value the
// caller passed in was here, `<redacted:secret>` says a declared `Secret[T]`
// was. A reader of a seam error should be able to tell which, and a test that
// accepted either would not notice the stages collapsing.
//
// Every assertion is PAIRED — the canary absent AND the marker present — so
// none can pass on an empty reply, and the false-positive half is the other
// side of the claim: an ordinary value is still quoted verbatim and the
// sentence around a redaction survives, so a seam error stays worth reading.
import { afterEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { REDACTED_ARG, seamFailure, serve } from '../bridge.ts'
import { forgetSecrets, markSecret } from '../runtime.ts'

// Spelled out rather than imported, so this suite can RUN against a tree with
// no second stage in it and fail on the leak instead of on a missing symbol.
const SECRET = 'SEKRIT-HELD-CONFIG-9a8b7c6d'
const REDACTED_SECRET = '<redacted:secret>'

const dirs: string[] = []
const servers: net.Server[] = []

afterEach(() => {
  // The registry is process-wide by design, so each test starts and ends with
  // an empty set; otherwise one test's canary would redact another's.
  forgetSecrets()
  for (const server of servers.splice(0)) server.close()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

function socketPath(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'revl_seam_secret_'))
  dirs.push(dir)
  return path.join(dir, 'provider.sock')
}

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

describe('seamFailure, the held secret', () => {
  it('scrubs a registered secret that is not one of the arguments', () => {
    markSecret(SECRET)
    const text = seamFailure(
      new Error(`upstream refused for token ${SECRET}`), ['user-1234'])
    expect(text).not.toContain(SECRET)
    expect(text).toContain(REDACTED_SECRET)
  })

  it('keeps the failure worth reading', () => {
    markSecret(SECRET)
    const text = seamFailure(
      new TypeError(`bad token ${SECRET} at row 3`), ['user-1234'])
    expect(text).toContain('TypeError')
    expect(text).toContain('bad token')
    expect(text).toContain('at row 3')
    expect(text).not.toContain(SECRET)
  })

  it('reports an argument as an argument even when it is also a secret', () => {
    // The argument scrub runs first, so the more specific of the two facts is
    // the one the consumer is told. Both markers exist so this stays legible.
    markSecret(SECRET)
    const text = seamFailure(new Error(`no entry for ${SECRET}`), [SECRET])
    expect(text).not.toContain(SECRET)
    expect(text).toContain(REDACTED_ARG)
    expect(text).not.toContain(REDACTED_SECRET)
  })

  it('finds a secret registered from inside a container', () => {
    // A `Secret[T]` where T is a record is confidential WHOLE, and `markSecret`
    // walks it, so a leaf interpolated into host text is still matched.
    markSecret({ token: SECRET, tenant: 'acme' })
    const text = seamFailure(new Error(`refused: ${SECRET}`), [])
    expect(text).not.toContain(SECRET)
    expect(text).toContain(REDACTED_SECRET)
  })

  it('leaves an ordinary diagnostic alone', () => {
    // The false-positive control. The match is exact against what a declared
    // marking registered, never a pattern, so a message that mentions no
    // secret is byte-identical — a funnel that erased everything would pass
    // every test above and fail this one.
    markSecret(SECRET)
    const text = seamFailure(new Error('connection reset by peer'), ['user-1234'])
    expect(text).toBe('Error: connection reset by peer')
  })

  it('costs nothing when the composition declares no secret', () => {
    const text = seamFailure(new Error(`token ${SECRET} refused`), [])
    expect(text).toContain(SECRET)   // nothing registered, nothing redacted
    expect(text).not.toContain(REDACTED_SECRET)
  })
})

describe('the seam end to end', () => {
  /** A provider holding a credential it was never PASSED — the shape a
   *  `Secret[T]` config field takes once the component is constructed. */
  async function holdingSeam(): Promise<string> {
    const ctx: any = {
      vault: {
        fetch(key: string) {
          throw new Error(`upstream refused key ${key} for token ${SECRET}`)
        },
      },
    }
    const sock = socketPath()
    servers.push(await serve(ctx, { vault: ['fetch'] }, sock))
    return sock
  }

  it('does not send a held secret back over the wire', async () => {
    markSecret(SECRET)
    const sock = await holdingSeam()
    const reply = await callOnce(
      sock, { key: 'vault', method: 'fetch', args: ['user-1234'] })
    expect(reply.ok).toBe(false)
    expect(reply.error).not.toContain(SECRET)
    expect(reply.error).toContain(REDACTED_SECRET)
    // ...and the call's own argument is still reported as an argument, so the
    // two stages are both live on the wire rather than one masking the other.
    expect(reply.error).toContain(REDACTED_ARG)
    // ...and the operator still learns what went wrong.
    expect(reply.error).toContain('upstream refused key')
  })
})
