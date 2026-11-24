// Erase TypeScript from an emitted revl module (and the runtime it imports),
// so a browser can load it (issue #3294).
//
//     node scripts/strip-types.mjs <out-dir> <file.ts> [...]
//
// A browser has no type stripper, and node's own `.ts` support is not available
// in a page either, so an emitted module has to become JavaScript before it
// ships to one. An emitted module is strip-only TypeScript
// (docs/ts-runtime-contract.md, rule 3): erasing annotations and rewriting each
// relative `.ts` specifier to the JavaScript file the page can load is the
// whole job, and the TypeScript compiler this package already depends on does
// the erasing.
//
// Outputs are written FLAT into `<out-dir>`, one `.js` per input, and every
// relative `.ts` specifier is resolved against the input's own directory and
// rewritten to `./<basename>.js`. So
//
//     node scripts/strip-types.mjs /tmp/page tests/generated/conformance.ts runtime.ts
//
// yields `/tmp/page/conformance.js` and `/tmp/page/runtime.js`, and the emitted
// module's `import { host } from '../runtime.ts'` (the default
// `runtime_import`) becomes `./runtime.js`, which is where `runtime.js` is. A
// relative `.ts` specifier that resolves to a file the step was not given is an
// error, because the page could not load it either; pass that file too.
//
// Three things are refused by name rather than converted or left alone: an enum
// or a namespace that is not `declare`d, and a constructor parameter property
// (none is erasable, so the module is no longer strip-only); and a dynamic
// `import()` written with a template string (its specifier cannot be rewritten).
// A `declare`d enum or namespace is erased whole and is not refused.
//
// Nothing is bundled: a bare specifier such as `cordis` is left for the page's
// import map (docs/ts-runtime-contract.md, "The browser target").
//
// Run it from `backends/typescript` after `npm ci`. A module the contract
// allows to import a node builtin keeps that import and still will not load in
// a page, which is the contract's refusal, not this script's.

import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const [outDir, ...rawInputs] = process.argv.slice(2)
if (!outDir || rawInputs.length === 0) {
  console.error('usage: node scripts/strip-types.mjs <out-dir> <file.ts> [...]')
  process.exit(2)
}

const inputs = rawInputs.map((input) => path.resolve(input))
const outputs = new Map()
for (const input of inputs) {
  if (!input.endsWith('.ts')) {
    console.error(`${input}: the strip step takes .ts inputs`)
    process.exit(2)
  }
  const output = path.resolve(outDir, `${path.basename(input, '.ts')}.js`)
  if ([...outputs.values()].includes(output)) {
    console.error(`two inputs would both write ${output}; the outputs are flat, so their names must differ`)
    process.exit(2)
  }
  outputs.set(input, output)
}

fs.mkdirSync(outDir, { recursive: true })

// The one place a specifier is decided: relative `.ts` specifiers are resolved
// the way node resolves them from the input, then named by the output file
// they land on. Everything else (a bare `cordis`, a node builtin, an already
// `.js` sibling) is left exactly as the emitter wrote it.
function mappedSpecifier(specifier, fromFile) {
  if (!specifier.startsWith('.') || !specifier.endsWith('.ts')) return specifier
  const resolved = path.resolve(path.dirname(fromFile), specifier)
  const output = outputs.get(resolved)
  if (!output) {
    throw new Error(
      `${path.relative(process.cwd(), fromFile)} imports '${specifier}', which resolves to ` +
      `${path.relative(process.cwd(), resolved)}; that file is not one of the strip step's ` +
      'inputs, so the page could not load it. Pass it too.')
  }
  return `./${path.basename(output)}`
}

// Rewriting the JS text with a regex would also rewrite `from './x.ts'` inside
// a string literal or a comment. Parse the transpiled output instead and edit
// only the module-specifier nodes the parser reports, at their own offsets.
function rewriteSpecifiers(js, fromFile) {
  const source = ts.createSourceFile('output.js', js, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
  const edits = []
  const noteSpecifier = (literal) => {
    const next = mappedSpecifier(literal.text, fromFile)
    if (next === literal.text) return
    const start = literal.getStart(source)
    const end = literal.getEnd()
    const quote = js[start]
    edits.push({ start, end, text: `${quote}${next}${quote}` })
  }
  const walk = (node) => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
        node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      noteSpecifier(node.moduleSpecifier)
    }
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword &&
        node.arguments.length >= 1) {
      const specifier = node.arguments[0]
      if (ts.isStringLiteral(specifier)) {
        noteSpecifier(specifier)
      } else if (ts.isNoSubstitutionTemplateLiteral(specifier) || ts.isTemplateExpression(specifier)) {
        // A template string is not a specifier node this step can rewrite, and
        // leaving it would ship a `.ts` path the page cannot load.
        throw new Error(
          `${path.relative(process.cwd(), fromFile)}: a dynamic import written with a template ` +
          `string (${specifier.getText(source)}) cannot be rewritten; write the specifier as a ` +
          'plain string literal.')
      }
    }
    ts.forEachChild(node, walk)
  }
  walk(source)
  // Right to left, so an earlier edit cannot move a later offset.
  for (const edit of edits.sort((a, b) => b.start - a.start)) {
    js = js.slice(0, edit.start) + edit.text + js.slice(edit.end)
  }
  return js
}

// An emitted module is strip-only TypeScript (docs/ts-runtime-contract.md,
// rule 3): every type annotation can be deleted and nothing else changes.
// `ts.transpileModule` is more generous than that and silently turns an enum, a
// namespace or a parameter property into runtime code, which hides a module
// that is no longer strip-only. Refuse each by name instead.
function unerasable(text, fromFile) {
  const source = ts.createSourceFile(fromFile, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const found = []
  const note = (node, what, advice) => {
    const { line } = source.getLineAndCharacterOfPosition(node.getStart(source))
    found.push(`  line ${line + 1}: ${what} is not erasable ` +
      `TypeScript; ${advice}`)
  }
  const modifierFlags = ts.ModifierFlags.Public | ts.ModifierFlags.Private |
    ts.ModifierFlags.Protected | ts.ModifierFlags.Readonly | ts.ModifierFlags.Override
  const isDeclared = (node) =>
    (ts.getCombinedModifierFlags(node) & ts.ModifierFlags.Ambient) !== 0
  const walk = (node) => {
    // An ambient declaration (`declare enum`, `declare namespace A.B`,
    // `declare global`) is erased whole, and so is everything inside it, so the
    // subtree is not looked at.
    if ((ts.isEnumDeclaration(node) || ts.isModuleDeclaration(node)) && isDeclared(node)) return
    if (ts.isEnumDeclaration(node)) {
      note(node, `enum '${node.name.text}'`, 'use a const object and a union type.')
    } else if (ts.isModuleDeclaration(node)) {
      note(node, `namespace '${node.name.getText(source)}'`,
        'use a module, or declare it with `declare` if it only types an existing value.')
    } else if (ts.isParameter(node) && ts.isConstructorDeclaration(node.parent) &&
               (ts.getCombinedModifierFlags(node) & modifierFlags)) {
      note(node, `parameter property '${node.name.getText(source)}'`,
        'declare the field and assign it in the constructor body.')
    }
    ts.forEachChild(node, walk)
  }
  walk(source)
  return found
}

for (const input of inputs) {
  const source = fs.readFileSync(input, 'utf8')
  const refused = unerasable(source, input)
  if (refused.length > 0) {
    console.error(`${input}: the module is not strip-only TypeScript, so a page cannot run it as ` +
      `written:\n${refused.join('\n')}`)
    process.exit(1)
  }
  const { outputText, diagnostics } = ts.transpileModule(source, {
    fileName: path.basename(input),
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
      // Keep the emitted import graph exactly as the emitter wrote it.
      verbatimModuleSyntax: true,
    },
  })
  const errors = (diagnostics ?? []).filter((d) => d.category === ts.DiagnosticCategory.Error)
  if (errors.length > 0) {
    const rendered = errors.map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n'))
    console.error(`${input}: the emitter produced TypeScript the compiler refuses:\n${rendered.join('\n')}`)
    process.exit(1)
  }
  let output = outputText
  try {
    output = rewriteSpecifiers(output, input)
  } catch (error) {
    console.error(`${input}: ${error.message}`)
    process.exit(1)
  }
  const target = outputs.get(input)
  fs.writeFileSync(target, output)
  console.log(`${input} -> ${target}`)
}
