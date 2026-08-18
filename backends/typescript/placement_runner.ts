// One node-placed process of a placement composition (spawned by
// src/revl/placement.py for a process whose backend is "node").
//
// Same spec shape as src/revl/_process_runner.py, plus `module`: the path to
// the emitted cordis-ts module for this composition. Brings its slice up on a
// cordis Context: load cross-consumed keys as bridge proxies, load its own
// components (emitted TS), serve the keys other processes need, run probes,
// hold until SIGTERM, then tear down consumers-first.
//
// Output is line-prefixed `[name]` so the conductor can interleave processes.

import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { Context, FiberState } from 'cordis'

import { makeProxy, serve } from './bridge.ts'

const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const name: string = spec.name

const STATE: Record<number, string> = {
  [FiberState.PENDING]: 'PENDING',
  [FiberState.LOADING]: 'LOADING',
  [FiberState.ACTIVE]: 'ACTIVE',
  [FiberState.FAILED]: 'FAILED',
  [FiberState.DISPOSED]: 'DISPOSED',
  [FiberState.UNLOADING]: 'UNLOADING',
}

function log(channel: string, subject: string, detail = ''): void {
  console.log(`[${name}] ${channel.padEnd(6)}| ${String(subject).padEnd(16)}| ${detail}`.trimEnd())
}

// --- probes: parsed, not evaluated -----------------------------------------
// A placement file is *data*, not a program, so a probe is dispatched from a
// parse rather than handed to `new Function` (the JS `eval`). The admitted
// grammar is the same one every other backend requires (src/revl/placement.py
// ::_parse_probe, src/revl/_process_runner.py::_eval_probe): one method call
// on one key this process holds, with literal arguments.

const PROBE_RE = /^\s*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\(([\s\S]*)\)\s*$/

/** Scan a probe's argument list: string / number / true / false / null only. */
function parseProbeArgs(src: string): unknown[] {
  const args: unknown[] = []
  let rest = src.trim()
  while (rest.length > 0) {
    if (rest[0] === "'" || rest[0] === '"') {
      const quote = rest[0]
      let i = 1
      let text = ''
      for (; i < rest.length && rest[i] !== quote; i++) {
        if (rest[i] === '\\' && i + 1 < rest.length) i++
        text += rest[i]
      }
      if (i >= rest.length) throw new Error(`unterminated string literal in probe args: ${src}`)
      args.push(text)
      rest = rest.slice(i + 1).trim()
      if (rest.length > 0 && rest[0] !== ',') {
        throw new Error(`probe arguments must be a comma-separated literal list: ${src}`)
      }
    } else {
      const end = rest.indexOf(',')
      const raw = (end < 0 ? rest : rest.slice(0, end)).trim()
      if (raw === 'true' || raw === 'false') args.push(raw === 'true')
      else if (raw === 'null') args.push(null)
      else if (raw !== '' && !Number.isNaN(Number(raw))) args.push(Number(raw))
      else throw new Error(`probe arguments must be literals, got ${JSON.stringify(raw)}`)
      rest = end < 0 ? '' : rest.slice(end + 1).trim()
    }
    if (rest.startsWith(',')) rest = rest.slice(1).trim()
  }
  return args
}

function evalProbe(expr: string, scope: Record<string, unknown>): unknown {
  const match = PROBE_RE.exec(expr)
  if (!match) throw new Error('probe must be of the form key.method(arg, ...)')
  const [, key, method, argSrc] = match
  if (!(key in scope)) {
    const held = Object.keys(scope).sort().join(', ') || 'none'
    throw new Error(`'${key}' is not a key this process holds (holds: ${held})`)
  }
  const service = scope[key] as Record<string, unknown>
  const target = service?.[method]
  if (typeof target !== 'function') throw new Error(`'${key}' has no method '${method}'`)
  return (target as (...a: unknown[]) => unknown).apply(service, parseProbeArgs(argSrc))
}

const mod = await import(pathToFileURL(path.resolve(spec.module)).href)
const ctx = new Context()
ctx.on('internal/status', (fiber: any, oldState: number) =>
  log('fiber', fiber.name, `${STATE[oldState]} -> ${STATE[fiber.state]}`),
)

const fibers: Array<[string, any]> = []

// 1. proxies for keys provided by other processes
for (const [key, info] of Object.entries<any>(spec.proxies || {})) {
  const { component, onPeerLost } = makeProxy(key, info.methods, info.socket)
  const fiber = ctx.plugin(component)
  await fiber
  fibers.push([`${key}-proxy`, fiber])
  onPeerLost(() => void fiber.dispose())
  log('proxy', key, `-> ${info.socket}`)
}

// 2. this process's own components, in IR load order
for (const cname of spec.components as string[]) {
  const config = (spec.config || {})[cname]
  const fiber = config ? ctx.plugin(mod[cname], config) : ctx.plugin(mod[cname])
  await fiber
  fibers.push([cname, fiber])
  log('load', cname, `state=${STATE[fiber.state]}`)
}

// 3. serve keys other processes need
let server: import('node:net').Server | undefined
if (spec.serve) {
  // `methods` (key -> declared operations) is the stub's allowlist; fall back
  // to the bare key list for a spec written before it existed.
  server = await serve(ctx, spec.serve.methods ?? spec.serve.keys, spec.serve.socket)
  log('serve', spec.serve.keys.join(', '), `-> ${spec.serve.socket}`)
}

// 4. probes: call provided services (may cross a seam), print results
const scope: Record<string, unknown> = {}
for (const key of (spec.provides || []) as string[]) scope[key] = (ctx as any)[key]
for (const key of Object.keys(spec.proxies || {})) scope[key] = (ctx as any)[key]
for (const expr of (spec.probe || []) as string[]) {
  try {
    let value = evalProbe(expr, scope)
    if (value && typeof (value as any).then === 'function') value = await value
    log('probe', expr, `=> ${value === undefined ? 'undefined' : JSON.stringify(value)}`)
  } catch (error) {
    log('probe', expr, `ERROR ${error}`)
  }
}

console.log(`[${name}] UP`)

// 5. hold until the conductor stops us
let stopping = false
async function teardown(): Promise<void> {
  if (stopping) return
  stopping = true
  for (const [, fiber] of [...fibers].reverse()) {
    try {
      await fiber.dispose()
    } catch {
      /* best-effort */
    }
  }
  if (server) server.close()
  console.log(`[${name}] DOWN`)
  process.exit(0)
}
process.on('SIGTERM', teardown)
process.on('SIGINT', teardown)
const keepAlive = setInterval(() => {}, 1 << 30) // hold the event loop
void keepAlive
