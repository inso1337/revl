// item 421 F5 — the CONSUMER side of the seam error funnel.
//
// `seamFailure` ran on the provider only. The provider's dispatch funnel scrubs
// the error it is about to send, and then `seamCall` did
//
//     if (!reply.ok) throw new Error(reply.error)
//
// — verbatim. That is a trust assumption about the peer, not a property of this
// process: the reply is attacker-controlled text from the consumer's point of
// view, and the consumer holds values it has no intention of handing to
// whatever handler catches the throw. Every in-repo typescript sink was
// verified scrubbed, so nothing in this tree leaks today; the gap is that the
// scrub depends on the peer's implementation rather than on this process's.
//
// The fix reuses the funnel rather than restating it: the consumer runs the
// same `seamFailure` over the reply before throwing. A peer running this tier's
// `serve` has already scrubbed, so the second pass finds nothing to match and
// is the identity — asserted below, because that is the whole reason the pass
// is safe to add. A peer that is not this tier's `serve` is caught by it.
//
// The non-vacuity arm is the wire itself: the hostile provider in this file
// really does send the canary, and the test reads the raw reply first and
// asserts it leaks. Without the consumer pass the assertion below it is the one
// that fails — which is the leak, not a missing marker.
import { afterEach, describe, expect, it } from 'vitest'
import { spawn, type ChildProcess } from 'node:child_process'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { makeProxy, REDACTED_ARG, seamFailure } from '../bridge.ts'
import { forgetSecrets, markSecret } from '../runtime.ts'

// Spelled out rather than imported, so this suite can RUN against a tree with
// no consumer-side stage and fail on the leak instead of a missing symbol.
const REDACTED_SECRET = '<redacted:secret>'

const ARG_CANARY = 'SEKRIT-CONSUMER-ARG-4f1a2b'
const HELD_CANARY = 'SEKRIT-CONSUMER-HELD-7c3d9e'

const dirs: string[] = []
const peers: ChildProcess[] = []
const proxies: Array<{ close: () => void }> = []

afterEach(() => {
  // The registry is process-wide by design, so each test starts and ends with
  // an empty set; otherwise one test's canary would redact another's.
  forgetSecrets()
  for (const peer of peers.splice(0)) peer.kill()
  for (const proxy of proxies.splice(0)) proxy.close()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

function socketPath(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'revl_seam_consumer_'))
  dirs.push(dir)
  return path.join(dir, 'hostile.sock')
}

/** One request, one reply, over a real socket — the raw wire, unfunnelled. */
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

/** The peer's whole program. It answers every call with a failure quoting the
 *  caller's own argument AND a credential it is holding, verbatim — no
 *  `seamFailure`, no registry, no scrub of any kind. This is the peer the
 *  consumer pass exists for: a non-revl implementation, or one predating item
 *  421 F5. */
const HOSTILE_PROVIDER = `
const net = require('node:net')
const sock = process.env.HOSTILE_SOCK
const held = process.env.HOSTILE_HELD
net.createServer((conn) => {
  let buf = ''
  conn.on('data', (chunk) => {
    buf += chunk
    const nl = buf.indexOf('\\n')
    if (nl < 0) return
    const request = JSON.parse(buf.slice(0, nl))
    const error = 'Error: no entry for ' + request.args[0] + ' (vault ' + held + ')'
    conn.write(JSON.stringify({ ok: false, error }) + '\\n')
  })
}).listen(sock)
`

/** A hostile peer, in its OWN process. It has to be its own process: `seamCall`
 *  blocks the consumer on `execFileSync`, so a provider sharing this event loop
 *  would never get to answer — which is also why nothing in this suite drove
 *  the consumer path before. The provider in production is always another
 *  process, so this is the shape the gap is actually about. */
async function hostileSeam(): Promise<string> {
  const sock = socketPath()
  const peer = spawn(process.execPath, ['-e', HOSTILE_PROVIDER], {
    env: { ...process.env, HOSTILE_SOCK: sock, HOSTILE_HELD: HELD_CANARY },
    stdio: 'ignore',
  })
  peers.push(peer)
  for (let i = 0; i < 200; i += 1) {
    if (fs.existsSync(sock)) return sock
    await new Promise((r) => setTimeout(r, 25))
  }
  throw new Error('the hostile peer never came up')
}

function consumer(sock: string) {
  const handle = makeProxy('store', ['lookup'], sock)
  proxies.push(handle)
  return handle.proxy
}

describe('the consumer side of a seam failure', () => {
  it('does not throw back a value the peer failed to scrub', async () => {
    markSecret(HELD_CANARY)
    const sock = await hostileSeam()
    // The premise, read off the wire: this peer really does send both values.
    // If this assertion ever stops holding the test below proves nothing.
    const raw = await callOnce(sock, { key: 'store', method: 'lookup', args: [ARG_CANARY] })
    expect(raw.ok).toBe(false)
    expect(raw.error).toContain(ARG_CANARY)
    expect(raw.error).toContain(HELD_CANARY)

    const proxy = consumer(sock)
    let thrown: string | null = null
    try {
      proxy.lookup(ARG_CANARY)
    } catch (error) {
      thrown = String(error)
    }
    expect(thrown).not.toBeNull()
    expect(thrown).not.toContain(ARG_CANARY)
    expect(thrown).not.toContain(HELD_CANARY)
    // Paired with the absences: the markers say WHAT was removed, and the
    // sentence around them survives, so the diagnostic is still worth reading.
    expect(thrown).toContain(REDACTED_ARG)
    expect(thrown).toContain(REDACTED_SECRET)
    expect(thrown).toContain('no entry for')
  })

  it('still quotes what it has no reason to hide', async () => {
    const sock = await hostileSeam()
    const proxy = consumer(sock)
    // `alice` is an argument of this call, so the funnel hides it — that is the
    // argument stage doing its job, not over-reach. What must survive verbatim
    // is the rest: the sentence around the redaction, and the peer's own
    // credential, which nothing here passed in and nothing here registered. The
    // pair is what keeps the test above from being "redact everything".
    expect(() => proxy.lookup('alice')).toThrow(/no entry for <redacted:arg>/)
    expect(() => proxy.lookup('alice')).toThrow(new RegExp(`vault ${HELD_CANARY}`))
  })

  it('is the identity over a reply the provider already scrubbed', () => {
    // The consumer pass is only safe to add because of this: a peer running
    // this tier's `serve` has already replaced these values, so the second
    // application has nothing left to match and cannot mangle the markers.
    const scrubbed = seamFailure(new Error(`no entry for ${ARG_CANARY}`), [ARG_CANARY])
    expect(scrubbed).toContain(REDACTED_ARG)
    expect(seamFailure(scrubbed, [ARG_CANARY])).toBe(scrubbed)
  })
})
