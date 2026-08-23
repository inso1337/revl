// residue-probe — a standalone lifecycle linter for foreign Cordis (TS) plugins.
//
// `revl run` proves *no residue* on exit for revl-authored compositions: after
// teardown it asserts that four observable categories of the Cordis runtime are
// back to the pre-load baseline (src/revl/run.py, `_Driver._teardown`):
//
//     run.py category   run.py expression                         probe here
//     ----------------  ----------------------------------------  --------------------------
//     registry          root.registry.size == 0                   ctx.registry.size
//     provisions        root.reflect.store == {}                  ctx.reflect.store (names)
//     effects           root.fiber._disposables.length == base    ctx.fiber.getEffects().length
//     listeners         root.events._hooks == baseline            ctx.events._hooks
//
// The py driver is the *model*. This is new TS work pointing the same contract
// at plugins revl did NOT author: mount → unmount ANY Cordis plugin N cycles
// and report which of those four categories did not return to baseline. The
// field access matches backends/typescript/runtime.ts `snapshotRuntime`, the
// TS backend's own R4 (no-residue) oracle — so the mapping to the frozen
// contract is exactly the one the emitted-code tests already trust.
//
// Runs on the real cordis runtime (backends/typescript/node_modules). No build
// step: Node >= 23.6 erasable-syntax TypeScript, same as demo.ts.

import { Context } from 'cordis'

/** A Cordis component (plugin). Structural — matches what `ctx.plugin` accepts. */
export interface CordisPlugin {
  name?: string
  inject?: unknown
  provide?: string | string[]
  apply(ctx: Context, config?: unknown): unknown
  // functional plugins are also accepted by cordis; probe() handles both.
  (ctx: Context, config?: unknown): unknown
}

/** The four categories run.py proves return to baseline, snapshotted off the
 *  live root Context. Deliberately identical in shape to the cordis half of
 *  backends/typescript/runtime.ts `RuntimeSnapshot` (host resources omitted:
 *  a foreign plugin does not touch revl's host stdlib). */
export interface Snapshot {
  /** plugin runtimes registered (run.py: registry.size) */
  registrySize: number
  /** service impls in the reflect store, by name (run.py: reflect.store) */
  serviceImpls: string[]
  /** effects held by the root fiber (run.py: fiber._disposables.length) */
  rootEffects: number
  /** event hooks with >=1 listener, by event name (run.py: events._hooks) */
  hookCounts: Record<string, number>
}

export type Category = 'registry' | 'provisions' | 'effects' | 'listeners'
export const CATEGORIES: Category[] = ['registry', 'provisions', 'effects', 'listeners']

/** Snapshot the four observable categories off a root Context. */
export function snapshot(ctx: Context): Snapshot {
  const store = ctx.reflect.store as Record<PropertyKey, { name: string } | undefined>
  const hooks = (ctx.events as unknown as { _hooks: Record<string, unknown[]> })._hooks
  return {
    registrySize: ctx.registry.size,
    serviceImpls: Reflect.ownKeys(store)
      .map((key) => store[key]?.name)
      .filter((n): n is string => typeof n === 'string')
      .sort(),
    rootEffects: ctx.fiber.getEffects().length,
    hookCounts: Object.fromEntries(
      Object.entries(hooks)
        .filter(([, list]) => list.length > 0)
        .map(([name, list]) => [name, list.length]),
    ),
  }
}

/** Per-category leak: what did not return to baseline, expressed as the delta. */
export interface CategoryLeak {
  leaked: boolean
  detail: string
}

export interface LeakReport {
  registry: CategoryLeak
  provisions: CategoryLeak
  effects: CategoryLeak
  listeners: CategoryLeak
}

function countDelta(base: Record<string, number>, cur: Record<string, number>): Record<string, number> {
  const delta: Record<string, number> = {}
  for (const key of new Set([...Object.keys(base), ...Object.keys(cur)])) {
    const d = (cur[key] ?? 0) - (base[key] ?? 0)
    if (d !== 0) delta[key] = d
  }
  return delta
}

/** Diff two snapshots into a per-category leak report (cur vs baseline). */
export function diffSnapshots(base: Snapshot, cur: Snapshot): LeakReport {
  const regDelta = cur.registrySize - base.registrySize
  const effDelta = cur.rootEffects - base.rootEffects
  const added = cur.serviceImpls.filter((n) => !base.serviceImpls.includes(n))
  const removed = base.serviceImpls.filter((n) => !cur.serviceImpls.includes(n))
  const hookDelta = countDelta(base.hookCounts, cur.hookCounts)
  return {
    registry: {
      leaked: regDelta !== 0,
      detail:
        regDelta === 0
          ? `registry.size back to baseline (${base.registrySize})`
          : `registry.size ${cur.registrySize} (baseline ${base.registrySize}, +${regDelta})`,
    },
    provisions: {
      leaked: added.length > 0 || removed.length > 0,
      detail:
        added.length || removed.length
          ? `reflect.store diff: ${[...added.map((n) => `+${n}`), ...removed.map((n) => `-${n}`)].join(', ')}`
          : 'reflect.store back to baseline',
    },
    effects: {
      leaked: effDelta !== 0,
      detail:
        effDelta === 0
          ? `root fiber effects back to baseline (${base.rootEffects})`
          : `root fiber effects ${cur.rootEffects} (baseline ${base.rootEffects}, +${effDelta})`,
    },
    listeners: {
      leaked: Object.keys(hookDelta).length > 0,
      detail: Object.keys(hookDelta).length
        ? `events._hooks diff: ${Object.entries(hookDelta)
            .map(([n, d]) => `${n} ${d > 0 ? '+' : ''}${d}`)
            .join(', ')}`
        : 'event hooks back to baseline',
    },
  }
}

export interface ProbeOptions {
  /** mount/unmount cycles to run and measure (default 5). */
  cycles?: number
  /** warmup cycles run BEFORE the baseline snapshot. 0 (default) mirrors
   *  run.py exactly: baseline is taken before the very first mount, so a
   *  one-time first-mount offset counts as residue. Set >0 to tolerate a fixed
   *  first-mount cost and flag only per-cycle *growth*. */
  warmup?: number
}

export interface ProbeReport {
  component: string
  cycles: number
  warmup: number
  baseline: Snapshot
  final: Snapshot
  /** snapshot after each measured cycle — lets a bench separate a fixed
   *  offset from unbounded per-cycle growth. */
  perCycle: Snapshot[]
  leaks: LeakReport
  leakedCategories: Category[]
  leaked: boolean
}

// Let cordis settle its async lifecycle work (mirrors run.py `_flush`: it drains
// the microtask queue, plus a couple of macrotask turns for timer-backed work).
async function flush(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
  await new Promise((r) => setTimeout(r, 0))
}

async function oneCycle(ctx: Context, component: CordisPlugin, config: unknown): Promise<void> {
  const fiber = ctx.plugin(component as never, config as never)
  try {
    await fiber
  } catch {
    /* a plugin whose apply rejects still gets torn down below */
  }
  await flush()
  await fiber.dispose()
  await flush()
}

/**
 * Mount → unmount `component` for `cycles` (after `warmup` un-measured cycles),
 * on a fresh root Context, and report which of the four contract categories did
 * not return to baseline. This is the foreign-plugin analogue of run.py's
 * baseline proof, run N times.
 */
export async function probe(
  component: CordisPlugin,
  config: unknown,
  options: ProbeOptions = {},
): Promise<ProbeReport> {
  const cycles = options.cycles ?? 5
  const warmup = options.warmup ?? 0
  const ctx = new Context()

  for (let i = 0; i < warmup; i++) await oneCycle(ctx, component, config)

  // Baseline AFTER warmup (0 warmup == run.py: baseline before first mount).
  const baseline = snapshot(ctx)
  const perCycle: Snapshot[] = []
  for (let i = 0; i < cycles; i++) {
    await oneCycle(ctx, component, config)
    perCycle.push(snapshot(ctx))
  }
  const final = perCycle[perCycle.length - 1] ?? baseline

  const leaks = diffSnapshots(baseline, final)
  const leakedCategories = CATEGORIES.filter((c) => leaks[c].leaked)
  return {
    component: component.name ?? '(anonymous plugin)',
    cycles,
    warmup,
    baseline,
    final,
    perCycle,
    leaks,
    leakedCategories,
    leaked: leakedCategories.length > 0,
  }
}

/** Human-readable report (the CLI prints this; exit code is separate). */
export function formatReport(r: ProbeReport): string {
  const lines: string[] = []
  lines.push(`residue-probe — ${r.component}`)
  lines.push(`  ${r.cycles} mount/unmount cycle(s)${r.warmup ? `, ${r.warmup} warmup` : ''}`)
  lines.push('  contract: registry / provisions / effects / listeners back to baseline (revl run.py)')
  lines.push('')
  for (const cat of CATEGORIES) {
    const c = r.leaks[cat]
    lines.push(`  [${c.leaked ? 'LEAK' : ' ok '}] ${cat.padEnd(10)} ${c.detail}`)
  }
  lines.push('')
  if (r.leaked) {
    lines.push(`  RESIDUE LEFT — leaked: ${r.leakedCategories.join(', ')}`)
  } else {
    lines.push('  no residue — the plugin left nothing behind across all cycles')
  }
  return lines.join('\n')
}
