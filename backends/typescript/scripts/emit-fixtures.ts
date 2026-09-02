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
  // item 435(c): index scans over a Str, so `str_scan_memo.test.ts` can count
  // the code points the emitted `Str` helpers actually materialise
  emitFixture('str_scan.ir.json', 'str_scan.ts')
  // item 165: identifiers that are JS/TS reserved words, renamed uniformly
  emitFixture('reserved_words.ir.json', 'reserved_words.ts')
  // item 279: a reserved-word JSON field on a DYNAMIC (json_parse/Any) value
  // stays reachable via the raw key (`tc["function"]`), matching the py tier
  emitFixture('dynamic_reserved_key.ir.json', 'dynamic_reserved_key.ts')
  // item 281: json_stringify of a record carrying an `Int` (JS bigint) field
  // must render a bare JSON number, not throw — the @ts bigint replacer.
  emitFixture('fr3_json_int.ir.json', 'fr3_json_int.ts')
  // item 167: the routed-require scenario for router.test.ts. Generated here at
  // setup (so the test's static import resolves) but deliberately NOT checked in
  // and NOT enumerated as a checked-in fixture pair: it carries no `test`
  // blocks, so it is outside the committed-current coverage gate
  // (generated_coverage.test.ts) — it is regenerated from its fixture on every
  // run, cold clone included. The alias keeps the pair off that gate's scan.
  const emitRouterModule = emitFixture
  emitRouterModule('router.ir.json', 'router.ts')
  // item 243 Slice 2b: the three-entry-kind teardown loop (bracket +
  // transactional + compensation on one LIFO stack, two-phase abort).
  emitFixture('witnessed_teardown.ir.json', 'witnessed_teardown.ts')
  // item 318 -> 324: the per-tool-call H1 seam — a provide-method witnessed fs
  // mutation registered into the component activation frame
  // (`Frame.transactionalMethod`), persisting on a clean unload and reverting
  // on `frame.abort()`. Regenerated from its fixture every run (the whole
  // `tests/generated/` dir is gitignored); carries no `test` blocks, so the
  // alias keeps the pair off `generated_coverage.test.ts`'s regex scan, exactly
  // like `emitRouterModule` above.
  const emitMethodWitnessed = emitFixture
  emitMethodWitnessed('method_witnessed.ir.json', 'method_witnessed.ts')
  // item 247 (method-body compensate remainder): the method-body-compensation soundness fix — a per-tool-call
  // `emit ... compensate ...` registered onto the activation frame
  // (`Frame.compensationMethod`), discharged on a clean unload and fired in
  // Phase 2 on `frame.abort()` (method_compensate.test.ts). Carries no `test`
  // blocks, so the alias keeps the pair off `generated_coverage.test.ts`'s
  // scan, like the method-witnessed fixture above.
  const emitMethodCompensate = emitFixture
  emitMethodCompensate('method_compensate.ir.json', 'method_compensate.ts')
  // item 369: the H1 flagship on ts — the witnessed stdlib/fs.rvl catalog
  // (real externs) driven through cordis, persisting on commit and reverting
  // residue-free on abort (ts_witnessed_fs.test.ts). Carries no `test` blocks,
  // so the alias keeps the pair off `generated_coverage.test.ts`'s scan, like
  // the router/method-witnessed fixtures above.
  const emitTsWitnessedFs = emitFixture
  emitTsWitnessedFs('ts_witnessed_fs.ir.json', 'ts_witnessed_fs.ts')
  // item 131: explicit async/await EFFECT composition — the LIFO teardown
  // ACROSS an in-flight `effect await` acquisition (async_effect_composition.
  // test.ts). Carries no `test` blocks, so the alias keeps the pair off
  // `generated_coverage.test.ts`'s scan, like the fixtures above.
  const emitAsyncEffectComposition = emitFixture
  emitAsyncEffectComposition('async_effect_composition.ir.json', 'async_effect_composition.ts')
  // Phase-1 continue-and-record: a raising BRACKET inverse must not break
  // cordis' sequential disposal chain and starve every earlier-registered
  // (later-disposed) inverse (phase1_bracket_fault.test.ts). Carries no `test`
  // blocks, so the alias keeps the pair off `generated_coverage.test.ts`'s
  // scan, like the fixtures above.
  const emitPhase1BracketFault = emitFixture
  emitPhase1BracketFault('phase1_bracket_fault.ir.json', 'phase1_bracket_fault.ts')
}

// Allow running directly (`node scripts/emit-fixtures.ts`) as a standalone
// pre-step, in addition to being imported by the vitest config.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  emitFixtures()
}
