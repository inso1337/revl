// py<->node interop bridge demo (docs/interop-bridge.md §3, headline milestone).
//
// One composition, two languages, two OS processes:
//   provider (Python):  PgDatabase, provides `db`   (demo/bridge_pypy.py --provider)
//   consumer (Node):    UserCache on cordis, requires `db` through a bridge proxy
//
// Both run the SAME service contract from user_cache.rvl; only the wire is
// shared. Proves, across the language boundary:
//   1. cache.put's `emit db.execute(...)` crosses from Node into the Python
//      provider's real pool (transport + value-copy marshalling, JSON wire);
//   2. a Python-provided revl service is consumed by a Node cordis component
//      unchanged;
//   3. peer death is withdrawal: killing the Python provider deactivates the
//      Node UserCache reactively, replaying its inverses (R2/R3), no exception.
//
// Run under the TS backend (needs `npm install` here and the cordis-py venv):
//   node backends/typescript/bridge_node.ts
// Exits nonzero on any failed check.

import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { Context, FiberState } from 'cordis'

import { makeProxy } from './bridge.ts'
import { UserCache } from './golden/user_cache.ts'
import { hostLog } from './runtime.ts'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..', '..')
const VENV_PY = path.join(REPO, 'backends', 'python', '.venv', 'bin', 'python')
const PROVIDER = path.join(REPO, 'demo', 'bridge_pypy.py')

const sock = `/tmp/revl_pynode_${process.pid}.sock`
const trace = `/tmp/revl_pynode_${process.pid}.trace`
fs.writeFileSync(trace, '')

let failures = 0
function check(name: string, ok: boolean, detail = ''): void {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(22)} ${detail}`)
  if (!ok) failures++
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

// --- bring up the Python provider (PgDatabase serving `db`) ----------------
const provider = spawn(VENV_PY, [PROVIDER, '--provider', sock, trace], {
  stdio: ['ignore', 'pipe', 'pipe'],
})
let providerErr = ''
provider.stderr.on('data', (d) => (providerErr += d))
await new Promise<void>((resolve, reject) => {
  let out = ''
  provider.stdout.on('data', (d) => {
    out += d
    if (out.includes('READY')) resolve()
  })
  provider.on('exit', () => reject(new Error(`provider exited before READY:\n${providerErr}`)))
  setTimeout(() => reject(new Error('provider start timeout')), 15000)
})
console.log('== provider up (PgDatabase serving `db` in a Python process) ==')

// --- Node consumer: UserCache on cordis, `db` via the cross-language proxy --
const ctx = new Context()
const transitions: string[] = []
ctx.on('internal/status', (fiber, oldState) =>
  transitions.push(`${fiber.name}:${oldState}->${fiber.state}`),
)

const { component: dbProxy, onPeerLost } = makeProxy('db', ['query', 'execute'], sock)
const proxyFiber = ctx.plugin(dbProxy)
await proxyFiber
const cacheFiber = ctx.plugin(UserCache)
await cacheFiber
onPeerLost(() => void proxyFiber.dispose())

check('consumer-active', cacheFiber.state === FiberState.ACTIVE,
  'Node UserCache ACTIVE against a Python-provided `db`')

;(ctx as any).cache.put('alice', '42')
const got = (ctx as any).cache.get('alice')
check('local-read', got === '42', `cache.get("alice") -> ${JSON.stringify(got)}`)

await sleep(200) // let the provider flush its trace file
const provTrace = fs.readFileSync(trace, 'utf8')
check('crossed-emission', provTrace.includes('cache_log') && provTrace.includes('.execute'),
  'Python provider pool.execute recorded the emit that crossed from Node')

// --- peer death is withdrawal ----------------------------------------------
console.log('== killing the Python provider ==')
provider.kill('SIGTERM')
for (let i = 0; i < 80 && cacheFiber.state === FiberState.ACTIVE; i++) await sleep(50)

check('peer-death-withdrawal', cacheFiber.state !== FiberState.ACTIVE,
  `Node UserCache deactivated when the Python provider died (state=${cacheFiber.state})`)
check('reactive-deactivation',
  cacheFiber.state === FiberState.PENDING ||
    transitions.some((t) => t.startsWith('UserCache:') && t.endsWith(`->${FiberState.UNLOADING}`)),
  'UserCache deactivated via provider withdrawal (R2), not by a thrown error')
const inverses = hostLog.filter((e) => e.includes('.remove(') || e.includes('.drop'))
check('lifo-inverses', inverses.length > 0, `consumer inverses replayed: ${JSON.stringify(inverses)}`)

for (const stale of [sock, trace]) {
  try {
    fs.unlinkSync(stale)
  } catch {
    /* already gone */
  }
}
console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) FAILED`)
process.exit(failures === 0 ? 0 : 1)
