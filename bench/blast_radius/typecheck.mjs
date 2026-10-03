// Type-check every TypeScript rendering under bench/blast_radius/ts/, each as
// its own program, with the options the ts tier checks its emitted modules
// under (backends/typescript/tsconfig.generated.json).
//
// The renderings live outside the ts tier, so `cordis` and `@types/node` are
// resolved from backends/typescript/node_modules (run `npm ci` there first).
// Exits non-zero on any diagnostic, and on finding no rendering at all.
//
//   node bench/blast_radius/typecheck.mjs [file.ts ...]

import { existsSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const backend = resolve(here, '..', '..', 'backends', 'typescript')
const modules = join(backend, 'node_modules')
const GATE = 'typecheck-bench'

if (!existsSync(join(modules, 'typescript')) || !existsSync(join(modules, 'cordis'))) {
  console.error(`${GATE}: ${modules} has no typescript/cordis; run \`npm ci\` in backends/typescript`)
  process.exit(1)
}

const { checkEachAsItsOwnProgram, loadConfig } = await import(
  join(backend, 'scripts', 'typecheck-lib.mjs'))

const config = loadConfig(GATE, join(backend, 'tsconfig.generated.json'), backend)
const options = {
  ...config.options,
  paths: { cordis: [join(modules, 'cordis')] },
  typeRoots: [join(modules, '@types')],
}

const args = process.argv.slice(2)
const tsDir = join(here, 'ts')
const roots = args.length > 0
  ? args.map((f) => resolve(f))
  : readdirSync(tsDir).filter((f) => f.endsWith('.ts')).sort().map((f) => join(tsDir, f))
if (roots.length === 0) {
  console.error(`${GATE}: no renderings found under ${tsDir}`)
  process.exit(1)
}
const { failed, programs } = checkEachAsItsOwnProgram(roots, options, backend)
console.log(`${GATE}: ${programs} rendering(s) checked, ${failed} with errors`)
process.exit(failed === 0 ? 0 : 1)
