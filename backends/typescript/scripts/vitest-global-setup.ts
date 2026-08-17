// Vitest global setup: emit TypeScript modules for the test fixtures so the
// suites can import them statically.  Runs before any test file is collected.
import { spawnSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const backend = dirname(dirname(fileURLToPath(import.meta.url)))

function emitFixture(fixture: string, outName: string): void {
  const result = spawnSync(
    'python3',
    ['emit.py', '--runtime', '../../runtime.ts', join('tests', 'fixtures', fixture)],
    { cwd: backend, encoding: 'utf-8' },
  )
  if (result.status !== 0) {
    throw new Error(`emit.py failed for ${fixture}:\n${result.stderr}`)
  }
  mkdirSync(join(backend, 'tests', 'generated'), { recursive: true })
  writeFileSync(join(backend, 'tests', 'generated', outName), result.stdout)
}

export default function setup(): void {
  emitFixture('user_cache.ir.json', 'user_cache.ts')
  emitFixture('r3_migrator.ir.json', 'r3_migrator.ts')
  emitFixture('tenants.ir.json', 'tenants.ts')
  emitFixture('v3_types_functions.ir.json', 'v3_types_functions.ts')
  emitFixture('v3_tests.ir.json', 'v3_tests.test.ts')
}
