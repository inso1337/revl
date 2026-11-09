// Erase TypeScript from an emitted revl module (and the runtime it imports),
// so a browser can load it (issue #3294).
//
//     node scripts/strip-types.mjs <out-dir> <file.ts> [...]
//
// A browser has no type stripper, and node's own `.ts` support is not available
// in a page either, so an emitted module has to become JavaScript before it
// ships to one. An emitted module is strip-only TypeScript
// (docs/ts-runtime-contract.md, rule 3): erasing annotations and rewriting each
// relative `./x.ts` specifier to `./x.js` is the whole job, and the TypeScript
// compiler this package already depends on does the erasing.
//
// Output is `<out-dir>/<basename>.js`, so
//
//     node scripts/strip-types.mjs /tmp/page tests/generated/conformance.ts runtime.ts
//
// yields `/tmp/page/conformance.js` and `/tmp/page/runtime.js`, and the emitted
// module's `import { host } from '../runtime.ts'` now names `../runtime.js`.
// Nothing is bundled: a bare specifier such as `cordis` is left for the page's
// import map (docs/ts-runtime-contract.md, "The browser target").
//
// Run it from `backends/typescript` after `npm ci`. The output is plain ESM at
// the target's own import graph; a module the contract allows to import
// `node:fs` keeps that import and still will not load in a page, which is the
// contract's refusal, not this script's.

import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const [outDir, ...inputs] = process.argv.slice(2)
if (!outDir || inputs.length === 0) {
  console.error('usage: node scripts/strip-types.mjs <out-dir> <file.ts> [...]')
  process.exit(2)
}

// A sibling module the emitted module names with its real extension (contract
// rule 2). The compiler has already erased `import type` lines by the time
// this runs, so a bare `cordis` is not touched.
const STATIC = /(\bfrom\s+)(['"])(\.[^'"]*?)\.ts\2/g
const DYNAMIC = /(\bimport\s*\(\s*)(['"])(\.[^'"]*?)\.ts\2/g
const SIDE_EFFECT = /(\bimport\s+)(['"])(\.[^'"]*?)\.ts\2/g

function toJsSpecifiers(js) {
  const rewrite = (_match, lead, quote, specifier) => `${lead}${quote}${specifier}.js${quote}`
  return js.replace(STATIC, rewrite).replace(DYNAMIC, rewrite).replace(SIDE_EFFECT, rewrite)
}

fs.mkdirSync(outDir, { recursive: true })
for (const input of inputs) {
  const source = fs.readFileSync(input, 'utf8')
  const { outputText, diagnostics } = ts.transpileModule(source, {
    fileName: path.basename(input),
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
      // Keep the emitted import graph exactly as the emitter wrote it. The
      // page resolves the same specifiers the node tier does, only with `.js`
      // where the contract requires `.ts`.
      verbatimModuleSyntax: true,
    },
  })
  const errors = (diagnostics ?? []).filter((d) => d.category === ts.DiagnosticCategory.Error)
  if (errors.length > 0) {
    const rendered = errors.map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n'))
    console.error(`${input}: the emitter produced TypeScript the compiler refuses:\n${rendered.join('\n')}`)
    process.exit(1)
  }
  const output = path.join(outDir, path.basename(input).replace(/\.ts$/, '.js'))
  fs.writeFileSync(output, toJsSpecifiers(outputText))
  console.log(`${input} -> ${output}`)
}
