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
let attempts = 0
function attempt() {
  const s = net.connect(process.env.BRIDGE_SOCK)
  let buf = ''
  s.on('connect', () => s.write(process.env.BRIDGE_REQ + '\\n'))
  s.on('data', (d) => {
    buf += d
    const i = buf.indexOf('\\n')
    if (i >= 0) { process.stdout.write(buf.slice(0, i)); s.end(); process.exit(0) }
  })
  s.on('error', () => {
    s.destroy()
    if (++attempts > 100) { process.stdout.write(JSON.stringify({ ok: false, error: 'bridge connect failed' })); process.exit(0) }
    setTimeout(attempt, 50)  // provider may still be coming up
  })
}
attempt()
`

// Canonical ADT/Result wire codec (docs/interop-bridge.md "Canonical value
// encoding"). The TS runtime ADT form is `{ kind, value? }` (records are plain
// objects, Opt is bare value | undefined). On the wire a tagged value is
// `{ "$kind": Case, "$value"?: payload }`; the `$kind` marker is what separates
// it from a record. So encode renames kind/value -> $kind/$value, decode the
// reverse, both recursively; records/arrays/scalars/null pass through.

function isNativeAdt(o: Record<string, unknown>): boolean {
  return typeof o.kind === 'string' && Object.keys(o).every((k) => k === 'kind' || k === 'value')
}

export function encodeValue(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(encodeValue)
  if (v && typeof v === 'object' && !(v instanceof Map)) {
    const o = v as Record<string, unknown>
    if (isNativeAdt(o)) {
      const out: Record<string, unknown> = { $kind: o.kind }
      if ('value' in o) out.$value = encodeValue(o.value)
      return out
    }
    const rec: Record<string, unknown> = {}
    for (const k of Object.keys(o)) rec[k] = encodeValue(o[k])
    return rec
  }
  return v
}

export function decodeValue(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(decodeValue)
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    if (typeof o.$kind === 'string') {
      const out: Record<string, unknown> = { kind: o.$kind }
      if ('$value' in o) out.value = decodeValue(o.$value)
      return out
    }
    const rec: Record<string, unknown> = {}
    for (const k of Object.keys(o)) rec[k] = decodeValue(o[k])
    return rec
  }
  return v
}

function syncCall(socketPath: string, key: string, method: string, args: unknown[]): unknown {
  const request = JSON.stringify({ key, method, args })
  const out = execFileSync(process.execPath, ['-e', CLIENT_SRC], {
    env: { ...process.env, BRIDGE_SOCK: socketPath, BRIDGE_REQ: request },
    encoding: 'utf8',
  })
  const reply = JSON.parse(out)
  if (!reply.ok) throw new Error(reply.error)
  return decodeValue(reply.value)
}

/** A cordis component that provides `key` via a proxy forwarding to `socketPath`,
 *  plus `onPeerLost` to observe the provider's death (monitor connection EOF). */
export function makeProxy(key: string, methods: string[], socketPath: string) {
  const lostCallbacks: Array<() => void> = []
  let fired = false
  let everConnected = false
  let monitor: net.Socket
  const connectMonitor = () => {
    monitor = net.connect(socketPath)
    monitor.on('connect', () => { everConnected = true })
    monitor.on('error', () => {}) // a 'close' event always follows
    monitor.on('close', () => {
      if (!everConnected) {
        setTimeout(connectMonitor, 50) // provider not up yet; keep trying
        return
      }
      if (fired) return
      fired = true
      for (const cb of lostCallbacks) cb()
    })
  }
  connectMonitor()

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

/** Provider side: export `keys` from `ctx` over a Unix socket, so another
 *  process can proxy them. Dispatches each JSON call to the committed view
 *  `ctx[key][method](...args)`; the reply carries the value (or the error). */
export async function serve(ctx: Context, keys: string[], socketPath: string): Promise<net.Server> {
  const exported = new Set(keys)

  const server = net.createServer((sock) => {
    let buf = ''
    sock.on('data', async (chunk) => {
      buf += chunk
      let nl: number
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)
        if (!line) continue
        let reply: unknown
        try {
          const req = JSON.parse(line)
          if (!exported.has(req.key)) {
            reply = { ok: false, error: `key ${req.key} is not exported by this process` }
          } else {
            const service = (ctx as any)[req.key]
            let result = service[req.method](...(req.args ?? []))
            if (result && typeof result.then === 'function') result = await result
            reply = { ok: true, value: encodeValue(result ?? null) }
          }
        } catch (error) {
          reply = { ok: false, error: String(error) }
        }
        sock.write(JSON.stringify(reply) + '\n')
      }
    })
    sock.on('error', () => {})
  })

  await new Promise<void>((resolve) => server.listen(socketPath, () => resolve()))
  return server
}
