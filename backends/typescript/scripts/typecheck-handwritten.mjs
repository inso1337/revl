// Typecheck every HAND-WRITTEN module in this tier (issue #223).
//
// #198 put the emitted output under a gate. This is the other half: the vitest
// suites and the hand-written sources beside them — `bridge.ts`,
// `revl_fs_ts.ts`, `placement_runner.ts` and the rest — which were in no
// tsconfig at all. See `tsconfig.handwritten.json` for the file set and for why
// each file is checked as its own program.
//
// Two guards, because a typecheck gate's real failure mode is not reporting a
// wrong error, it is reporting nothing:
//
// 1. COVERAGE. Every `.ts` on disk under `backends/typescript/` (bar
//    `node_modules/`) must be a root of `tsconfig.json`,
//    `tsconfig.generated.json` or `tsconfig.handwritten.json`. A file matched
//    by none of the three is a hard failure naming it. This is the guard that
//    closes issue #223 for good: it is not possible to add a `.ts` here and
//    have it checked by nobody, and it is not possible to quietly narrow an
//    `include` back down.
// 2. COLD CHECKOUT. The suites import `tests/generated/*.ts`, which is
//    gitignored and written by `scripts/emit-fixtures.ts` at vitest
//    config-load. With that directory cold every import would fail as TS2307,
//    which is a confusing way to say "run the tests first" — so say it.
//
// Run it as `node scripts/typecheck-handwritten.mjs`, after `npx vitest run`.

import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  allTypeScriptFiles,
  checkEachAsItsOwnProgram,
  die as sharedDie,
  loadConfig,
} from './typecheck-lib.mjs'

const GATE = 'typecheck-handwritten'
const die = (message) => sharedDie(GATE, message)

const backend = dirname(dirname(fileURLToPath(import.meta.url)))
const configPath = join(backend, 'tsconfig.handwritten.json')

// Every config that claims coverage of part of this tier. Guard 1 is the union
// of their roots; add a config here the moment you add one, or its files will
// read as uncovered.
const CONFIGS = [
  'tsconfig.json',
  'tsconfig.generated.json',
  'tsconfig.handwritten.json',
]

const config = loadConfig(GATE, configPath, backend)
const roots = config.fileNames.map((f) => resolve(f))

if (roots.length === 0) {
  die(
    "tsconfig.handwritten.json's include matched no files. Nothing below is a " +
      'claim about anything — re-point the include at the hand-written sources ' +
      'rather than letting the gate pass on an empty set (issue #223).',
  )
}

// --- guard 2: the emitted modules the suites import must be on disk --------
// Derived from what the hand-written files actually import, rather than from
// the fixture list `typecheck-generated.mjs` reads: this asks the narrower
// question "can the files I am about to check resolve their imports", and it
// keeps working if the fixture list is reorganised.
const generatedImports = new Set()
for (const root of roots) {
  const src = readFileSync(root, 'utf-8')
  for (const m of src.matchAll(/from\s+'(\.\.?\/[^']*generated\/[^']+\.ts)'/g)) {
    generatedImports.add(resolve(dirname(root), m[1]))
  }
}
const coldImports = [...generatedImports].filter((f) => !existsSync(f)).sort()
if (coldImports.length > 0) {
  die(
    `${coldImports.length} emitted module(s) the suites import are not on ` +
      `disk (${coldImports.map((f) => relative(backend, f)).join(', ')}).\n` +
      '  tests/generated/ is gitignored and written by scripts/emit-fixtures.ts.\n' +
      '  Run `npx vitest run` (or `npm test`) first, then re-run this.',
  )
}

// --- guard 1: no `.ts` in this tier is checked by nobody -------------------
const covered = new Set()
for (const name of CONFIGS) {
  const path = join(backend, name)
  if (!existsSync(path)) {
    die(
      `${name} is gone, so this gate can no longer tell whether the files it ` +
        'covered are checked by anybody. Re-derive the list in CONFIGS against ' +
        'whatever replaced it.',
    )
  }
  for (const f of loadConfig(GATE, path, backend).fileNames) covered.add(resolve(f))
}

const uncovered = allTypeScriptFiles(backend)
  .map((f) => resolve(f))
  .filter((f) => !covered.has(f))
  .map((f) => relative(backend, f))
  .sort()
if (uncovered.length > 0) {
  die(
    `${uncovered.length} TypeScript file(s) in this tier are matched by no ` +
      `tsconfig, so nothing typechecks them:\n` +
      uncovered.map((f) => `    ${f}`).join('\n') +
      '\n  Add each to the include of the config that should own it ' +
      `(${CONFIGS.join(', ')}). Excluding one needs a named reason and an ` +
      'issue — that gap is what #223 was.',
  )
}

const { failed, programs } = checkEachAsItsOwnProgram(
  roots, config.options, backend)

if (failed > 0) {
  die(
    `${failed} of ${programs} hand-written module(s) do not typecheck. ` +
      'Fix the source (or the emitter, if a call site is right and the ' +
      'emitted signature is wrong); do not narrow this gate.',
  )
}

console.log(
  `typecheck-handwritten: ${programs} hand-written modules typecheck ` +
    `(one program each), and all ${covered.size} .ts files in this tier are ` +
    'covered by a tsconfig.',
)
