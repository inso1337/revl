// FRAMEWORK-BENCH-1, the third host: a residue probe for a tool pack installed
// on @modelcontextprotocol/sdk (pinned in package.json / package-lock.json).
//
//   node probe.mjs <module> [--cycles N] [--json]
//
// <module> exports `install(server, config)`. The probe builds ONE McpServer,
// connects a Client to it over the SDK's own in-memory transport, and then
// installs and uninstalls the module N times on that server. It is the MCP
// counterpart of tools/residue-probe (which mounts a Cordis plugin N times),
// and it measures the same question: after the host has unloaded the module,
// is the host back where it started?
//
// UNLOAD is what the framework offers and nothing more. Every registration
// method (`registerTool`, `registerResource`, `registerPrompt` and their
// deprecated short forms) returns a handle with `remove()`; the probe records
// each handle the module obtains and calls `remove()` on all of them. MCP has
// no per-registration teardown callback, so the module may also RETURN a
// function from `install`, and the probe calls it after the removals. That
// return convention is this benchmark's, not the SDK's, and bench/hosts.json
// says so: it is the least a host that loads tool packs dynamically has to
// offer, and without it a careful module would have no way to be clean.
//
// FOUR CATEGORIES, each read from something public:
//   registry   tools, resources and prompts the Client can list (the protocol)
//   resources  host.Pool / host.Map / host.Job handles still open
//              (backends/typescript/runtime.ts `liveResources`, the same set
//              the TS backend's own no-residue check reads)
//   listeners  listeners on `process`, per event name
//   timers     timers created and neither fired nor cleared, counted by
//              wrapping the global timer functions for the probe's lifetime
// A category leaks when its final value differs from the baseline taken after
// the host is connected and before the first install.
//
// Exit code: 0 no residue, 1 residue, 2 the module could not be probed.

import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { liveResources } from '../../backends/typescript/runtime.ts'

export const REGISTRATION_METHODS = [
  'registerTool', 'tool',
  'registerResource', 'resource',
  'registerPrompt', 'prompt',
]

export const CATEGORIES = ['registry', 'resources', 'listeners', 'timers']

function parseArgs(argv) {
  const opts = { cycles: 5, json: false, module: undefined }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--json') opts.json = true
    else if (a === '--cycles') opts.cycles = Number(argv[++i])
    else if (a.startsWith('--')) throw new Error(`unknown flag ${a}`)
    else opts.module = a
  }
  if (!opts.module) throw new Error('usage: node probe.mjs <module> [--cycles N] [--json]')
  if (!Number.isInteger(opts.cycles) || opts.cycles < 1) {
    throw new Error(`--cycles must be a positive integer (got ${opts.cycles})`)
  }
  return opts
}

async function listed(fn, key) {
  try {
    const out = await fn()
    return (out[key] ?? []).map((item) => `${key}:${item.name ?? item.uri}`)
  } catch {
    // A server that has never had a registration of this kind does not
    // advertise the list method. That is an empty list, not an error.
    return []
  }
}

function listenerCounts() {
  const out = {}
  for (const name of process.eventNames()) {
    out[String(name)] = process.listenerCount(name)
  }
  return out
}

// Timers are counted by the probe itself rather than read from
// `process.getActiveResourcesInfo()`, which omits a timer that was `unref()`ed:
// an unref'd interval nobody clears is still a leak, it just does not keep the
// process alive. A timer is live from creation until it fires (one-shot) or is
// cleared. The wrappers are installed before the baseline, so the host's own
// timers are in the baseline and only a change is reported.
const liveTimers = new Set()

function trackTimers() {
  const { setTimeout: st, setInterval: si, clearTimeout: ct, clearInterval: ci } = globalThis
  globalThis.setTimeout = function (fn, ...rest) {
    const handle = st.call(this, function (...args) {
      liveTimers.delete(handle)
      return typeof fn === 'function' ? fn.apply(this, args) : undefined
    }, ...rest)
    liveTimers.add(handle)
    return handle
  }
  globalThis.setInterval = function (...args) {
    const handle = si.apply(this, args)
    liveTimers.add(handle)
    return handle
  }
  globalThis.clearTimeout = function (handle) {
    liveTimers.delete(handle)
    return ct.call(this, handle)
  }
  globalThis.clearInterval = function (handle) {
    liveTimers.delete(handle)
    return ci.call(this, handle)
  }
  return () => Object.assign(globalThis, {
    setTimeout: st, setInterval: si, clearTimeout: ct, clearInterval: ci,
  })
}

function timerCount() {
  return liveTimers.size
}

// How long a snapshot waits for work already queued to run. A timeout due
// within this window fires before the read, so a zero-delay callback is not
// mistaken for a live timer; one due later than this is still pending after
// unload, which is what a leaked timer is.
export const SETTLE_MS = 20

async function settle() {
  // Let removal notifications, microtask-scheduled teardown and near-due
  // timers finish before reading, so a slow-but-correct pack is not reported
  // as leaking.
  for (let i = 0; i < 3; i++) await new Promise((r) => setImmediate(r))
  await new Promise((r) => setTimeout(r, SETTLE_MS))
  await new Promise((r) => setImmediate(r))
}

export async function snapshot(client) {
  await settle()
  const registry = [
    ...await listed(() => client.listTools(), 'tools'),
    ...await listed(() => client.listResources(), 'resources'),
    ...await listed(() => client.listPrompts(), 'prompts'),
  ].sort()
  return {
    registry,
    resources: [...liveResources].sort(),
    listeners: listenerCounts(),
    timers: timerCount(),
  }
}

function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b)
}

export function leaksBetween(baseline, final) {
  const leaks = {}
  for (const cat of CATEGORIES) {
    if (!same(baseline[cat], final[cat])) {
      leaks[cat] = { baseline: baseline[cat], final: final[cat] }
    }
  }
  return leaks
}

// Wrap the server's registration methods so every handle the module obtains
// is recorded. The wrapper changes nothing about what a registration does.
function recordRegistrations(server, into) {
  const originals = {}
  for (const method of REGISTRATION_METHODS) {
    const original = server[method]
    if (typeof original !== 'function') continue
    originals[method] = original
    server[method] = function (...args) {
      const handle = original.apply(this, args)
      into.push({ method, name: String(args[0]), handle })
      return handle
    }
  }
  return () => {
    for (const [method, original] of Object.entries(originals)) server[method] = original
  }
}

// The SDK registers a capability (tools, resources, prompts) the first time
// something of that kind is registered, and refuses to do so once a transport
// is connected. A host that loads tool packs after it is serving has to open
// the three kinds up front. It does that here through the public API only: one
// registration of each kind, removed again before the transport connects, so
// the baseline lists are empty and every list method answers.
function openCapabilities(server) {
  const text = { content: [{ type: 'text', text: '' }] }
  server.registerTool('__host_init', { description: 'host init' }, async () => text).remove()
  server.registerResource('__host_init', 'bench://host-init', {},
    async () => ({ contents: [] })).remove()
  server.registerPrompt('__host_init', { description: 'host init' },
    () => ({ messages: [] })).remove()
}

export async function probe(install, { cycles = 5, config = {} } = {}) {
  const untrackTimers = trackTimers()
  const server = new McpServer({ name: 'framework-bench-host', version: '0.0.0' })
  openCapabilities(server)
  const client = new Client({ name: 'framework-bench-probe', version: '0.0.0' })
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await server.connect(serverSide)
  await client.connect(clientSide)

  const baseline = await snapshot(client)
  const perCycle = []
  let registered = 0
  try {
    for (let cycle = 0; cycle < cycles; cycle++) {
      const handles = []
      const restore = recordRegistrations(server, handles)
      let teardown
      try {
        teardown = await install(server, config)
      } finally {
        restore()
      }
      if (cycle === 0) registered = handles.length
      const mounted = await snapshot(client)
      for (const { handle } of handles) handle.remove()
      if (typeof teardown === 'function') await teardown()
      const after = await snapshot(client)
      perCycle.push({ cycle, registered: handles.length,
                      returnedTeardown: typeof teardown === 'function',
                      mounted, after })
    }
  } finally {
    await client.close()
    await server.close()
    untrackTimers()
  }
  const final = perCycle.length ? perCycle[perCycle.length - 1].after : baseline
  const leaks = leaksBetween(baseline, final)
  const leakedCategories = CATEGORIES.filter((c) => c in leaks)
  return {
    cycles,
    registered,
    returnedTeardown: perCycle.length ? perCycle[0].returnedTeardown : false,
    baseline,
    final,
    perCycle,
    leaks,
    leakedCategories,
    leaked: leakedCategories.length > 0,
  }
}

async function main() {
  const opts = parseArgs(process.argv.slice(2))
  const mod = await import(pathToFileURL(resolve(opts.module)).href)
  if (typeof mod.install !== 'function') {
    throw new Error(`module ${opts.module} has no exported function "install"`)
  }
  const report = await probe(mod.install, { cycles: opts.cycles, config: mod.config ?? {} })
  if (report.registered === 0) {
    throw new Error('install registered no tool, resource or prompt')
  }
  if (opts.json) {
    console.log(JSON.stringify(report, null, 2))
  } else {
    console.log(report.leaked
      ? `RESIDUE LEFT after ${report.cycles} cycles: ${report.leakedCategories.join(', ')}`
      : `no residue after ${report.cycles} cycles (${report.registered} registrations)`)
  }
  process.exit(report.leaked ? 1 : 0)
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  main().catch((err) => {
    console.error(`mcp-probe: ${err?.message ?? err}`)
    process.exit(2)
  })
}
