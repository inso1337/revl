// residue-probe CLI — point the no-residue contract at any Cordis (TS) plugin.
//
//   node run.mjs <module> [export] [--config file.json] [--cycles N] [--warmup K] [--json]
//
//   <module>   path to a module exporting a Cordis plugin (.ts or .js)
//   [export]   named export to probe (default: "plugin")
//   --config   JSON file (or inline JSON) passed as the plugin's config
//   --cycles   mount/unmount cycles to measure (default 5)
//   --warmup   un-measured cycles before baseline (default 0 == run.py-strict)
//   --json     emit the machine-readable ProbeReport instead of the text report
//
// Exit code is 0 when nothing leaked, 1 when any category left residue — so a
// later bench (roadmap item 20) can consume this directly as a pass/fail oracle.

import { ensureDeps } from './ensure-deps.mjs'
import { pathToFileURL } from 'node:url'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

function parseArgs(argv) {
  const positional = []
  const opts = { export: 'plugin', cycles: 5, warmup: 0, json: false, config: undefined }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--json') opts.json = true
    else if (a === '--cycles') opts.cycles = Number(argv[++i])
    else if (a === '--warmup') opts.warmup = Number(argv[++i])
    else if (a === '--config') opts.config = argv[++i]
    else if (a.startsWith('--')) throw new Error(`unknown flag ${a}`)
    else positional.push(a)
  }
  if (positional.length < 1) {
    throw new Error(
      'usage: node run.mjs <module> [export] [--config f.json] [--cycles N] [--warmup K] [--json]',
    )
  }
  opts.module = positional[0]
  if (positional[1]) opts.export = positional[1]
  return opts
}

function loadConfig(spec) {
  if (spec === undefined) return undefined
  const text = spec.trimStart().startsWith('{') ? spec : readFileSync(spec, 'utf8')
  return JSON.parse(text)
}

async function main() {
  const opts = parseArgs(process.argv.slice(2))
  ensureDeps()
  const { probe, formatReport } = await import('./probe.ts')

  const mod = await import(pathToFileURL(resolve(opts.module)).href)
  const component = mod[opts.export]
  if (!component) {
    throw new Error(`module ${opts.module} has no export "${opts.export}"`)
  }
  const config = opts.config !== undefined ? loadConfig(opts.config) : mod.config

  const report = await probe(component, config, { cycles: opts.cycles, warmup: opts.warmup })
  if (opts.json) {
    console.log(JSON.stringify(report, null, 2))
  } else {
    console.log(formatReport(report))
  }
  process.exit(report.leaked ? 1 : 0)
}

main().catch((err) => {
  console.error(`residue-probe: ${err.message ?? err}`)
  process.exit(2)
})
