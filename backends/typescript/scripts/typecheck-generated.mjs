// Typecheck every EMITTED module under `tests/generated/` (issue #198).
//
// `tsconfig.json` covered `runtime.ts`, `demo.ts` and `golden/**` — so the
// modules the vitest suites import and execute were compiled by nobody. This
// closes that: one `tsc` program per emitted module, options read from
// `tsconfig.generated.json` (see that file for why the settings and the
// one-program-per-module shape are what they are).
//
// Why a script rather than `tsc -p tsconfig.generated.json`: each emitted
// module augments cordis' global `Context`, so two modules that both provide a
// service named `db` cannot share a program without TypeScript merging their
// augmentations and resolving `ctx.db` to the wrong interface. `emit.py`
// produces a standalone program per IR module; this checks each one the way it
// is produced.
//
// Requires `tests/generated/` to be populated (it is gitignored, and written
// by `scripts/emit-fixtures.ts` at vitest config-load time). It does not
// regenerate: on a cold checkout it FAILS with the command to run, rather than
// checking zero files and reporting success. Silent-nothing is the failure
// mode this whole file exists to remove.

import { createRequire } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const ts = require('typescript')

const backend = dirname(dirname(fileURLToPath(import.meta.url)))
const generatedDir = join(backend, 'tests', 'generated')
const configPath = join(backend, 'tsconfig.generated.json')

function die(message) {
  console.error(`typecheck-generated: ${message}`)
  process.exit(1)
}

/** The output filenames `scripts/emit-fixtures.ts` writes, read out of that
 * source. This is the anti-vacuity guard: it is what makes "0 files checked,
 * all good" impossible. Matching on the CALL rather than on a name lets it see
 * the aliased calls (`emitRouterModule(...)`, `emitHostTraceSecret(...)`) the
 * fixture list uses to keep a module off `generated_coverage.test.ts`'s scan. */
function expectedModules() {
  const src = readFileSync(join(backend, 'scripts', 'emit-fixtures.ts'), 'utf-8')
  const calls = [
    ...src.matchAll(/\(\s*'([^']+\.ir\.json)'\s*,\s*'([^']+\.ts)'\s*\)/g),
  ].map((m) => m[2])
  if (calls.length === 0) {
    die(
      'no emitFixture(...) calls found in scripts/emit-fixtures.ts. Either the ' +
        'fixture list moved or its call shape changed; re-derive this guard ' +
        'against whatever replaced it rather than deleting it.',
    )
  }
  return [...new Set(calls)].sort()
}

const config = ts.getParsedCommandLineOfConfigFile(configPath, {}, {
  ...ts.sys,
  onUnRecoverableConfigFileDiagnostic: (d) =>
    die(ts.flattenDiagnosticMessageText(d.messageText, '\n')),
})
if (!config) die(`could not read ${relative(backend, configPath)}`)
if (config.errors.length > 0) {
  die(
    config.errors
      .map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n'))
      .join('\n'),
  )
}

const expected = expectedModules()
const missing = expected.filter((name) => !existsSync(join(generatedDir, name)))
if (missing.length > 0) {
  die(
    `tests/generated/ is missing ${missing.length} of ${expected.length} ` +
      `emitted modules (${missing.join(', ')}).\n` +
      '  That directory is gitignored and written by scripts/emit-fixtures.ts.\n' +
      '  Run `npx vitest run` (or `npm test`) first, then re-run this.',
  )
}

// The config's `include` glob and the fixture list must agree, or a module
// could be emitted, executed by vitest, and still checked by nobody — the
// exact gap issue #198 is about, reintroduced one file at a time.
const roots = config.fileNames.map((f) => resolve(f))
const uncovered = expected.filter(
  (name) => !roots.includes(resolve(join(generatedDir, name))),
)
if (uncovered.length > 0) {
  die(
    `${uncovered.join(', ')} is emitted into tests/generated/ but not matched ` +
      `by tsconfig.generated.json's include. Widen the include; do not drop ` +
      'the fixture.',
  )
}

const host = ts.createCompilerHost(config.options, true)
const formatHost = {
  getCanonicalFileName: (f) => f,
  getCurrentDirectory: () => backend,
  getNewLine: () => ts.sys.newLine,
}

let failed = 0
for (const root of roots) {
  const program = ts.createProgram({
    rootNames: [root],
    options: config.options,
    host,
  })
  const diagnostics = [
    ...program.getSyntacticDiagnostics(),
    ...program.getSemanticDiagnostics(),
    ...program.getGlobalDiagnostics(),
  ]
  if (diagnostics.length > 0) {
    failed += 1
    process.stderr.write(
      ts.formatDiagnosticsWithColorAndContext(diagnostics, formatHost),
    )
  }
}

if (failed > 0) {
  die(
    `${failed} of ${roots.length} emitted module(s) do not typecheck. These are ` +
      'emitter defects unless proven otherwise — fix the emitter or the ' +
      'runtime, not this gate.',
  )
}

console.log(
  `typecheck-generated: ${roots.length} emitted modules typecheck ` +
    `(${expected.length} from the fixture list, one program each).`,
)
