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

import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { Context } from 'cordis'

import { makeProxy, serve } from './bridge.ts'
import { assertNoResidue, fiberStateName, snapshotRuntime } from './runtime.ts'

const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const name: string = spec.name

// item 396 option B: a `@ts ref` thunk resolves its host module at call time
// through `globalThis.__REVL_REF_ROOT__` joined with the recorded relative path
// (so the emitted artifact carries no machine path). Set it BEFORE the module is
// imported, and HASH-CHECK each ref's file against the IR pin before any host
// code can run — the ts twin of the py driver's plug-time refusal.
;(globalThis as any).__REVL_REF_ROOT__ = spec.refRoot ?? ''
// item 410: the SECOND root a stdlib-origin `@ts ref` resolves against — the
// install tree. The runner LIVES at backends/typescript/placement_runner.ts, and
// in both supported layouts (source checkout, installed wheel) the install root
// is exactly two directories up, so we self-derive it when the spec omits the
// key. This makes multi-process placement work without threading the stdlib root
// through placement.py's spec, and never falls back to the user root.
const _stdlibRefRoot: string =
  spec.stdlibRefRoot ?? path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
;(globalThis as any).__REVL_STDLIB_REF_ROOT__ = _stdlibRefRoot
for (const ref of (spec.refs || []) as Array<{ extern: string; path: string; sha256: string; root?: string }>) {
  // per-kind: a stdlib ref hash-checks against the install root, a user ref
  // against the user root. No cross-domain fallback — the two roots are the two
  // trust domains (item 410 invariants 1 and 2).
  const base = ref.root === 'stdlib' ? _stdlibRefRoot : (spec.refRoot ?? '')
  const abs = path.resolve(base, ref.path)
  let got: string
  try {
    got = crypto.createHash('sha256').update(fs.readFileSync(abs)).digest('hex')
  } catch (e) {
    throw new Error(`revl @ts ref for extern \`${ref.extern}\`: cannot read ${abs} (${e})`)
  }
  if (got !== ref.sha256)
    throw new Error(
      `revl @ts host-module ref for extern \`${ref.extern}\` does not match the ` +
      `file pinned at compile: expected sha256 ${ref.sha256} for ${ref.path}, ` +
      `but ${abs} hashes ${got} (item 396 option B / 410 deploy contract)`,
    )
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
  log('fiber', fiber.name, `${fiberStateName(oldState)} -> ${fiberStateName(fiber.state)}`),
)

// once mode: `revl run --backend ts --once` drives the boot -> LIFO teardown
// -> no-residue proof round-trip and exits; a placement spec never sets this,
// so the hold-until-stopped behavior below is unchanged for placements.
const once: boolean = spec.once === true
// the no-residue proof is a diff against the pre-load runtime: registry,
// reflect, root effects, event hooks, and host resources (runtime.ts R4).
const baseline = snapshotRuntime(ctx)

const fibers: Array<[string, any]> = []

// 1. proxies for keys provided by other processes. A seam is local (a UDS
//    `socket`) or a network TCP+mTLS `endpoint` (item 56/149): the node client
//    dials both. `deadline` (seconds; src/revl/placement.py) bounds each call —
//    a wedged provider breaches a SeamDeadline rather than blocking the consumer.
for (const [key, info] of Object.entries<any>(spec.proxies || {})) {
  const target = info.endpoint ?? info.socket
  const deadlineMs = info.deadline != null ? info.deadline * 1000 : null
  // item 118: the correlation envelope this consumer stamps on every crossing,
  // when placement.py shipped one for this seam (its own peer identity + the
  // composition, plus a per-boot secret on a LOCAL seam; a network seam carries
  // no secret because the mTLS handshake already bound the identity). Absent,
  // the request line is byte-identical to the pre-118 wire.
  const { component, onPeerLost } = makeProxy(
    key, info.methods, target, deadlineMs, info.correlation ?? null,
  )
  const fiber = ctx.plugin(component)
  await fiber
  fibers.push([`${key}-proxy`, fiber])
  onPeerLost(() => void fiber.dispose())
  const where = info.endpoint ? `tcp://${info.endpoint.host}:${info.endpoint.port}` : info.socket
  log('proxy', key, `-> ${where}`)
}

// 2. this process's own components, in IR load order
for (const cname of spec.components as string[]) {
  const config = (spec.config || {})[cname]
  const fiber = config ? ctx.plugin(mod[cname], config) : ctx.plugin(mod[cname])
  await fiber
  fibers.push([cname, fiber])
  log('load', cname, `state=${fiberStateName(fiber.state)}`)
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

// 5. teardown, consumers first (reverse load order — the same contract the py
//    driver's _dispose_all and the rust runner's teardown enforce). In once
//    mode the runner then proves no residue against the pre-load snapshot and
//    exits; otherwise it holds until the conductor stops us.
let stopping = false
async function teardown(): Promise<void> {
  if (stopping) return
  stopping = true
  for (const [label, fiber] of [...fibers].reverse()) {
    try {
      await fiber.dispose()
    } catch {
      /* best-effort */
    }
    log('swap', label, 'dispose')
  }
  if (server) server.close()
  if (once) {
    // no-residue proof (the cordis-ts mirror of the rust runner's once mode:
    // registry().len()==0 / reflect().services().len()==0, and of the py
    // driver's registry.size==0 / reflect.store=={} check): after a LIFO
    // teardown the live runtime must match its pre-load snapshot — nothing
    // left in the registry, nothing in reflect, no effects/hooks/resources.
    const now = snapshotRuntime(ctx)
    log('residue', 'registry', `${now.registrySize} live plugin(s)`)
    log('residue', 'provisions', `${now.serviceImpls.length} service(s) provided`)
    try {
      assertNoResidue(ctx, baseline)
      console.log(`[${name}] NO-RESIDUE — the composition left nothing behind`)
    } catch (error) {
      console.log(`[${name}] RESIDUE-LEFT — ${String(error).split('\n')[0]}`)
    }
  }
  console.log(`[${name}] DOWN`)
  process.exit(0)
}

if (once) {
  await teardown()
} else {
  process.on('SIGTERM', teardown)
  process.on('SIGINT', teardown)
  const keepAlive = setInterval(() => {}, 1 << 30) // hold the event loop
  void keepAlive
}
