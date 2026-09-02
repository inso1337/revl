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
import fs from 'node:fs'
import net from 'node:net'
import tls from 'node:tls'

// --- seam endpoints: a local UDS (default) or a network TCP + mTLS seam -------
//
// The node client mirrors backends/python/bridge.py's `Endpoint`/`TlsConfig`
// (docs/network-placement.md): a bare socket-path string is a local UDS (full
// back-compat), and a `{ host, port, tls }` object is a network seam reached
// over TCP wrapped in mutual TLS. The provider (py, item 56) demands the
// consumer's certificate (`CERT_REQUIRED`) and the consumer verifies the
// provider's against the same CA — a network seam is "the two processes holding
// CA-signed certs", not "whoever can reach the port". The canonical value
// codec, the seam deadline (item 54) and reactive withdrawal on peer death all
// apply unchanged over TCP; only the address family and the TLS wrap differ.

export interface TlsPaths {
  cert: string
  key: string
  ca: string
  identity: string
  server_hostname?: string
}

export interface NetworkEndpoint {
  host: string
  port: number
  tls: TlsPaths
}

/** A seam target: a UDS path (legacy/local) or a network TCP+mTLS endpoint. */
export type SeamTarget = string | NetworkEndpoint

function isNetwork(target: SeamTarget): target is NetworkEndpoint {
  return typeof target !== 'string' && target != null && typeof target.host === 'string'
}

/** A seam call that outlived its deadline: the provider is neither answering nor
 *  gone — it hung. The node mirror of backends/python/bridge.py's `SeamDeadline`
 *  (a distinguishable fault, disjoint from a peer death and from a remote
 *  error), so a caller — or a test — can tell a hang apart by kind. */
export class SeamDeadlineError extends Error {
  readonly key: string
  readonly method: string
  readonly deadlineMs: number
  constructor(key: string, method: string, deadlineMs: number) {
    super(`revl: seam call ${key}.${method} exceeded its ${deadlineMs}ms deadline`)
    this.name = 'SeamDeadlineError'
    this.key = key
    this.method = method
    this.deadlineMs = deadlineMs
  }
}

// One-shot client: connect (UDS or TCP+mTLS), send one request line, print the
// reply line, exit. Run as `node -e`, fed the target/request/deadline via the
// environment. A wedged provider trips the in-child deadline timer and the child
// prints a `{ seamDeadline: true }` reply rather than hanging; a TCP refusal
// (provider still coming up) is retried, so start order stays irrelevant.
const CLIENT_SRC = `
const net = require('node:net')
const tls = require('node:tls')
const fs = require('node:fs')
const mode = process.env.BRIDGE_MODE
const deadlineMs = process.env.BRIDGE_DEADLINE_MS ? Number(process.env.BRIDGE_DEADLINE_MS) : 0
let attempts = 0
let timer = null
let secured = false
function done(obj) {
  if (timer) clearTimeout(timer)
  process.stdout.write(JSON.stringify(obj))
  process.exit(0)
}
if (deadlineMs > 0) {
  timer = setTimeout(() => done({ ok: false, seamDeadline: true, error: 'seam call exceeded ' + deadlineMs + 'ms deadline' }), deadlineMs)
}
function dial() {
  let s
  if (mode === 'tcp') {
    s = tls.connect({
      host: process.env.BRIDGE_HOST,
      port: Number(process.env.BRIDGE_PORT),
      ca: fs.readFileSync(process.env.BRIDGE_CA),
      cert: fs.readFileSync(process.env.BRIDGE_CERT),
      key: fs.readFileSync(process.env.BRIDGE_KEY),
      servername: process.env.BRIDGE_SERVERNAME || process.env.BRIDGE_HOST,
    })
    s.on('secureConnect', () => { secured = true; s.write(process.env.BRIDGE_REQ + '\\n') })
  } else {
    s = net.connect(process.env.BRIDGE_SOCK)
    s.on('connect', () => s.write(process.env.BRIDGE_REQ + '\\n'))
  }
  let buf = ''
  s.on('data', (d) => {
    buf += d
    const i = buf.indexOf('\\n')
    if (i >= 0) { s.destroy(); done(JSON.parse(buf.slice(0, i))) }
  })
  s.on('error', () => {
    s.destroy()
    // A TLS handshake failure (bad cert / wrong CA) is terminal — never retry it
    // away as if the provider were merely still coming up.
    if (mode === 'tcp' && secured) { done({ ok: false, error: 'bridge tls stream error' }) }
    if (++attempts > 100) { done({ ok: false, error: 'bridge connect failed' }) }
    else setTimeout(dial, 50) // provider may still be coming up
  })
}
dial()
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

/** `Int` is a `bigint` on this tier, and the wire says `Int` crosses as a JSON
 *  *number* (docs/interop-bridge.md, "Canonical value encoding"). JSON.stringify
 *  THROWS on a bigint, so it has to convert here — and it refuses rather than
 *  rounding when the value is outside the range a JSON number holds exactly.
 *  Rounding at a seam is the silent precision loss this tier's BigInt port
 *  exists to remove; a 64-bit `Int` that does not fit the wire is a limit of
 *  the wire, and it should say so rather than hand over a different number. */
function bigintToWire(v: bigint): number {
  if (v > BigInt(Number.MAX_SAFE_INTEGER) || v < BigInt(Number.MIN_SAFE_INTEGER)) {
    throw new RangeError(
      `revl: Int ${v} is outside the range a JSON number represents exactly ` +
        `(docs/interop-bridge.md encodes Int as a number)`,
    )
  }
  return Number(v)
}

/** Bigints in an outbound argument, and nothing else. The request path does
 *  not run the ADT codec below — changing that is a cross-language decision,
 *  not this tier's — so this only replaces a guaranteed throw. */
function encodeBigInts(v: unknown): unknown {
  if (typeof v === 'bigint') return bigintToWire(v)
  if (Array.isArray(v)) return v.map(encodeBigInts)
  if (v && typeof v === 'object' && !(v instanceof Map)) {
    const o = v as Record<string, unknown>
    const rec: Record<string, unknown> = {}
    for (const k of Object.keys(o)) rec[k] = encodeBigInts(o[k])
    return rec
  }
  return v
}

export function encodeValue(v: unknown): unknown {
  if (typeof v === 'bigint') return bigintToWire(v)
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

/** Environment for the one-shot client, per seam target (UDS or TCP+mTLS). */
function clientEnv(target: SeamTarget, request: string, deadlineMs: number | null): NodeJS.ProcessEnv {
  const base: NodeJS.ProcessEnv = { ...process.env, BRIDGE_REQ: request }
  if (deadlineMs != null) base.BRIDGE_DEADLINE_MS = String(Math.max(1, Math.round(deadlineMs)))
  if (isNetwork(target)) {
    return {
      ...base,
      BRIDGE_MODE: 'tcp',
      BRIDGE_HOST: target.host,
      BRIDGE_PORT: String(target.port),
      BRIDGE_CERT: target.tls.cert,
      BRIDGE_KEY: target.tls.key,
      BRIDGE_CA: target.tls.ca,
      BRIDGE_SERVERNAME: target.tls.server_hostname ?? target.host,
    }
  }
  return { ...base, BRIDGE_MODE: 'uds', BRIDGE_SOCK: target }
}

/** One blocking seam round-trip to `target`. `deadlineMs` bounds the reply: a
 *  wedged provider surfaces as a `SeamDeadlineError` (a hang), a dropped
 *  connection as a plain `Error` (a death), a provider-side failure as the
 *  marshalled error — the same three disjoint fault kinds as the py client. */
function seamCall(
  target: SeamTarget,
  key: string,
  method: string,
  args: unknown[],
  deadlineMs: number | null,
): unknown {
  const request = JSON.stringify({ key, method, args: args.map(encodeBigInts) })
  let out: string
  try {
    out = execFileSync(process.execPath, ['-e', CLIENT_SRC], {
      env: clientEnv(target, request, deadlineMs),
      encoding: 'utf8',
      // Backstop the in-child deadline timer: if the child itself wedges, kill
      // it a little past the seam deadline and treat that as the same breach.
      timeout: deadlineMs != null ? Math.round(deadlineMs) + 5000 : undefined,
      maxBuffer: 64 * 1024 * 1024,
    })
  } catch (error) {
    if ((error as { code?: string }).code === 'ETIMEDOUT') {
      throw new SeamDeadlineError(key, method, deadlineMs ?? 0)
    }
    throw error
  }
  const reply = JSON.parse(out)
  if (reply.seamDeadline) throw new SeamDeadlineError(key, method, deadlineMs ?? 0)
  if (!reply.ok) throw new Error(reply.error)
  return decodeValue(reply.value)
}

/** Watch a provider for death: connect an idle monitor to `target` (UDS or
 *  TCP+mTLS) and call `onLost` once when the connection drops after having been
 *  established (peer death — the mTLS monitor EOFs exactly as the UDS one does).
 *  Returns a handle whose `close()` tears the monitor down without firing. */
export function watchPeer(
  target: SeamTarget,
  onLost: () => void,
): { close: () => void; ready: Promise<void> } {
  let fired = false
  let closed = false
  let everConnected = false
  let monitor: net.Socket | tls.TLSSocket
  let markReady: () => void
  const ready = new Promise<void>((resolve) => { markReady = resolve })
  const established = () => {
    everConnected = true
    markReady()
  }
  const connect = () => {
    if (closed) return
    if (isNetwork(target)) {
      monitor = tls.connect({
        host: target.host,
        port: target.port,
        ca: fs.readFileSync(target.tls.ca),
        cert: fs.readFileSync(target.tls.cert),
        key: fs.readFileSync(target.tls.key),
        servername: target.tls.server_hostname ?? target.host,
      })
      monitor.on('secureConnect', established)
    } else {
      monitor = net.connect(target)
      monitor.on('connect', established)
    }
    monitor.on('error', () => {}) // a 'close' event always follows
    monitor.on('close', () => {
      if (closed) return
      if (!everConnected) {
        setTimeout(connect, 50) // provider not up yet; keep trying
        return
      }
      if (fired) return
      fired = true
      onLost()
    })
  }
  connect()
  return {
    ready,
    close() {
      closed = true
      try {
        monitor?.destroy()
      } catch {
        /* best-effort */
      }
    },
  }
}

/** A cordis component that provides `key` via a proxy forwarding to `target`
 *  (a UDS path, or a `{ host, port, tls }` network TCP+mTLS endpoint), plus
 *  `onPeerLost` to observe the provider's death. `deadlineMs` bounds each seam
 *  call; on a **network** seam a breached deadline is treated as a lost peer and
 *  triggers reactive withdrawal (a wedged remote provider is, to a consumer,
 *  indistinguishable from a dead one — the seam is unusable either way), while a
 *  local UDS seam keeps the death-only withdrawal it always had. */
export function makeProxy(
  key: string,
  methods: string[],
  target: SeamTarget,
  deadlineMs: number | null = null,
) {
  const lostCallbacks: Array<() => void> = []
  let fired = false
  const fireLost = () => {
    if (fired) return
    fired = true
    for (const cb of lostCallbacks) cb()
  }
  const network = isNetwork(target)
  const monitor = watchPeer(target, fireLost)

  const proxy: Record<string, (...args: unknown[]) => unknown> = {}
  for (const method of methods) {
    proxy[method] = (...args: unknown[]) => {
      try {
        return seamCall(target, key, method, args, deadlineMs)
      } catch (error) {
        // A network seam that breaches its deadline withdraws: the remote
        // provider is unreachable-in-time, so the consumer stops depending on
        // it rather than re-attempting against a wedged machine.
        if (network && error instanceof SeamDeadlineError) fireLost()
        throw error
      }
    }
  }

  const component = {
    name: `${key[0].toUpperCase()}${key.slice(1)}Proxy`,
    inject: [] as string[],
    provide: [key],
    apply(ctx: Context) {
      ctx.effect(function* () {
        yield () => monitor.close()
        yield (ctx as any).provide(key, proxy)
      }, `${key}-proxy/body`)
    },
  }

  return {
    component,
    proxy,
    onPeerLost: (cb: () => void) => lostCallbacks.push(cb),
    monitorReady: monitor.ready,
    close: () => monitor.close(),
  }
}

/** Normalize `serve`'s exports to key -> allowed method names.
 *
 *  Two accepted forms, mirroring backends/python/bridge.py:
 *  - `{ key: [method, ...] }` — the *declared* form: the service's own
 *    operation list, read off the IR (src/revl/placement.py ships it in the
 *    process spec), so the stub admits exactly what the `service` declaration
 *    admits.
 *  - `string[]` — the legacy key-only form (demo/bridge_ts_adt.mts). With no
 *    declared list the allowlist is derived at dispatch time from the provided
 *    object's own function-valued properties: weaker (it trusts the object,
 *    not the interface) but still an allowlist. */
function exportTable(
  exports: string[] | Record<string, string[]>,
): Map<string, Set<string> | null> {
  const table = new Map<string, Set<string> | null>()
  if (Array.isArray(exports)) {
    for (const key of exports) table.set(key, null)
  } else {
    for (const [key, methods] of Object.entries(exports)) table.set(key, new Set(methods ?? []))
  }
  return table
}

function publicMethods(service: unknown): Set<string> {
  const names = new Set<string>()
  for (let o = service as Record<string, unknown> | null; o && o !== Object.prototype; o = Object.getPrototypeOf(o)) {
    for (const name of Object.getOwnPropertyNames(o)) {
      if (name === 'constructor' || name.startsWith('_')) continue
      try {
        if (typeof (service as Record<string, unknown>)[name] === 'function') names.add(name)
      } catch {
        /* a getter that throws is not a method */
      }
    }
  }
  return names
}

// --- what a failure may carry BACK across the seam (item 421 F5) -------------
//
// Mirror of `backends/python/bridge.py`'s `seam_failure` / `confidential.py`
// `redact_call_text`, spelled locally because this file is loaded standalone by
// the node consumer and imports nothing from the runtime.
//
// The consumer is on the other side of a trust boundary. A forward crossing into
// a declared `Secret[T]` receiver authorises disclosure TO THE RECEIVER; it does
// not authorise the error channel to perform the reverse crossing the checker
// refuses statically. And the trigger needs no author interpolation: a plain
// `store.get(token)` miss throws a message quoting the token. So every argument
// value this call was made with is scrubbed out of the host error text, while the
// error's TYPE and the sentence around it survive, so the reply is still worth
// reading, just without the caller's bytes in it.

/** The placeholder a caller's own argument is rendered as inside seam error
 *  text. Must equal `confidential.REDACTED_ARG` on the python tier: a polyglot
 *  seam should produce the SAME marker whichever tier answered. */
export const REDACTED_ARG = '<redacted:arg>'

// An argument shorter than this is left alone: below it a substring match is a
// coin flip against ordinary English and replacing it would shred the
// diagnostic for no confidentiality gain. Same bound as the python tier.
const MIN_MATCHABLE_ARG = 3

function argNeedles(value: unknown, into: Set<string>): void {
  if (value === null || value === undefined || typeof value === 'boolean') return
  if (typeof value === 'string') {
    if (value.length >= MIN_MATCHABLE_ARG) into.add(value)
    return
  }
  if (typeof value === 'number' || typeof value === 'bigint') {
    const form = String(value)
    if (form.length >= MIN_MATCHABLE_ARG) into.add(form)
    return
  }
  if (Array.isArray(value)) {
    for (const item of value) argNeedles(item, into)
    return
  }
  if (typeof value === 'object') {
    // Values only: a record's KEYS are field names the author wrote, not the
    // caller's data, and erasing them would destroy the diagnostic's shape.
    for (const item of Object.values(value as Record<string, unknown>)) argNeedles(item, into)
  }
}

/** The error text a provider-side failure is allowed to send back to the
 *  consumer, with this call's own argument values replaced by `REDACTED_ARG`.
 *  Longest needle first, so one that contains another leaves no tail behind. */
export function seamFailure(error: unknown, args: unknown[]): string {
  let text = String(error)
  const needles = new Set<string>()
  argNeedles(args ?? [], needles)
  for (const needle of [...needles].sort((a, b) => b.length - a.length)) {
    if (needle && text.includes(needle)) text = text.split(needle).join(REDACTED_ARG)
  }
  return text
}

/** Provider side: export a declared surface from `ctx` over a Unix socket, so
 *  another process can proxy it. Dispatches each JSON call to the committed
 *  view `ctx[key][method](...args)` — but only after checking BOTH halves of
 *  the request against the declaration: an unknown key and an unknown method
 *  are refused identically, so the seam is exactly the enumerable surface the
 *  service declares (G8). The reply carries the value (or the error). */
export async function serve(
  ctx: Context,
  exports: string[] | Record<string, string[]>,
  socketPath: string,
): Promise<net.Server> {
  const table = exportTable(exports)

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
        // Hoisted so the catch can scrub the failing call's own arguments out of
        // the host error text (item 421 F5). A line that fails to parse never
        // assigns it, and an empty needle set leaves the text alone.
        let callArgs: unknown[] = []
        try {
          const req = JSON.parse(line)
          callArgs = req.args ?? []
          if (!table.has(req.key)) {
            reply = { ok: false, error: `key ${req.key} is not exported by this process` }
          } else {
            const service = (ctx as any)[req.key]
            const allowed = table.get(req.key) ?? publicMethods(service)
            if (!allowed.has(req.method)) {
              const listed = [...allowed].sort().join(', ') || '(none)'
              reply = {
                ok: false,
                error: `method ${req.method} is not exported for key ${req.key} (exported: ${listed})`,
              }
            } else {
              let result = service[req.method](...(req.args ?? []))
              if (result && typeof result.then === 'function') result = await result
              reply = { ok: true, value: encodeValue(result ?? null) }
            }
          }
        } catch (error) {
          reply = { ok: false, error: seamFailure(error, callArgs) }
        }
        sock.write(JSON.stringify(reply) + '\n')
      }
    })
    sock.on('error', () => {})
  })

  await new Promise<void>((resolve) => server.listen(socketPath, () => resolve()))
  return server
}
