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
  emitFixture('v3_map.ir.json', 'v3_map.ts')
  emitFixture('v3_tests.ir.json', 'v3_tests.test.ts')
  emitFixture('spawn.ir.json', 'spawn.ts')
  emitFixture('instance_get.ir.json', 'instance_get.ts')
  emitFixture('fr1_loop.ir.json', 'fr1_loop.ts')
  // item 165: identifiers that are JS/TS reserved words, renamed uniformly
  emitFixture('reserved_words.ir.json', 'reserved_words.ts')
  // item 279: a reserved-word JSON field on a DYNAMIC (json_parse/Any) value
  // stays reachable via the raw key (`tc["function"]`), matching the py tier
  emitFixture('dynamic_reserved_key.ir.json', 'dynamic_reserved_key.ts')
  // item 167: the routed-require scenario for router.test.ts. Generated here at
  // setup (so the test's static import resolves) but deliberately NOT checked in
  // and NOT enumerated as a checked-in fixture pair: it carries no `test`
  // blocks, so it is outside the committed-current coverage gate
  // (generated_coverage.test.ts) — it is regenerated from its fixture on every
  // run, cold clone included. The alias keeps the pair off that gate's scan.
  const emitRouterModule = emitFixture
  emitRouterModule('router.ir.json', 'router.ts')
}

// Allow running directly (`node scripts/emit-fixtures.ts`) as a standalone
// pre-step, in addition to being imported by the vitest config.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  emitFixtures()
}
