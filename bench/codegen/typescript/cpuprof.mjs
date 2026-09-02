// CPU-profile SAMPLE COUNTS attributed to emitted frames.
//
//   node bench/codegen/typescript/cpuprof.mjs
//
// Reports the SHARE of self-samples each function received, never a duration.
// A share is a ratio inside one profile, so it is far steadier under machine
// load than any elapsed time: if the process is descheduled, every function in
// it loses samples together and the shares are unchanged.
//
// This exists to answer "where does the emitted program actually spend its
// instructions", independently of the microtask counts in run.mjs.

import { Session } from 'node:inspector/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))

async function profile (label, fn) {
  const session = new Session()
  session.connect()
  await session.post('Profiler.enable')
  await session.post('Profiler.setSamplingInterval', { interval: 200 })
  await session.post('Profiler.start')
  await fn()
  const { profile: p } = await session.post('Profiler.stop')
  session.disconnect()

  const self = new Map()
  const byId = new Map(p.nodes.map((n) => [n.id, n]))
  for (const id of p.samples) {
    const n = byId.get(id)
    if (!n) continue
    const f = n.callFrame
    const where = f.url.includes('/emitted/') ? 'EMITTED'
        : f.url.includes('/cases/') ? 'hand'
        : f.url.includes('backends/typescript/runtime') ? 'runtime.ts'
        : f.url.includes('node_modules/cordis') ? 'cordis'
        : f.url.startsWith('node:') ? 'node' : 'other'
    const key = `${where}  ${f.functionName || '(anonymous)'}`
    self.set(key, (self.get(key) ?? 0) + 1)
  }
  const total = p.samples.length
  console.log(`\n## ${label}   ${total} samples`)
  for (const [k, v] of [...self.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12)) {
    console.log(`  ${String(v).padStart(7)}  ${((v / total) * 100).toFixed(1).padStart(5)}%  ${k}`)
  }
}

const scan = await import(join(HERE, 'cases', 'string_scan.ts'))
const S = 'ab1cd2ef3gh4'.repeat(60)
await profile('string scan, emitted', async () => {
  for (let i = 0; i < 30; i++) { scan.emittedProbeFn(S) }
})
await profile('string scan, hand-written', async () => {
  for (let i = 0; i < 30000; i++) { scan.handProbeFn(S) }
})

const match = await import(join(HERE, 'cases', 'match_sync_arms.ts'))
await profile('match with sync arms, emitted', async () => {
  for (let i = 0; i < 400000; i++) await match.emitted()
})
await profile('match with sync arms, hand-written', async () => {
  for (let i = 0; i < 400000; i++) await match.hand()
})
