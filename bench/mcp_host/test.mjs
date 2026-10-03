// Self-check for the MCP host probe (`npm test`, or `node test.mjs`).
//
// Each fixture leaks exactly one category, or none, and the probe has to name
// exactly that category. A probe that reports every pack as clean, or every
// pack as leaking, fails here: the clean fixture pins the first direction and
// each leaky one pins the second for its own category.

import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import { probe } from './probe.mjs'

const EXPECTED = {
  'clean-kv.ts': [],
  'leaky-resource.ts': ['resources'],
  'leaky-listener.ts': ['listeners'],
  'leaky-timer.ts': ['timers'],
  'leaky-registry.ts': ['registry'],
}

let failures = 0
function check(name, ok, detail = '') {
  if (!ok) failures++
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? `: ${detail}` : ''}`)
}

console.log('mcp host probe self-check\n')
for (const [file, expected] of Object.entries(EXPECTED)) {
  const mod = await import(pathToFileURL(resolve('fixtures', file)).href)
  const report = await probe(mod.install, { cycles: 3 })
  const got = report.leakedCategories
  check(file, JSON.stringify(got) === JSON.stringify(expected),
    `expected [${expected}] got [${got}]`)
  check(`${file} registered something`, report.registered > 0,
    `${report.registered} registrations`)
}

const nothing = await import(pathToFileURL(resolve('fixtures', 'registers-nothing.ts')).href)
const empty = await probe(nothing.install, { cycles: 1 })
check('a pack that registers nothing is visible as such', empty.registered === 0)

console.log(failures ? `\n${failures} check(s) FAILED` : '\nall checks passed')
process.exit(failures ? 1 : 0)
