// revl cordis/TypeScript backend — host-builtin stub stdlib + adapter glue.
//
// The emitted modules import `host` from here.  Everything is deliberately
// observable: every host call is recorded into `hostLog` (and forwarded to
// subscribers) so the demo and the R1–R4 tests can assert ordering, and every
// acquired resource registers in `liveResources` so R4 (no-residue) can be
// asserted against the host as well as against cordis introspection.

import type { Context } from 'cordis'

// ---------------------------------------------------------------------------
// v2: realm placement (docs/design-v2-realms.md)
//
// cordis v4 compares isolate labels with `===`, and its public type for the
// label is `symbol`.  We therefore keep a process-wide string -> symbol
// registry: equal realm strings must resolve to one shared symbol, never to
// separately allocated `Symbol(name)` values (which would compare as distinct
// identities even though the strings match).

const realmLabels = new Map<string, symbol>()

/** Process-wide string -> label-symbol registry (equal strings share a realm). */
export function realmLabel(name: string): symbol {
  let label = realmLabels.get(name)
  if (!label) {
    label = Symbol(name)
    realmLabels.set(name, label)
  }
  return label
}

/** An emitted component, including the v2 `isolate` placement field. */
export interface RealmComponent {
  name?: string
  inject?: any
  provide?: string | string[]
  isolate?: Record<string, string>
  apply(ctx: Context, config?: any): any
}

/**
 * Load an emitted component honoring its realm placements: apply
 * `ctx.isolate(key, realmLabel(label))` per entry BEFORE `ctx.plugin` — the
 * fiber's context chain is fixed at plugin time, so isolation cannot happen
 * inside `apply`.
 */
export function plug(ctx: Context, component: RealmComponent, config?: any) {
  let scoped: Context = ctx
  for (const [key, realm] of Object.entries(component.isolate ?? {})) {
    scoped = scoped.isolate(key, realmLabel(realm))
  }
  return scoped.plugin(component, config)
}

// ---------------------------------------------------------------------------
// Observability

export const hostLog: string[] = []

const listeners = new Set<(entry: string) => void>()

/** Subscribe to host events (returns an unsubscribe function). */
export function onHostEvent(listener: (entry: string) => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Reset host state between tests. */
export function resetHost(): void {
  hostLog.length = 0
  liveResources.clear()
  poolCounter = 0
  mapCounter = 0
}

function record(entry: string): void {
  hostLog.push(entry)
  for (const listener of listeners) listener(entry)
}

/** Labels of currently-open host resources (empty ⇔ no residue, R4). */
export const liveResources = new Set<string>()

// ---------------------------------------------------------------------------
// Host builtins (docs/backend-ir.md §Host builtins)

let poolCounter = 0

export class PoolHandle {
  closed = false
  readonly statements: string[] = []
  readonly label: string
  readonly url: string
  readonly size: number

  constructor(url: string, size: number) {
    this.url = url
    this.size = size
    this.label = `pool#${++poolCounter}(${url})`
    liveResources.add(this.label)
    record(`${this.label}.open size=${size}`)
  }

  private assertOpen(op: string): void {
    if (this.closed) throw new Error(`${this.label}.${op} after close`)
  }

  query(sql: any): any[] {
    this.assertOpen('query')
    record(`${this.label}.query(${sql})`)
    this.statements.push(String(sql))
    return []
  }

  execute(sql: any): number {
    this.assertOpen('execute')
    record(`${this.label}.execute(${sql})`)
    this.statements.push(String(sql))
    return 1
  }

  close(): void {
    this.assertOpen('close')
    this.closed = true
    liveResources.delete(this.label)
    record(`${this.label}.close`)
  }
}

let mapCounter = 0

export class MapHandle {
  dropped = false
  readonly label: string
  private readonly data = new globalThis.Map<any, any>()

  constructor() {
    this.label = `map#${++mapCounter}`
    liveResources.add(this.label)
    record(`${this.label}.new`)
  }

  private assertLive(op: string): void {
    if (this.dropped) throw new Error(`${this.label}.${op} after drop`)
  }

  get(key: any): any {
    this.assertLive('get')
    record(`${this.label}.get(${key})`)
    return this.data.get(key) ?? null
  }

  insert(key: any, value: any): void {
    this.assertLive('insert')
    record(`${this.label}.insert(${key}, ${value})`)
    this.data.set(key, value)
  }

  remove(key: any): void {
    this.assertLive('remove')
    record(`${this.label}.remove(${key})`)
    this.data.delete(key)
  }

  get size(): number {
    return this.data.size
  }

  drop(): void {
    this.assertLive('drop')
    this.dropped = true
    this.data.clear()
    liveResources.delete(this.label)
    record(`${this.label}.drop`)
  }
}

// ---------------------------------------------------------------------------
// Adapter glue

export interface ConfigFieldSpec {
  required?: boolean
  default?: unknown
}

function applyConfigDefaults(
  component: string,
  raw: object | undefined,
  spec: Record<string, ConfigFieldSpec>,
): Record<string, unknown> {
  const config: Record<string, unknown> = {}
  for (const [field, fieldSpec] of Object.entries(spec)) {
    const value = (raw as Record<string, unknown> | undefined)?.[field]
    if (value !== undefined) {
      config[field] = value
    } else if (fieldSpec.required) {
      throw new TypeError(`${component}: missing required config field "${field}"`)
    } else {
      config[field] = fieldSpec.default
    }
  }
  return config
}

/** The namespace emitted code resolves `host.<fn>` calls against. */
export const host = {
  Pool: {
    open(url: any, size: any): PoolHandle {
      if (String(url).startsWith('boom://')) {
        // deliberate test hook: a refusing acquisition (IR v1/A8, L-Raise)
        record(`pool.open refused ${url}`)
        throw new Error(`refused to open ${url}`)
      }
      return new PoolHandle(String(url), Number(size))
    },
  },
  Map: {
    new(): MapHandle {
      return new MapHandle()
    },
  },
  Job: {
    // async host builtin (IR v1/A1): resolves on later ticks so `await`
    // steps have a real in-flight window for divert tests
    async run(name: any): Promise<string> {
      record(`job.run ${name} start`)
      for (let i = 0; i < 5; i++) await Promise.resolve()
      record(`job.run ${name} done`)
      return String(name)
    },
  },
  applyConfigDefaults,
}

// ---------------------------------------------------------------------------
// Runtime introspection (R4 — no-residue assertions)

export interface RuntimeSnapshot {
  /** Number of plugin runtimes registered. */
  registrySize: number
  /** Names of service impls currently in the reflect store. */
  serviceImpls: string[]
  /** Number of effects held by the root fiber. */
  rootEffects: number
  /** Event hooks with at least one listener, by event name. */
  hookCounts: Record<string, number>
  /** Open host resources (pools, maps). */
  liveHostResources: string[]
}

export function snapshotRuntime(ctx: Context): RuntimeSnapshot {
  const store = ctx.reflect.store as Record<PropertyKey, { name: string }>
  const hooks = (ctx.events as any)._hooks as Record<string, unknown[]>
  return {
    registrySize: ctx.registry.size,
    serviceImpls: Reflect.ownKeys(store)
      .map((key) => store[key].name)
      .sort(),
    rootEffects: ctx.fiber.getEffects().length,
    hookCounts: Object.fromEntries(
      Object.entries(hooks)
        .filter(([, list]) => list.length > 0)
        .map(([name, list]) => [name, list.length]),
    ),
    liveHostResources: [...liveResources].sort(),
  }
}

/** Throws unless the runtime looks exactly like `baseline` (R4). */
export function assertNoResidue(ctx: Context, baseline: RuntimeSnapshot): void {
  const now = snapshotRuntime(ctx)
  const before = JSON.stringify(baseline, null, 2)
  const after = JSON.stringify(now, null, 2)
  if (before !== after) {
    throw new Error(`residue detected (R4):\nbaseline ${before}\nafter ${after}`)
  }
}
