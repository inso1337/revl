// Driver for the TypeScript codegen benchmarks.
//
//   node --expose-gc bench/codegen/typescript/run.mjs
//   node --expose-gc bench/codegen/typescript/run.mjs --case match-sync-arms-in-async-fn
//   node --expose-gc bench/codegen/typescript/run.mjs --timing        # QUIET MACHINE ONLY
//
// DEFAULT OUTPUT CARRIES NO DURATIONS. Every number printed without --timing
// is a COUNT: microtask turns, Promise allocations, code points materialised.
// Counts are deterministic and do not move when the machine is busy, which is
// what makes them safe to act on. `--timing` adds an interleaved A/B ratio and
// is meaningful only on an otherwise idle machine; it prints a banner saying so.
//
// Every case is checked against `emitted/` BEFORE it is measured. If the
// fragment the hand version was written against is no longer in the compiler's
// output, the case is reported STALE and skipped, so a re-emit that changes the
// shape fails loudly instead of quietly measuring code the emitter no longer
// produces.
//
// Requires node >= 22.18 (native TypeScript type stripping) and `cordis`
// resolvable from the repo root. See README.md.

import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { promiseAllocs, microtaskTurns, ratio, fmtRatio } from './lib/measure.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const argv = process.argv.slice(2)
const only = argv.includes('--case') ? argv[argv.indexOf('--case') + 1] : null
const withTiming = argv.includes('--timing')

function checkProvenance (mod) {
  for (const p of mod.provenance ?? []) {
    const text = readFileSync(join(HERE, 'emitted', p.file), 'utf8')
    if (!text.includes(p.snippet)) {
      return `emitted/${p.file} no longer contains: ${p.snippet.slice(0, 70)}`
    }
  }
  return null
}

async function counts (fn) {
  return { turns: await microtaskTurns(fn), promises: await promiseAllocs(fn) }
}

function per (n, ops) { return ops ? (n / ops).toFixed(1) : String(n) }

const files = readdirSync(join(HERE, 'cases')).filter((f) => f.endsWith('.ts')).sort()
console.log(`node ${process.version}   cases: ${files.length}`)
console.log('all default numbers are COUNTS, not durations: they do not move with machine load')
if (withTiming) {
  console.log('\n!! --timing: the ratios below are only meaningful on an IDLE machine.')
  console.log('!! Do not act on them from a box running other agents.')
}

for (const file of files) {
  const mod = await import(join(HERE, 'cases', file))
  if (only && mod.name !== only) continue
  const stale = checkProvenance(mod)
  if (stale) { console.log(`\n## ${mod.name}\n  STALE: ${stale}`); continue }
  if (mod.setup) await mod.setup()
  if (mod.check) await mod.check()

  const ops = mod.opsPerRun ?? 0
  const rows = [['emitted', await counts(mod.emitted)]]
  if (mod.handMemo) rows.push([mod.handMemoLabel ?? 'hand-memo', await counts(mod.handMemo)])
  rows.push(['hand', await counts(mod.hand)])

  console.log(`\n## ${mod.name}`)
  console.log(`  ${mod.summary}`)
  const unit = ops ? ' per logical op' : ' per run'
  for (const [label, c] of rows) {
    console.log(`  ${label.padEnd(11)} microtask turns ${per(c.turns, ops).padStart(9)}` +
                `   promise allocs ${per(c.promises, ops).padStart(9)}${unit}`)
  }
  const [, base] = rows[0]
  const [, best] = rows[rows.length - 1]
  console.log(`  EXCESS vs hand: ${base.turns - best.turns} microtask turns, ` +
              `${base.promises - best.promises} promise allocations` +
              (ops ? ` (${per(base.turns - best.turns, ops)} / ${per(base.promises - best.promises, ops)} per op)` : ''))
  if (mod.shape) {
    console.log(`  shape metric (read off the emitted text, not executed): ` +
                `emitted ${mod.shape.emitted.closuresPerCall} closures / ` +
                `${mod.shape.emitted.awaitsPerCall} awaits per call, hand ` +
                `${mod.shape.hand.closuresPerCall} / ${mod.shape.hand.awaitsPerCall}`)
  }
  if (mod.effectCalls) console.log(`  ${await mod.effectCalls()}`)

  if (withTiming && mod.emittedHot && mod.handHot) {
    console.log(`  [idle-machine only] emitted/hand: ${fmtRatio(await ratio(mod.emittedHot, mod.handHot))}`)
    if (mod.handMemoHot) {
      console.log(`  [idle-machine only] emitted/hand-memo: ${fmtRatio(await ratio(mod.emittedHot, mod.handMemoHot))}`)
    }
  }
}

// --- allocation-complexity probe for the string case -----------------------
// Exact and load-independent: intercept Array.from and total the code points it
// materialises. Growth SHAPE against input length is the whole finding.
const scan = await import(join(HERE, 'cases', 'string_scan.ts'))
if (!only || only === scan.name) {
  console.log('\n## string-scan allocation complexity: code points materialised by Array.from')
  console.log('  (count_digits over an n-code-point ASCII string; exact counts, no timing)')
  console.log('  n           emitted        memo         hand')
  const realFrom = Array.from
  let cps = 0
  const counting = function (...args) { const r = realFrom.apply(Array, args); cps += r.length; return r }
  for (const n of scan.sizes) {
    const s = 'ab1cd2ef3gh4'.repeat(Math.ceil(n / 12)).slice(0, n)
    const row = []
    for (const fn of [scan.emittedProbeFn, scan.memoProbeFn, scan.handProbeFn]) {
      cps = 0
      Array.from = counting
      try { fn(s) } finally { Array.from = realFrom }
      row.push(cps)
    }
    console.log(`  ${String(n).padEnd(8)}${row.map((c) => String(c).padStart(13)).join('')}`)
  }
  console.log('  emitted grows as 2n^2 + n; memo and hand are n. Quadratic in the input length.')
}

// --- element-copy probe for the list-build case ----------------------------
const lb = await import(join(HERE, 'cases', 'list_build.ts'))
if (!only || only === lb.name) {
  console.log('\n## list-build element copies (exact counts, no timing)')
  console.log('  n         emitted         hand')
  for (const n of lb.sizes) {
    const e = lb.copiedElements(lb.emittedBuildFn, n)
    const h = lb.copiedElements(lb.handBuildFn, n)
    console.log(`  ${String(n).padEnd(8)}${String(e).padStart(9)}${String(h).padStart(13)}`)
  }
  console.log('  emitted grows as n(n-1)/2; hand copies nothing. Quadratic in the list length.')
}
