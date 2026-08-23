// revl cordis/TypeScript backend — host stdlib + adapter glue.
//
// The emitted modules import `host` from here.  Everything is deliberately
// observable: every host call is recorded into `hostLog` (and forwarded to
// subscribers) so the demo and the R1–R4 tests can assert ordering, and every
// acquired resource registers in `liveResources` so R4 (no-residue) can be
// asserted against the host as well as against cordis introspection.
//
// `Pool` and `Job` are NOT placeholders: `Pool` is a real bounded connection
// pool over a deterministic fake database and `Job` is a real cancellable
// asynchronous unit of work.  Their semantics are defined once, for every
// tier, in backends/python/runtime.py under ".. _pool-job-semantics:" — this
// file implements exactly that state machine (same errors, same trace
// strings, same tick count).  Change that text first, then all four tiers.

import type { Context } from 'cordis'
import type { Fiber } from 'cordis'

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
// instance-parametric components (docs/design-v2-instances.md, phase 2)
//
// A `spawn` acquisition instantiates a component at RUNTIME as a child fiber of
// its spawner. This mirrors the cordis-py reference (backends/python/runtime.py
// `spawn` / `SpawnHandle`) on cordis v4:
//
//   - each key the target provides is isolated into a *fresh LOCAL realm* — an
//     unlabelled `ctx.isolate(key)`, which mints `Symbol(key)` per call
//     (node_modules/cordis/lib/index.js:1417), a distinct identity every spawn.
//     Two instances of one component therefore never collide on a provision
//     (disjoint by construction; no config value known at link time).
//   - the instance is plugged as a *child fiber* of the spawner's context
//     (`scoped.plugin`, registry.d.ts:57 -> `Fiber & PromiseLike<Fiber>`), i.e.
//     its own nested teardown scope, NOT an effect adopted flatly into the
//     spawner's fiber accumulator. Disposing the handle unloads that child now.

/** The value a `spawn` acquisition binds: a live component instance torn down
 * by its own `dispose()`. Because the instance is a child fiber, `dispose()`
 * runs its LIFO teardown *now*, independent of the spawner — a request-scoped
 * instance is reclaimed when the request ends, never deferred to the spawner's
 * teardown. Disposal is idempotent, so the spawner's own inverse
 * (`yield () => s.dispose()`) is a harmless no-op once the instance is gone. */
export class SpawnHandle {
  private disposed = false
  private readonly fiber: Fiber
  readonly component?: string

  // Explicit field assignment, not constructor parameter-properties: Node's
  // native type-stripping (used by tests/test_realm_conformance.py) only
  // erases annotations, it cannot transform the `private`-in-constructor
  // shorthand — which would make importing this runtime fail under `node`.
  constructor(fiber: Fiber, component?: string) {
    this.fiber = fiber
    this.component = component
  }

  /** Unload the instance's fiber (its LIFO teardown). Returns cordis'
   * `Promise<void>` so a caller in an async context can `await` reclamation;
   * the emitted inverse is drained through cordis' disposer protocol, which
   * already awaits a returned promise. */
  dispose(): Promise<void> | void {
    if (this.disposed) return
    this.disposed = true
    return this.fiber.dispose()
  }

  /** Read a provision the instance published, in *its* local realm. Only the
   * spawner (which holds this handle) can reach it — a sibling instance,
   * isolated into a different local realm, cannot (supervision-tree
   * addressing, decision 1/2). */
  get(key: string): any {
    return (this.fiber.ctx as any)[key]
  }

  /** Live-fiber introspection for tests/harness (never named by emitted revl):
   * the instance's fiber state and its committed context. */
  get state() {
    return this.fiber.state
  }

  get ctx(): Context {
    return this.fiber.ctx
  }
}

/** Instantiate `component` at runtime as a child of the spawner, each provided
 * key isolated into a fresh LOCAL realm. Returns a {@link SpawnHandle}. */
export function spawn(
  ctx: Context,
  component: RealmComponent,
  config: any,
  realms: string[],
): SpawnHandle {
  let scoped: Context = ctx
  for (const key of realms ?? []) {
    scoped = scoped.isolate(key) // no label -> a fresh local realm per spawn
  }
  const fiber = scoped.plugin(component, config)
  return new SpawnHandle(fiber, component.name)
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
  jobCounter = 0
  jobHandles.length = 0
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

/** A bounded connection pool (see backends/python/runtime.py,
 * ".. _pool-job-semantics:").  Real capacity accounting over a
 * deterministic fake database — no driver dependency. */
export class PoolHandle {
  closed = false
  readonly statements: string[] = []
  readonly label: string
  readonly url: string
  readonly size: number
  /** idle connection ids, lowest first (determinism) */
  private idle: number[]
  private checkedOut: number[] = []

  constructor(url: string, size: number) {
    this.url = url
    this.size = size
    this.idle = Array.from({ length: size }, (_, i) => i + 1)
    this.label = `pool#${++poolCounter}(${url})`
    liveResources.add(this.label)
    record(`${this.label}.open size=${size}`)
  }

  private assertOpen(op: string): void {
    if (this.closed) throw new Error(`${this.label}.${op} after close`)
  }

  /** Borrow a connection for the duration of one statement (silent: only an
   * explicit acquire/release is traced, so existing traces are unchanged). */
  private borrow(op: string): number {
    this.assertOpen(op)
    if (this.idle.length === 0) {
      throw new Error(
        `${this.label}.${op} exhausted (size=${this.size}, in_use=${this.checkedOut.length})`,
      )
    }
    const conn = this.idle.shift() as number
    this.checkedOut.push(conn)
    return conn
  }

  private giveBack(conn: number): void {
    this.checkedOut.splice(this.checkedOut.indexOf(conn), 1)
    this.idle.push(conn)
    this.idle.sort((a, b) => a - b)
  }

  capacity(): number {
    return this.closed ? 0 : this.size
  }

  inUse(): number {
    return this.checkedOut.length
  }

  available(): number {
    return this.idle.length
  }

  /** Check out the lowest-numbered idle connection; throws when exhausted. */
  acquire(): number {
    const conn = this.borrow('acquire')
    record(`${this.label}.acquire conn=${conn} ${this.checkedOut.length}/${this.size}`)
    return conn
  }

  release(conn: number): void {
    this.assertOpen('release')
    if (!this.checkedOut.includes(conn)) {
      throw new Error(`${this.label}.release conn=${conn} is not checked out`)
    }
    this.giveBack(conn)
    record(`${this.label}.release conn=${conn} ${this.checkedOut.length}/${this.size}`)
  }

  query(sql: any): any[] {
    const conn = this.borrow('query')
    try {
      record(`${this.label}.query(${sql})`)
      this.statements.push(String(sql))
      return []
    } finally {
      this.giveBack(conn)
    }
  }

  /** Rows affected. This is the one documented host builtin whose result a
   * revl program reads as an `Int` (`emission fn execute(sql: Str) -> Int` in
   * examples/user_cache.rvl), and `Int` is 64-bit — `bigint` on this tier
   * (docs/arithmetic.md). `acquire`/`capacity`/`inUse`/`available` stay
   * `number`: they are harness introspection, not part of the v0 host stdlib
   * surface in docs/backend-ir.md, and no revl program sees them. */
  execute(sql: any): bigint {
    const conn = this.borrow('execute')
    try {
      record(`${this.label}.execute(${sql})`)
      this.statements.push(String(sql))
      return 1n
    } finally {
      this.giveBack(conn)
    }
  }

  close(): void {
    this.assertOpen('close')
    this.checkedOut.length = 0 // a close actually releases
    this.idle.length = 0
    this.closed = true
    liveResources.delete(this.label)
    record(`${this.label}.close`)
  }
}

// ---------------------------------------------------------------------------
// Job — a cancellable asynchronous unit of work (see the semantics block in
// backends/python/runtime.py).  Deterministic: exactly JOB_TICKS microtask
// turns of simulated work, never a timer.

export const JOB_TICKS = 5

export type JobState = 'pending' | 'done' | 'cancelled'

export class JobCancelledError extends Error {}

let jobCounter = 0
const jobHandles: JobHandle[] = []

export class JobHandle implements PromiseLike<string> {
  readonly name: string
  readonly serial: number
  private status: JobState = 'pending'
  private remainingTicks = JOB_TICKS
  private driven: Promise<string> | undefined

  constructor(name: string) {
    this.name = name
    this.serial = ++jobCounter
    jobHandles.push(this)
    record(`job.run ${name} start`)
  }

  state(): JobState {
    return this.status
  }

  get remaining(): number {
    return this.remainingTicks
  }

  /** pending -> cancelled (true); a no-op returning false otherwise. */
  cancel(): boolean {
    if (this.status !== 'pending') return false
    this.status = 'cancelled'
    record(`job.run ${this.name} cancelled`)
    return true
  }

  private async drive(): Promise<string> {
    if (this.status === 'done') return this.name
    if (this.status === 'cancelled') {
      throw new JobCancelledError(`job "${this.name}" cancelled`)
    }
    while (this.remainingTicks > 0) {
      await Promise.resolve()
      // `state()` (not `this.status`) — cancel() may have run during the
      // await, which control-flow narrowing cannot see.
      if (this.state() === 'cancelled') {
        throw new JobCancelledError(`job "${this.name}" cancelled`)
      }
      this.remainingTicks--
    }
    this.status = 'done'
    record(`job.run ${this.name} done`)
    return this.name
  }

  // Thenable, not an eagerly-started Promise: the work begins on the first
  // `await`, matching the lazy tiers (python/rust) tick-for-tick.
  then<T1 = string, T2 = never>(
    onfulfilled?: ((value: string) => T1 | PromiseLike<T1>) | null,
    onrejected?: ((reason: any) => T2 | PromiseLike<T2>) | null,
  ): PromiseLike<T1 | T2> {
    if (!this.driven) this.driven = this.drive()
    return this.driven.then(onfulfilled, onrejected)
  }
}

/** Handles still in flight — a teardown that abandons a job leaves this > 0. */
export function pendingJobs(): number {
  return jobHandles.filter((job) => job.state() === 'pending').length
}

export function jobHandleList(): JobHandle[] {
  return [...jobHandles]
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

/** component name -> the configuration it last actually ran with, after
 * defaults.  The trace event `<Component>.config {...}` carries the same
 * information; this is the queryable form. */
export const resolvedConfig = new globalThis.Map<string, Record<string, unknown>>()

function applyConfigDefaults(
  component: string,
  raw: object | undefined,
  spec: Record<string, ConfigFieldSpec>,
): Record<string, unknown> {
  const config: Record<string, unknown> = {}
  const defaulted: string[] = []
  for (const [field, fieldSpec] of Object.entries(spec)) {
    const value = (raw as Record<string, unknown> | undefined)?.[field]
    if (value !== undefined) {
      config[field] = value
    } else if (fieldSpec.required) {
      throw new TypeError(`${component}: missing required config field "${field}"`)
    } else {
      config[field] = fieldSpec.default
      defaulted.push(field)
    }
  }
  resolvedConfig.set(component, config)
  // `JSON.stringify` THROWS on a BigInt, and an `Int` config field is a bigint
  // on this tier — so the trace line has to render one itself. Digits with no
  // `n` suffix and no quotes: the same text python/rust/java/go write for the
  // same value, which is what keeps the cross-tier trace comparison honest.
  const show = (value: unknown) =>
    typeof value === 'bigint' ? value.toString() : JSON.stringify(value)
  const body = Object.keys(config)
    .sort()
    .map((key) => `${key}=${show(config[key])}`)
    .join(', ')
  const tail = defaulted.length ? ` [defaults: ${defaulted.sort().join(', ')}]` : ''
  record(`${component}.config {${body}}${tail}`)
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
      const capacity = Number(size)
      if (!Number.isInteger(capacity) || capacity < 1) {
        throw new Error(`pool size must be an integer >= 1 (got ${size})`)
      }
      return new PoolHandle(String(url), capacity)
    },
  },
  Map: {
    new(): MapHandle {
      return new MapHandle()
    },
  },
  Job: {
    // async host builtin (IR v1/A1): a cancellable handle that resolves on
    // later ticks, so `await` steps have a real in-flight window — and real
    // async state — for the divert tests
    run(name: any): JobHandle {
      return new JobHandle(String(name))
    },
    pending: pendingJobs,
    TICKS: JOB_TICKS,
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
