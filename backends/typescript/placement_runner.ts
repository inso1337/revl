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
  server = await serve(ctx, spec.serve.keys, spec.serve.socket)
  log('serve', spec.serve.keys.join(', '), `-> ${spec.serve.socket}`)
}

// 4. probes: call provided services (may cross a seam), print results
const scope: Record<string, unknown> = {}
for (const key of (spec.provides || []) as string[]) scope[key] = (ctx as any)[key]
for (const key of Object.keys(spec.proxies || {})) scope[key] = (ctx as any)[key]
for (const expr of (spec.probe || []) as string[]) {
  try {
    const fn = new Function(...Object.keys(scope), `return (${expr})`)
    let value = fn(...Object.values(scope))
    if (value && typeof value.then === 'function') value = await value
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
