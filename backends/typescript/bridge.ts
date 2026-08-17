// py<->node interop bridge, consumer (Node / cordis) side.
//
// Mirror of backends/python/bridge.py's proxy half: a cordis component that
// provides a key by forwarding calls to a Unix-socket stub running in another
// process (here, the Python provider from demo/bridge_pypy.py). To the
// consumer, `ctx.db` is an ordinary provider; the language boundary is invisible.
//
// The crossed method here, Database.execute, is SYNCHRONOUS, which `revl audit`
// classifies as address-space-bound: a sync call over a socket is chatty. We
// honor that literally. Each call is a blocking round-trip (execFileSync a
// one-shot client), so the demo pays exactly the cost the audit predicts,
// while an `async fn` service would proxy without blocking. A persistent
// monitor connection turns provider death into provision withdrawal (R2/R3).

import type { Context } from 'cordis'
import { execFileSync } from 'node:child_process'
import net from 'node:net'

// One-shot client: connect, send one request line, print the reply line, exit.
// Run as `node -e`, fed the socket path and request via the environment.
const CLIENT_SRC = `
const net = require('node:net')
const s = net.connect(process.env.BRIDGE_SOCK)
let buf = ''
s.on('connect', () => s.write(process.env.BRIDGE_REQ + '\\n'))
s.on('data', (d) => {
  buf += d
  const i = buf.indexOf('\\n')
  if (i >= 0) { process.stdout.write(buf.slice(0, i)); s.end(); process.exit(0) }
})
s.on('error', (e) => { process.stdout.write(JSON.stringify({ ok: false, error: String(e) })); process.exit(0) })
`

function syncCall(socketPath: string, key: string, method: string, args: unknown[]): unknown {
  const request = JSON.stringify({ key, method, args })
  const out = execFileSync(process.execPath, ['-e', CLIENT_SRC], {
    env: { ...process.env, BRIDGE_SOCK: socketPath, BRIDGE_REQ: request },
    encoding: 'utf8',
  })
  const reply = JSON.parse(out)
  if (!reply.ok) throw new Error(reply.error)
  return reply.value
}

/** A cordis component that provides `key` via a proxy forwarding to `socketPath`,
 *  plus `onPeerLost` to observe the provider's death (monitor connection EOF). */
export function makeProxy(key: string, methods: string[], socketPath: string) {
  const lostCallbacks: Array<() => void> = []
  let fired = false
  const monitor = net.connect(socketPath)
  monitor.on('error', () => {}) // a 'close' event always follows
  monitor.on('close', () => {
    if (fired) return
    fired = true
    for (const cb of lostCallbacks) cb()
  })

  const proxy: Record<string, (...args: unknown[]) => unknown> = {}
  for (const method of methods) {
    proxy[method] = (...args: unknown[]) => syncCall(socketPath, key, method, args)
  }

  const component = {
    name: `${key[0].toUpperCase()}${key.slice(1)}Proxy`,
    inject: [] as string[],
    provide: [key],
    apply(ctx: Context) {
      ctx.effect(function* () {
        yield () => monitor.destroy()
        yield (ctx as any).provide(key, proxy)
      }, `${key}-proxy/body`)
    },
  }

  return { component, onPeerLost: (cb: () => void) => lostCallbacks.push(cb) }
}
