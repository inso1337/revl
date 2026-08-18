// Emit TypeScript modules for the test fixtures so the suites can import them
// statically.
//
// This MUST run before vitest resolves its `include` glob, otherwise a
// generated *test* file (`tests/generated/v3_tests.test.ts`) is invisible on a
// cold checkout where `tests/generated/` does not exist yet: the glob is
// resolved once, up front, and a file written later by `globalSetup` is never
// collected.  `vitest.config.ts` imports this module and calls `emitFixtures()`
// at load time — which vitest evaluates before collection — so the file is on
// disk in time.  See the timeout note in `vitest.config.ts` for the nested
// `vitest run` that re-runs this.
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

export function emitFixtures(): void {
  emitFixture('user_cache.ir.json', 'user_cache.ts')
  emitFixture('r3_migrator.ir.json', 'r3_migrator.ts')
  emitFixture('tenants.ir.json', 'tenants.ts')
  emitFixture('v3_types_functions.ir.json', 'v3_types_functions.ts')
  emitFixture('conformance.ir.json', 'conformance.ts')
  emitFixture('v3_stdlib.ir.json', 'v3_stdlib.ts')
  emitFixture('v3_tests.ir.json', 'v3_tests.test.ts')
}

// Allow running directly (`node scripts/emit-fixtures.ts`) as a standalone
// pre-step, in addition to being imported by the vitest config.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  emitFixtures()
}
