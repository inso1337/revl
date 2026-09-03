// Shared machinery for this tier's typecheck gates.
//
// `typecheck-generated.mjs` (issue #198) and `typecheck-handwritten.mjs`
// (issue #223) cover different halves of the tier and guard themselves against
// different vacuity failures, but they build programs the same way and for the
// same reason: every module that augments cordis' global `Context`
// (`declare module 'cordis' { interface Context { db: Database } }`) has to be
// checked in a program of its own, or two modules that provide the same
// service name merge their augmentations and TypeScript starts reporting
// TS2717 — and then resolving `ctx.db` to the WRONG interface — about code
// that is fine. That shape lives here once rather than twice.

import { createRequire } from 'node:module'
import { readdirSync } from 'node:fs'
import { join, relative } from 'node:path'

const require = createRequire(import.meta.url)
export const ts = require('typescript')

/** Fail the gate. Every caller exits non-zero: a typecheck gate that reports a
 * problem and returns 0 is the failure mode both of these scripts exist to
 * remove. */
export function die(gate, message) {
  console.error(`${gate}: ${message}`)
  process.exit(1)
}

/** The parsed `tsconfig` at `configPath`: compiler options plus the resolved
 * file list its `include` matches. */
export function loadConfig(gate, configPath, backend) {
  const config = ts.getParsedCommandLineOfConfigFile(configPath, {}, {
    ...ts.sys,
    onUnRecoverableConfigFileDiagnostic: (d) =>
      die(gate, ts.flattenDiagnosticMessageText(d.messageText, '\n')),
  })
  if (!config) die(gate, `could not read ${relative(backend, configPath)}`)
  if (config.errors.length > 0) {
    die(
      gate,
      config.errors
        .map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n'))
        .join('\n'),
    )
  }
  return config
}

/** Typecheck each root as its OWN program — see the header for why they cannot
 * share one. Prints every diagnostic; returns the number of roots that had at
 * least one, alongside the number of programs actually built.
 *
 * A `.d.ts` among the roots is AMBIENT, not a program: it declares types for
 * something else (`golden/temporal-sdk.d.ts` declares `@temporalio/workflow`,
 * which `golden/temporal_booktrip.ts` imports) and carries no code of its own.
 * Splitting the roots one-per-program would otherwise strand every declaration
 * file away from the module that needs it, which reads as TS2307 "cannot find
 * module" — a gate artifact, not a defect. So declaration files join every
 * program instead of forming one. */
export function checkEachAsItsOwnProgram(roots, options, backend) {
  const ambient = roots.filter((f) => f.endsWith('.d.ts'))
  const modules = roots.filter((f) => !f.endsWith('.d.ts'))
  const host = ts.createCompilerHost(options, true)
  const formatHost = {
    getCanonicalFileName: (f) => f,
    getCurrentDirectory: () => backend,
    getNewLine: () => ts.sys.newLine,
  }
  let failed = 0
  for (const root of modules) {
    const program = ts.createProgram({
      rootNames: [root, ...ambient],
      options,
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
  return { failed, programs: modules.length, ambient: ambient.length }
}

/** Every `.ts`/`.d.ts` file under `dir`, recursively, skipping `node_modules`.
 * This is the disk-side half of the coverage guard in
 * `typecheck-handwritten.mjs`: a config's `include` can only be checked against
 * something that does not come from a config. */
export function allTypeScriptFiles(dir, found = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue
    const full = join(dir, entry.name)
    if (entry.isDirectory()) allTypeScriptFiles(full, found)
    else if (entry.name.endsWith('.ts')) found.push(full)
  }
  return found
}
