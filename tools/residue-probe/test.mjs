// Runnable check for residue-probe (invoked by `npm test`). Self-contained: no
// vitest, no extra install — just Node + the shared cordis install. Asserts the
// probe distinguishes leak from no-leak.
//
//   clean fixture  -> 0 leaks, exit-code intent 0
//   leaky fixture  -> leaks in registry + provisions + effects + listeners
//
// Also runs the probe against a plugin already in the tree — the emitted
// golden UserCache (backends/typescript/golden/user_cache.ts) — proving a
// revl-authored TS plugin is clean under the same harness.

import { ensureDeps } from './ensure-deps.mjs'

ensureDeps()
const { probe } = await import('./probe.ts')

let failures = 0
function check(name, cond, detail = '') {
  const ok = !!cond
  if (!ok) failures++
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

function sameSet(a, b) {
  const A = new Set(a)
  return a.length === b.length && b.every((x) => A.has(x))
}

console.log('residue-probe self-check\n')

// --- clean fixture: 0 leaks ------------------------------------------------
{
  const { plugin, config } = await import('./fixtures/clean-plugin.ts')
  const r = await probe(plugin, config, { cycles: 5 })
  check('clean fixture reports NO leak', r.leaked === false, r.leakedCategories.join(', ') || 'none')
  check('clean fixture leaked 0 categories', r.leakedCategories.length === 0)
}

// --- leaky fixture: the expected nonzero leak set --------------------------
{
  const { plugin, config } = await import('./fixtures/leaky-plugin.ts')
  const r = await probe(plugin, config, { cycles: 5 })
  const expected = ['registry', 'provisions', 'effects', 'listeners']
  check('leaky fixture reports a leak', r.leaked === true)
  check(
    `leaky fixture leaks exactly {${expected.join(', ')}}`,
    sameSet(r.leakedCategories, expected),
    `got {${r.leakedCategories.join(', ')}}`,
  )
  check(
    'leaky provisions include the escaped service',
    r.leaks.provisions.detail.includes('leaked-svc'),
    r.leaks.provisions.detail,
  )
  // listeners + effects grow every cycle: unbounded, not a fixed offset
  const firstHooks = r.perCycle[0].hookCounts['internal/info'] ?? 0
  const lastHooks = r.perCycle[r.perCycle.length - 1].hookCounts['internal/info'] ?? 0
  check('leaky listener count grows per cycle (unbounded)', lastHooks > firstHooks, `${firstHooks} -> ${lastHooks}`)
}

// --- a plugin already in the tree: the emitted golden UserCache ------------
try {
  const golden = await import('../../backends/typescript/golden/user_cache.ts')
  const r = await probe(golden.UserCache, undefined, { cycles: 3 })
  // UserCache injects `db`; with no provider its fiber stays PENDING and never
  // activates — the point being it still leaves NOTHING behind across cycles.
  check('in-tree golden UserCache leaves no residue', r.leaked === false, r.leakedCategories.join(', ') || 'none')
} catch (e) {
  check('in-tree golden UserCache probed', false, String(e.message ?? e))
}

console.log(`\n${failures === 0 ? 'all checks passed' : `${failures} check(s) FAILED`}`)
process.exit(failures === 0 ? 0 : 1)
