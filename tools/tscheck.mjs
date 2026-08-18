// Typecheck a batch of emitted .ts files, one program each, one node start.
//
// Each emitted case augments cordis's `Context` with its own service key, so
// putting them in a single program collides on the augmentation instead of on
// anything real — every case needs its own program. Spawning `tsc` 48 times
// would re-parse lib.d.ts 48 times; instead we keep one SourceFile cache
// across programs, which makes the extra programs nearly free.
//
// stdin:  {"dir": ..., "files": [...], "tsconfig": ...}
// stdout: {"case_0.ts": ["message", ...], ...}   (absent/empty = clean)

import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const input = JSON.parse(readFileSync(0, 'utf8'))
const configDir = path.dirname(path.resolve(input.tsconfig))
// typescript lives in the backend's node_modules, not next to this script.
const require = createRequire(path.join(configDir, 'package.json'))
const ts = require('typescript')
const raw = ts.readConfigFile(input.tsconfig, ts.sys.readFile)
if (raw.error) {
  console.error(ts.flattenDiagnosticMessageText(raw.error.messageText, ' '))
  process.exit(1)
}
const parsed = ts.parseJsonConfigFileContent(raw.config, ts.sys, configDir)
const options = { ...parsed.options, noEmit: true, skipLibCheck: true }

const host = ts.createCompilerHost(options, true)
const cache = new Map()
const original = host.getSourceFile.bind(host)
host.getSourceFile = (fileName, languageVersion, onError, shouldCreate) => {
  if (cache.has(fileName)) return cache.get(fileName)
  const file = original(fileName, languageVersion, onError, shouldCreate)
  cache.set(fileName, file)
  return file
}

const report = {}
for (const name of input.files) {
  const full = path.join(input.dir, name)
  // The case file itself changes per program; never serve a stale copy.
  cache.delete(full)
  const program = ts.createProgram([full], options, host)
  const source = program.getSourceFile(full)
  const diagnostics = [
    ...program.getSyntacticDiagnostics(source),
    ...program.getSemanticDiagnostics(source),
  ]
  // Errors in runtime.ts or in cordis's own types are not this case's fault.
  const mine = diagnostics.filter((d) => d.file && d.file.fileName === full)
  if (mine.length) {
    report[name] = mine.map((d) => {
      const text = ts.flattenDiagnosticMessageText(d.messageText, ' ')
      const { line } = d.file.getLineAndCharacterOfPosition(d.start ?? 0)
      return `TS${d.code} (line ${line + 1}): ${text}`
    })
  }
}

process.stdout.write(JSON.stringify(report))
