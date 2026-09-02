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

// ---------------------------------------------------------------------------
// confidentiality: what a declared `Secret[T]` may look like in the host trace
// (roadmap item 421 F6, the typescript peer of backends/python/confidential.py)
//
// A `Secret[T]` declaration authorises disclosure TO THE DECLARED RECEIVER. It
// does not authorise the host trace to keep a copy: `record` interpolates a Map
// key, a `pool.query` sql, a stream item and a component's resolved config
// straight into `hostLog`, which is this tier's shared observability channel —
// exported, and forwarded to any `onHostEvent` subscriber a host installs.
//
// The scrub is placed at `record`, the ONE choke point every trace event passes
// through, not at each call site: an event is a string this file has already
// interpolated into, so a VALUE funnel cannot reach it, and per-printer
// redaction is the discipline this funnel exists to replace. The match is EXACT
// against the values a declared marking registered, never a pattern, so ordinary
// trace is byte-identical and a composition that declares no secret pays one
// empty-set test.

/** Must equal `confidential.REDACTED` / `taint.REDACTED_SECRET` on the py tier:
 *  a polyglot composition should redact to the SAME marker whichever tier
 *  produced the line. */
export const REDACTED_SECRET = '<redacted:secret>'

// A remembered confidential value has to be long enough that an exact match
// means something. Below this a value is a coin flip against ordinary trace
// data ('', '1', 'ok'), and blanket-erasing those would gut the trace for no
// confidentiality gain. Same bound as the py tier's `_MIN_MARKABLE`.
const MIN_MARKABLE = 4

const secretValues = new Set<string>()

/** Remember one already-rendered value as confidential. Walks a container so a
 *  `Secret[List[Str]]` receiver marks its elements, mirroring py's
 *  `register_secret_tree`. */
function rememberSecret(value: unknown): void {
  if (value === null || value === undefined || typeof value === 'boolean') return
  if (typeof value === 'string') {
    if (value.length >= MIN_MARKABLE) secretValues.add(value)
    return
  }
  if (typeof value === 'number' || typeof value === 'bigint') {
    const form = String(value)
    if (form.length >= MIN_MARKABLE) secretValues.add(form)
    return
  }
  if (Array.isArray(value)) {
    for (const item of value) rememberSecret(item)
    return
  }
  if (typeof value === 'object') {
    // Values only: a record's KEYS are field names the author wrote, not the
    // declared secret, and erasing them would destroy the trace's shape.
    for (const item of Object.values(value as Record<string, unknown>)) {
      rememberSecret(item)
    }
  }
}

/** The RECEIVER end of a declared crossing: called at the head of a provide
 *  method whose service declares that parameter `Secret[T]`. The emitter reads
 *  the same `params[i].secret` stamp the py tier reads. */
export function markSecret(...values: unknown[]): void {
  for (const value of values) rememberSecret(value)
}

/** The ORIGIN end: the return of an extern whose declared return type was
 *  `Secret[T]`, where the value first enters the value world. Returns its
 *  argument, so a call site wraps with no change in meaning. */
export function secretResult<T>(value: T): T {
  rememberSecret(value)
  return value
}

/** Drop every remembered value (test isolation; also called by `resetHost`). */
export function forgetSecrets(): void {
  secretValues.clear()
}

/** Replace every registered secret in free-form host text by the placeholder.
 *  Longest needle first, so one that contains another leaves no tail behind. */
export function redactText(text: string): string {
  if (secretValues.size === 0 || !text) return text
  let out = text
  for (const needle of [...secretValues].sort((a, b) => b.length - a.length)) {
    if (needle && out.includes(needle)) out = out.split(needle).join(REDACTED_SECRET)
  }
  return out
}

/** Subscribe to host events (returns an unsubscribe function). */
export function onHostEvent(listener: (entry: string) => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Reset host state between tests. */
export function resetHost(): void {
  hostLog.length = 0
  forgetSecrets()
  liveResources.clear()
  poolCounter = 0
  mapCounter = 0
  jobCounter = 0
  jobHandles.length = 0
  Clock.reset()
}

/** Append one entry to the shared observability trace (`hostLog`) and notify
 * subscribers. Exported so an extern's own `@ts` host body can participate in
 * the same trace every other host builtin uses — useful for a witnessed or
 * compensating extern that wants its crossing to show up in ordering
 * assertions alongside `Pool`/`Map`/`Job` events, without inventing a second
 * ad hoc channel. */
export function record(entry: string): void {
  // The confidentiality funnel (item 421 F6). Every event passes here before it
  // can reach `hostLog` or any subscriber, so a sink added tomorrow reads an
  // already-scrubbed line and cannot leak.
  const scrubbed = redactText(entry)
  hostLog.push(scrubbed)
  for (const listener of listeners) listener(scrubbed)
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

// ---------------------------------------------------------------------------
// Time as a coeffect (roadmap item 57, docs/time-coeffect.md)
//
// A timer (`every 30s { … }` / `after 5m { … }`) is a *revertible schedule*:
// arming it registers a firing with the clock, and its inverse is cancellation,
// so the emitted body yields `() => handle.cancel()` and the component's own
// teardown reverts it like any other effect — no orphaned interval outlives the
// activation (a leak would surface through `liveResources`, the same R4 residue
// set Pool/Map use).  The clock is a *coeffect the harness provides*, not
// wall-clock: `Clock.now()` moves only when something calls `Clock.advance(ms)`
// (`revl test`/replay drives it), so a firing is a deterministic timeline step
// (`fires on the 3rd tick`), never a race.  The reference tier mirrors
// backends/python/runtime.py tick-for-tick.
// ---------------------------------------------------------------------------

export type TimerMode = 'every' | 'after'
export type TimerState = 'live' | 'cancelled' | 'done'

let timerCounter = 0
const timers: TimerHandle[] = []

export class TimerHandle {
  readonly serial: number
  readonly mode: TimerMode
  readonly intervalMs: number
  private readonly body: () => void
  private status: TimerState = 'live'
  nextAt: number
  fired = 0

  constructor(mode: TimerMode, intervalMs: number, body: () => void) {
    if (!Number.isInteger(intervalMs) || intervalMs <= 0) {
      throw new Error(`timer interval must be a positive integer (ms), got ${intervalMs}`)
    }
    this.serial = ++timerCounter
    this.mode = mode
    this.intervalMs = intervalMs
    this.body = body
    this.nextAt = Clock.now() + intervalMs
    timers.push(this)
    liveResources.add(this.label)
    record(`${this.label}.schedule ${mode} ${intervalMs}ms`)
  }

  get label(): string {
    return `timer#${this.serial}`
  }

  state(): TimerState {
    return this.status
  }

  /** live -> cancelled (true); a no-op returning false once spent. The derived
   *  inverse the emitted body yields — running it on teardown proves the
   *  schedule leaves no residue. */
  cancel(): boolean {
    if (this.status !== 'live') return false
    this.status = 'cancelled'
    liveResources.delete(this.label)
    record(`${this.label}.cancel`)
    return true
  }

  /** @internal — driven only by Clock.advance. */
  fire(now: number): void {
    this.fired += 1
    clockFirings.push([this.serial, now])
    record(`${this.label}.fire #${this.fired} at ${now}ms`)
    this.body()
    if (this.mode === 'after') {
      // a one-shot's schedule is spent once it fires; release it through the
      // same path as cancel so `liveResources` clears and the teardown's own
      // `handle.cancel()` is a clean no-op.
      this.status = 'done'
      liveResources.delete(this.label)
      record(`${this.label}.cancel`)
    }
  }
}

const clockFirings: Array<[number, number]> = []
let clockNow = 0

/** The clock coeffect (item 57): time advances only when the harness calls
 *  `advance`, so timer firings are deterministic timeline steps. */
export const Clock = {
  now(): number {
    return clockNow
  },
  /** Advance logical time by `ms`, firing every timer that comes due —
   *  earliest first, ties broken by arm order — re-arming `every` timers across
   *  the whole span. Returns the firing count so a test can assert exactly how
   *  many steps an advance produced. */
  advance(ms: number): number {
    if (!Number.isInteger(ms) || ms < 0) {
      throw new Error(`clock advance must be a non-negative integer (ms), got ${ms}`)
    }
    const target = clockNow + ms
    let count = 0
    // An event loop: an `every` re-arms and may fire again within one advance,
    // and firings interleave across timers in true time order. Bounded because
    // each pass consumes one firing and every nextAt strictly increases.
    for (;;) {
      let next: TimerHandle | undefined
      for (const t of timers) {
        if (t.state() !== 'live' || t.nextAt > target) continue
        if (
          next === undefined ||
          t.nextAt < next.nextAt ||
          (t.nextAt === next.nextAt && t.serial < next.serial)
        ) {
          next = t
        }
      }
      if (next === undefined) break
      clockNow = next.nextAt
      if (next.mode === 'every') next.nextAt += next.intervalMs
      next.fire(clockNow)
      count += 1
    }
    clockNow = target
    return count
  },
  /** Live timers — a teardown that abandons one leaves this > 0. */
  pending(): number {
    return timers.filter((t) => t.state() === 'live').length
  },
  /** The recorded firing log: `[timerSerial, firedAtMs]` in order. */
  firings(): Array<[number, number]> {
    return clockFirings.map((f) => [f[0], f[1]])
  },
  reset(): void {
    clockNow = 0
    timerCounter = 0
    timers.length = 0
    clockFirings.length = 0
  },
}

export function scheduleEvery(intervalMs: number, body: () => void): TimerHandle {
  return new TimerHandle('every', intervalMs, body)
}

export function scheduleAfter(intervalMs: number, body: () => void): TimerHandle {
  return new TimerHandle('after', intervalMs, body)
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
    // Opt is bare `value | undefined` on this tier (emit.py: the stdlib Map
    // `lookup` answers undefined when absent — "exactly the Opt None case"),
    // so a missing key must read as `undefined`, never `null`: `None == None`
    // is false for `null == undefined` through revlEq. The interop bridge
    // canonicalizes both to JSON null on the wire.
    return this.data.get(key)
  }

  insert(key: any, value: any): void {
    this.assertLive('insert')
    record(`${this.label}.insert(${key}, ${value})`)
    this.data.set(key, value)
  }

  insert_if_absent(key: any, value: any): boolean {
    // item 397: the atomic compare-and-set. Node runs one event loop, so this
    // synchronous method (no await) is atomic by run-to-completion: no task can
    // interleave between the membership test and the insert. Returns whether it
    // inserted; a `false` (key already present) leaves the existing value
    // untouched.
    this.assertLive('insert_if_absent')
    if (this.data.has(key)) {
      record(`${this.label}.insert_if_absent(${key}) -> false`)
      return false
    }
    this.data.set(key, value)
    record(`${this.label}.insert_if_absent(${key}) -> true`)
    return true
  }

  remove(key: any): void {
    this.assertLive('remove')
    record(`${this.label}.remove(${key})`)
    this.data.delete(key)
  }

  // Iteration surface (docs/stdlib-2.0.md §Map). The checker promises
  // `size()`/`keys()` on a host `Map.new()` receiver too, and emit.py lowers
  // both as plain method calls on this object — so they must exist as methods,
  // not the bare `size` getter this class used to carry (which `store.size()`
  // would have tried to *call*). They mirror the value-Map builtins exactly:
  // `size()` is the entry count as a bigint (revl Int is a bigint on this
  // tier, as Pool.execute already returns), `keys()` yields the keys in
  // ascending canonical Str (code-point) order. Read-only — no host trace,
  // like the value-Map queries.
  size(): bigint {
    this.assertLive('size')
    return BigInt(this.data.size)
  }

  keys(): string[] {
    this.assertLive('keys')
    // Canonical order is code-point order; JS's default sort is UTF-16
    // code-unit order, which diverges past U+FFFF, so compare via Array.from
    // (the same comparator emit.py inlines for the value-Map `keys`).
    return ([...this.data.keys()] as string[]).sort((a, b) => {
      const A = Array.from(a), B = Array.from(b)
      for (let i = 0; i < Math.min(A.length, B.length); i++) {
        if (A[i] !== B[i]) return A[i] < B[i] ? -1 : 1
      }
      return A.length - B.length
    })
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
// Witnessed effects + the two-phase teardown loop
// (items 243/247, docs/design/teardown-contract.md; item 243 Slice 2b)
//
// One LIFO disposer stack per activation, three entry kinds sharing it:
//
//   bracket        acquire's inverse. Replays on EVERY teardown, clean or
//                  aborted (unchanged from before this slice).
//   transactional  a `witnessed` extern's declared inverse (item 243).
//                  Replays ONLY on abort; a clean commit DISCHARGES it and
//                  drops the witness (GC) — the mutation is the deliverable.
//   compensation   an `emit ... compensate ...` best-effort offset (item
//                  247). Replays ONLY on abort, and only in PHASE 2, after
//                  every bracket/transactional inverse has finished — never
//                  on a clean commit (discharged, never run).
//
// `_component_body`/`emit.py` lowers one activation into a SINGLE
// `ctx.effect(function* () {...})`, exactly as before this slice (REPORT.md
// §1.1: only within one effect's own yields does cordis guarantee strict
// LIFO disposal). Every step's disposer is now yielded through one of this
// `Frame`'s registration methods instead of a raw arrow, so all three kinds
// share that one stack in registration order — no second list.
//
// Commit-vs-abort discriminator: "did the body reach its final yield"
// (mirrors backends/python/runtime.py `Frame.drain`, item 243 Slice 2a
// decision 1 — no new cordis signal needed). The emitted body yields two
// sentinels around the ordinary steps:
//
//   yield frame.begin   // FIRST yielded -> disposed LAST (cordis LIFO)
//   ...ordinary steps...
//   yield frame.drain   // LAST yielded  -> disposed FIRST, ONLY reached if
//                       // the body ran to completion (a clean unload)
//
// `drain`, disposed first on a clean unload, flips `committed` before any
// earlier-registered entry's own disposer runs — every bracket/transactional/
// compensation entry then discharges (or, for bracket, still runs) in
// whatever order cordis visits them; the commit path has no phase split
// (docs/design/teardown-contract.md, "The teardown algorithm").
//
// On ABORT, `drain` is never yielded (the throw happens before the body's
// final statement), so `committed` stays false. cordis' OWN disposal chain
// runs each already-yielded disposer strictly sequentially (each `.then()`-
// chained, REPORT.md §1.1) — this is the mechanism that gives Phase 1 (every
// bracket + transactional inverse) automatic LIFO-to-completion, PROVIDED
// each disposer catches its own failure so one throw cannot break the
// `.then()` chain and starve every earlier (later-disposed) entry — the
// contract's "continue-and-record" (both `bracket-fault` and
// `restore-residue`). A compensation entry, invoked at ITS stack position
// during this same chain, does NOT fire there — that would be the OLD
// single-phase a5 behavior the contract retires. It ENQUEUES itself instead
// and returns immediately; `begin`, yielded first and so disposed dead LAST,
// is the "post-unwind hook" the contract's mechanism note describes — by the
// time cordis reaches it every earlier disposer has already run, so Phase 1
// is complete and the compensation queue is fully populated, in exactly the
// LIFO order cordis visited them. `begin` then drains it as Phase 2,
// best-effort and budget-bounded.
//
// No in-call preemption on this tier (teardown-contract.md's per-tier table:
// "typescript: none — single-threaded event loop; nothing runs to observe a
// deadline during a synchronous host call"). The only bound this tier can
// honestly deliver is the NORMATIVE between-compensation deadline check.

/** One boundary crossing's identity, captured at registration — never
 * re-read at teardown (docs/design/teardown-contract.md, "No data hazard").
 * `key` is the capability/service key, `method` the crossing's own method,
 * `args` its captured (serializable) arguments, `site` a best-effort source
 * label. */
export interface Crossing {
  key: string
  method: string
  args: unknown[]
  site: string
}

/** The merged residue schema's `kind` discriminator
 * (docs/design/teardown-contract.md, "The merged residue schema").
 * `unreconstructible` is recovery-only (no durable WAL sink exists on this
 * tier yet — see `DischargeDescriptor` below) and so never appears here. */
export type ResidueKind = 'restore-residue' | 'bracket-fault' | 'compensation-residue'

export interface AttemptedMeta {
  /** The named inverse/compensation call, or `null` iff never attempted
   * (a Phase-2 entry skipped under the budget). */
  call: string | null
  args: unknown[]
  phase: 1 | 2
}

export interface ResidueRecord {
  kind: ResidueKind
  crossing: Crossing
  attempted: AttemptedMeta
  error: { type: string; message: string } | null
  attemptedFlag: boolean
  outcome: 'failed' | 'unknown' | 'not-attempted'
  referent: string
  hint: string
}

/** The envelope both the abort path (here) and `revl recover` (py-only,
 * src/revl/recovery.py) return (docs/design/teardown-contract.md, "The
 * merged residue schema"). */
export interface AbortReport {
  clean: boolean
  outstanding: ResidueRecord[]
  /** Referents still out in the world, derived from `outstanding` — this
   * tier has no `World` adapter of its own (that is py's offline-recovery
   * concern, src/revl/recovery.py), so this is the honest generic view: what
   * the residue records themselves say is still out. */
  worldRemaining: string[]
  proof: string
}

/** The WAL discharge-descriptor shape (docs/design/teardown-contract.md,
 * "WAL descriptor") — every `transactional` inverse and every `compensation`
 * builds one of these AT REGISTRATION, in memory. There is no durable WAL
 * sink wired to this tier yet (that plumbing lives on the py tier only,
 * `backends/python/replay.py` / `src/revl/recovery.py`, out of this slice's
 * scope — see the emitter's module docstring); `Frame.descriptors()` exposes
 * the in-memory shape so a future WAL sink (or a test) can consume it
 * without this Frame needing to know anything about persistence. */
export interface DischargeDescriptor {
  record: 'discharge-descriptor'
  seq: number
  entry: 'transactional' | 'compensation'
  call: { receiver: string; method: string; args: unknown[] }
  origin: Crossing
  witness: unknown | null
  idempotency: string | null
}

export interface DischargeRecord {
  record: 'discharge'
  discharged: number[]
}

const RESIDUE_HINTS: Record<ResidueKind, string> = {
  'restore-residue':
    'the witnessed inverse failed to restore on abort (anticipated — TOCTOU / ' +
    'disk-full are expected failure modes, 243 rule 6); verify the world state ' +
    'by hand and re-run the inverse if it is still safe to.',
  'bracket-fault':
    'a bracket inverse that claimed G5 infallibility raised on abort — this is ' +
    'a CONTRACT-GRADE fault; do not keep trusting this activation\'s ' +
    "'revertible' label without inspecting it by hand.",
  'compensation-residue':
    'the best-effort compensation was attempted (or owed) and did not land; ' +
    'check whether the offsetting action still needs to run by hand.',
}

/** Read a `REVL_*` budget env var (milliseconds). Unset/blank -> `fallback`;
 * `0` -> no bound (the between-compensation check still runs and records
 * nothing expired); a non-finite or negative value falls back too, rather
 * than silently producing a nonsensical deadline. Read once, at Frame
 * construction (activation time) — docs/design/teardown-contract.md, "read
 * once at activation". */
function _revlBudgetMs(name: string, fallback: number): number {
  const env = typeof process !== 'undefined' ? (process as any).env : undefined
  const raw = env ? env[name] : undefined
  if (raw === undefined || raw === '') return fallback
  const parsed = Number(raw)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback
}

interface _PendingCompensation {
  seq: number
  crossing: Crossing
  methodName: string
  args: unknown[]
  run: () => unknown
}

/** A PROVIDE-METHOD-registered witnessed (transactional) entry (item 318) —
 * the per-tool-call H1 seam. Unlike an activation-body transactional entry
 * (whose disposer the body generator yields into cordis' LIFO stack), a method
 * body has no generator to yield into, and adopting it as a sibling
 * `ctx.effect` is UNSOUND on this cordis-style tier: cordis disposes an adopted
 * effect BEFORE the body effect's final `yield frame.drain`, so on a CLEAN
 * unload the disposer would observe `committed` still false and wrongly replay
 * (revert) the deliverable. So the entry is PARKED here and disposed by `drain`
 * itself, once `committed`/`aborting` are settled — see `transactionalMethod`
 * and `drain`. The observable flags mirror
 * backends/python/runtime.py's `_Transactional.discharged`/`replayed`, so a
 * test can assert the commit-vs-abort fate per entry. */
interface _DeferredTransactional {
  crossing: Crossing
  undoMethod: string
  undo: ((witness: unknown) => unknown) | null
  witness: unknown
  /** committed: inverse skipped, mutation persists, refs GC'd. */
  discharged: boolean
  /** aborted: inverse ran, mutation reverted, refs GC'd. */
  replayed: boolean
}

/** ctx -> the activation `Frame` on that context (item 318). Weak-keyed so a
 * torn-down instance's frame is collected with its context. Mirrors
 * backends/python/runtime.py's `_FRAME_BY_CTX`: the session-level abort seam
 * (`Frame.abort`) reaches a live activation's frame through the fiber ctx it
 * already holds. Populated in the `Frame` constructor; a component that never
 * builds a `Frame` (no witnessed/compensation entry) registers nothing. */
const _frameByCtx = new WeakMap<Context, Frame>()

/** The activation `Frame` on `ctx`, or `undefined`. The handle the item-245
 * commit/abort UX (and its tests) use to reach a live activation's frame and
 * call `abort()` before the clean unload that would otherwise implicitly
 * commit every per-tool-call mutation (item 318). */
export function frameForCtx(ctx: Context): Frame | undefined {
  return _frameByCtx.get(ctx)
}

/** One activation's teardown accumulator (item 243 Slice 2b) — the TS analog
 * of `backends/python/runtime.py`'s `Frame`, extended with the `compensation`
 * entry kind and the two-phase abort this tier had no precedent for (py's
 * own `Frame` implements bracket + transactional only; item 247's runtime
 * seam is new work here, built directly from
 * docs/design/teardown-contract.md's algorithm). */
export class Frame {
  readonly ctx: Context
  readonly name: string
  /** Flips true the instant `drain` is disposed — the commit-vs-abort
   * discriminator every registered entry reads at ITS OWN disposal time. */
  committed = false

  private seqCounter = 0
  private descriptorList: DischargeDescriptor[] = []
  private pending: _PendingCompensation[] = []
  private residue: ResidueRecord[] = []
  private readonly budgetMs: number
  private readonly perCallMs: number
  private dischargeRec: DischargeRecord | null = null
  /** item 318: PROVIDE-METHOD-registered witnessed entries (per-tool-call H1),
   * parked here rather than yielded into cordis' LIFO stack, and disposed by
   * `drain` once the commit-vs-abort bit is settled. */
  private deferredList: _DeferredTransactional[] = []
  /** item 247 (method-body compensate remainder): PROVIDE-METHOD-registered COMPENSATION entries (`emit ...
   * compensate ...` in a method body), the compensation analog of
   * `deferredList`. A method body has no generator to yield the compensation
   * disposer into, and adopting it as a sibling `ctx.effect` is unsound (cordis
   * disposes it BEFORE the body `drain`, so it fires the offset on a CLEAN
   * unload — destroying the deliverable, the item-247 bug left on the method-
   * body site). So it is parked here and disposed by `drain`: DISCHARGED on a
   * commit; ENQUEUED onto `pending` on an abort, so `begin`'s post-unwind
   * `runPhase2` fires it after every Phase-1 inverse. */
  private deferredCompensations: _PendingCompensation[] = []
  /** item 318: the reject signal for a component that already activated
   * cleanly. `committed` (flipped by `drain`) answers "did the ACTIVATION body
   * complete"; but a per-tool-call mutation runs AFTER activation, so on any
   * later clean unload `drain` runs and would always commit it. `abort()` sets
   * this BEFORE that unload; `drain` then leaves `committed` false, so every
   * transactional entry — activation-body and method-deferred alike — replays
   * and the mutations revert. */
  private aborting = false

  constructor(ctx: Context, name: string) {
    this.ctx = ctx
    this.name = name
    this.budgetMs = _revlBudgetMs('REVL_COMPENSATION_BUDGET_MS', 5000)
    this.perCallMs = _revlBudgetMs('REVL_COMPENSATION_PER_CALL_MS', 1000)
    // item 318: so the session-level abort seam can reach this live activation's
    // frame through the fiber ctx it holds. A no-op for every prior program
    // (nothing looks the frame up unless `abort()` is called).
    _frameByCtx.set(ctx, this)
  }

  private nextSeq(): number {
    this.seqCounter += 1
    return this.seqCounter
  }

  private static errorOf(err: unknown): { type: string; message: string } {
    if (err instanceof Error) return { type: err.constructor.name, message: err.message }
    return { type: 'Error', message: String(err) }
  }

  private static referentOf(crossing: Crossing): string {
    const args = crossing.args.map((a) => JSON.stringify(a)).join(', ')
    return `${crossing.key}.${crossing.method}(${args})`
  }

  private pushResidue(
    kind: ResidueKind,
    crossing: Crossing,
    attempted: AttemptedMeta,
    err: unknown,
    outcome: ResidueRecord['outcome'] = 'failed',
  ): void {
    this.residue.push({
      kind,
      crossing,
      attempted,
      error: err === undefined ? null : Frame.errorOf(err),
      attemptedFlag: attempted.call !== null,
      outcome,
      referent: Frame.referentOf(crossing),
      hint: RESIDUE_HINTS[kind],
    })
  }

  // -- entry registration ---------------------------------------------------

  /** Register a bracket (acquire) inverse. Replays on every teardown —
   * unchanged in outward behavior — but now routed through the Frame so a
   * Phase-1 failure is caught and recorded (`bracket-fault`) instead of
   * throwing straight into cordis' disposer chain, where an uncaught raise
   * would break the `.then()` chain and skip every earlier (later-disposed)
   * entry — exactly the residue "continue-and-record" forbids. The
   * contract's commit-path pseudocode has no catch on this arm ("still
   * runs"): a failure there is not this loop's to swallow. */
  bracket(crossing: Crossing, undoMethod: string, inverse: () => unknown): () => unknown {
    return () => {
      if (this.committed) return inverse()
      try {
        return inverse()
      } catch (err) {
        this.pushResidue('bracket-fault', crossing, { call: undoMethod, args: crossing.args, phase: 1 }, err)
      }
    }
  }

  /** Register a witnessed (transactional) inverse (item 243): abort-only
   * replay, commit-time discharge + witness GC. `witness` is the `Ok`
   * payload, captured once here at registration — never re-read at
   * teardown. Builds the WAL discharge-descriptor eagerly (in memory; see
   * `DischargeDescriptor`'s docstring for why there is no durable sink yet). */
  transactional(
    crossing: Crossing,
    undoMethod: string,
    undo: (witness: unknown) => unknown,
    witness: unknown,
  ): () => unknown {
    const seq = this.nextSeq()
    this.descriptorList.push({
      record: 'discharge-descriptor',
      seq,
      entry: 'transactional',
      call: { receiver: this.name, method: undoMethod, args: [witness] },
      origin: crossing,
      witness,
      idempotency: null,
    })
    return () => {
      // discharge (below, in `drain`) already accounted for this seq the
      // instant `committed` flipped true — nothing left to do here.
      if (this.committed) return
      try {
        return undo(witness)
      } catch (err) {
        this.pushResidue('restore-residue', crossing, { call: undoMethod, args: [witness], phase: 1 }, err)
      }
    }
  }

  /** Register a PROVIDE-METHOD witnessed (transactional) inverse (item 318) —
   * the per-tool-call H1 seam. An agent's fs mutation fires from a
   * provide-method (per request), and its inverse must OUTLIVE the method call:
   * the method returns, but the rollback must survive until the
   * component/session commits or aborts. This component's activation frame is
   * that accumulator — component-long, and its `committed`/`aborting` bit
   * already drives every transactional entry's discharge-vs-replay.
   *
   * Unlike `transactional` (whose disposer the activation body yields into
   * cordis' LIFO stack), this does NOT return a disposer: a method body has no
   * generator to yield into, and adopting a sibling `ctx.effect` is unsound on
   * this cordis-style tier (disposed BEFORE the body's `drain`, so a clean
   * unload would see `committed` false and wrongly revert the deliverable — the
   * hazard item 318 found on py). So the entry is PARKED in `deferredList` and
   * disposed by `drain`, where `committed`/`aborting` is already settled. The
   * WAL discharge-descriptor is still built at registration, durably ahead of
   * the commit-vs-abort decision, so `descriptors()` enumerates every crossing
   * and `drain`'s discharge record names every committed one. Registration is
   * unconditional here; the emitted call site invokes it only on the `Ok`
   * branch, so a failed mutation that touched nothing schedules no rollback. */
  transactionalMethod(
    crossing: Crossing,
    undoMethod: string,
    undo: (witness: unknown) => unknown,
    witness: unknown,
  ): _DeferredTransactional {
    const seq = this.nextSeq()
    this.descriptorList.push({
      record: 'discharge-descriptor',
      seq,
      entry: 'transactional',
      call: { receiver: this.name, method: undoMethod, args: [witness] },
      origin: crossing,
      witness,
      idempotency: null,
    })
    const entry: _DeferredTransactional = {
      crossing,
      undoMethod,
      undo,
      witness,
      discharged: false,
      replayed: false,
    }
    this.deferredList.push(entry)
    return entry
  }

  /** Mark this activation ABORTING (item 318). A component that activated
   * cleanly reaches its final `yield frame.drain`, so any later clean unload
   * runs `drain` and would implicitly COMMIT every accumulated transactional
   * entry. A session-level reject calls this first: `drain` then leaves
   * `committed` false, so the activation-body transactional entries AND the
   * per-tool-call deferred entries all replay their inverses and the mutations
   * revert, residue-free. Idempotent; a no-op on the commit path. */
  abort(): void {
    this.aborting = true
  }

  /** The parked per-tool-call transactional entries not yet disposed
   * (introspection for the H1 proof, mirroring
   * backends/python/runtime.py's `_deferred_transactional`). */
  deferredEntries(): _DeferredTransactional[] {
    return [...this.deferredList]
  }

  /** Register a compensation (item 247): audit-facing, best-effort,
   * ABORT-ONLY, Phase 2. `args` are captured here, at registration — never
   * re-read at teardown (the "no data hazard" reason for the phase split).
   * Never runs on a clean unload (discharged: the forward emission was
   * already the deliverable). On abort it does not fire when cordis visits
   * its stack position — see this section's module doc for the enqueue/
   * post-unwind-hook mechanism. */
  compensation(
    crossing: Crossing,
    method: string,
    args: unknown[],
    run: () => unknown,
  ): () => unknown {
    const seq = this.nextSeq()
    this.descriptorList.push({
      record: 'discharge-descriptor',
      seq,
      entry: 'compensation',
      call: { receiver: this.name, method, args },
      origin: crossing,
      witness: null,
      idempotency: null,
    })
    const entry: _PendingCompensation = { seq, crossing, methodName: method, args, run }
    return () => {
      // discharge (below, in `drain`) already accounted for this seq the
      // instant `committed` flipped true — nothing left to do here.
      if (this.committed) return
      this.pending.push(entry)
    }
  }

  /** Register a PROVIDE-METHOD `emit ... compensate ...` step's offset as a
   * COMPENSATION on THIS component's activation frame (item 247 (method-body compensate remainder)) — the
   * compensation analog of `transactionalMethod` (item 318), and the method-body
   * analog of `compensation` (item 247). A per-tool-call emission fires from a
   * provide-method; its offset must outlive the method call and is owed ONLY on
   * an abort, never on a clean commit (the emission was the deliverable).
   *
   * Unlike `compensation` (whose disposer the activation body's generator yields
   * into cordis' LIFO stack), a method body has no generator to yield into.
   * Adopting the disposer as a sibling `ctx.effect` is UNSOUND — cordis disposes
   * it BEFORE the body's `drain`, firing the offset on a clean unload (the
   * placeholder-lowering bug this closes). So the entry is PARKED in
   * `deferredCompensations` and disposed by `drain`: DISCHARGED on a commit
   * (never runs — its seq still joins the discharge record); ENQUEUED onto
   * `pending` on an abort, where `begin`'s post-unwind `runPhase2` fires it in
   * Phase 2 after every proof inverse. `args` are captured at registration (the
   * "no data hazard" reason for the phase split), exactly as `compensation`. */
  compensationMethod(
    crossing: Crossing,
    method: string,
    args: unknown[],
    run: () => unknown,
  ): void {
    const seq = this.nextSeq()
    this.descriptorList.push({
      record: 'discharge-descriptor',
      seq,
      entry: 'compensation',
      call: { receiver: this.name, method, args },
      origin: crossing,
      witness: null,
      idempotency: null,
    })
    this.deferredCompensations.push({ seq, crossing, methodName: method, args, run })
  }

  // -- prologue / epilogue sentinels ----------------------------------------

  /** Yielded FIRST -> disposed LAST. No-op on commit (nothing left to do —
   * the commit path has no phase split). On abort this is the "post-unwind
   * hook": every earlier-registered entry has already been disposed by the
   * time cordis reaches this one, so Phase 1 is complete and the
   * compensation queue is fully populated — drain it as Phase 2. */
  begin = (): void => {
    if (this.committed) return
    this.runPhase2()
  }

  /** Yielded LAST -> disposed FIRST. Reaching disposal here IS the proof the
   * body ran to completion: a clean, committing unload (mirrors
   * backends/python/runtime.py `Frame.drain`'s "did drain run"
   * discriminator). Flips `committed` before any earlier-registered entry's
   * own disposer runs, later in this same `.then()` chain. */
  drain = (): void => {
    // item 318: `aborting` is the reject signal for an already-activated
    // component (a session-level abort of per-tool-call work). When set,
    // `drain` runs but does NOT commit: `committed` stays false, so every
    // transactional entry — the activation body's cordis-yielded ones AND the
    // method-registered deferred ones below — replays its inverse and the
    // mutations revert. A plain unload never sets it, so this stays
    // byte-identical to the previous unconditional commit for every existing
    // activation-body-only program.
    if (!this.aborting) this.committed = true
    // Every registered transactional/compensation entry WILL discharge — the
    // flag just flipped true, and every entry's own disposer reads it — so
    // the discharge record can be built from the full registry right now,
    // rather than waiting for each one to actually be disposed (which, for
    // `begin`, yielded first and so disposed LAST, would still be pending at
    // this point). Mirrors backends/python/runtime.py `Frame.drain`, which
    // reads `self._transactional` the same way, eagerly, at this same point.
    // The discharge record is the COMMIT proof; it must NOT be written for an
    // aborting teardown, where the inverses are being replayed, not committed.
    if (this.committed && this.descriptorList.length) {
      this.dischargeRec = {
        record: 'discharge',
        discharged: this.descriptorList.map((d) => d.seq),
      }
    }
    // item 318: dispose the method-registered (deferred) transactional entries
    // HERE, now that the commit-vs-abort bit is settled. They are not cordis
    // disposers, so this is their SOLE disposal — no double-free with the
    // fiber's own unwind. On a commit each discharges (mutation persists,
    // witness GC'd); on an abort each replays (reverts, residue-free). A
    // Phase-1 restore failure is caught and recorded (`restore-residue`),
    // never thrown into the disposer chain — same continue-and-record rule as
    // the activation-body transactional inverse.
    //
    // item 369: replay in reverse INVOCATION order (LIFO), NOT registration
    // order. `deferredList` is pushed newest-last as each provide-method fires
    // (`transactionalMethod`), so it must be drained newest-FIRST — exactly
    // like the activation-body path, where cordis unwinds its disposer stack
    // LIFO. On a COMMIT order is immaterial (every entry no-op discharges);
    // on an ABORT two inverses whose
    // paths OVERLAP must undo newest-first or a FIFO replay leaves residue or
    // DESTROYS pre-session data — every stdlib/fs.rvl inverse is idempotent-
    // and-total, so the oldest inverse runs first, no-ops, and the newer one
    // undoes into the hole (G7, 243 §2). Mirrors backends/python/runtime.py.
    const deferred = [...this.deferredList].reverse()
    this.deferredList = []
    for (const entry of deferred) {
      if (this.committed) {
        entry.discharged = true
        entry.undo = null
        entry.witness = null
        continue
      }
      entry.replayed = true
      const undo = entry.undo
      const witness = entry.witness
      entry.undo = null
      entry.witness = null
      if (undo === null) continue
      try {
        undo(witness)
      } catch (err) {
        this.pushResidue(
          'restore-residue',
          entry.crossing,
          { call: entry.undoMethod, args: [witness], phase: 1 },
          err,
        )
      }
    }
    // item 247 (method-body compensate remainder): dispose the method-registered COMPENSATION entries now that the
    // commit-vs-abort bit is settled — the compensation analog of the deferred
    // transactional loop above, and the method-body analog of the activation-
    // body `compensation` (item 247). On a COMMIT each DISCHARGES (never runs —
    // the emission was the deliverable; its seq already joined the discharge
    // record above). On an ABORT each is ENQUEUED onto `pending`, never fired
    // inline here, so `begin`'s post-unwind `runPhase2` fires it in Phase 2
    // strictly AFTER every Phase-1 inverse (the deferred transactional above AND
    // the activation-body disposers cordis unwinds between `drain` and `begin`).
    // Enqueued newest-first so Phase 2 runs the newest compensation first (LIFO),
    // matching the activation-body path.
    if (!this.committed) {
      const deferredComp = [...this.deferredCompensations].reverse()
      for (const entry of deferredComp) this.pending.push(entry)
    }
    this.deferredCompensations = []
  }

  private runPhase2(): void {
    const total = this.budgetMs
    const deadline = total === 0 ? Number.POSITIVE_INFINITY : Date.now() + total
    // LIFO within Phase 2: `pending` is already in exactly that order — each
    // compensation pushed itself in the order cordis called its disposer,
    // which is cordis' own LIFO position order (its disposal chain visits
    // yields newest-first) — no extra reverse needed.
    for (const entry of this.pending) {
      if (Date.now() >= deadline) {
        this.pushResidue(
          'compensation-residue', entry.crossing,
          { call: null, args: entry.args, phase: 2 }, 'deadline-expired', 'not-attempted',
        )
        continue
      }
      // `perCallMs` is read (env parity across tiers) but cannot cut off an
      // in-flight synchronous call on this tier — see the module doc above.
      void this.perCallMs
      try {
        entry.run()
      } catch (err) {
        this.pushResidue(
          'compensation-residue', entry.crossing,
          { call: entry.methodName, args: entry.args, phase: 2 }, err,
        )
      }
    }
    this.pending = []
  }

  // -- introspection ---------------------------------------------------------

  /** The WAL discharge-descriptors built at registration, in registration
   * (`seq`) order — see `DischargeDescriptor`'s docstring. */
  descriptors(): DischargeDescriptor[] {
    return [...this.descriptorList]
  }

  /** The discharge record written when `drain` ran (a clean commit), or
   * `null` before that or when nothing needed discharging. */
  dischargeRecord(): DischargeRecord | null {
    return this.dischargeRec
  }

  /** The merged residue envelope (docs/design/teardown-contract.md, "The
   * merged residue schema"). Only meaningful once this frame's `ctx.effect`
   * has finished disposing after an ABORT — nothing in the commit-path
   * pseudocode can add a residue record, so `outstanding` is always empty
   * after a clean unload. */
  report(): AbortReport {
    const outstanding = [...this.residue]
    const worldRemaining = [...new Set(outstanding.map((r) => r.referent))]
    return { clean: outstanding.length === 0, outstanding, worldRemaining, proof: this.proof(outstanding) }
  }

  private proof(outstanding: ResidueRecord[]): string {
    if (outstanding.length === 0) {
      return `${this.name}: every Phase-1 inverse and Phase-2 compensation completed; no residue.`
    }
    const counts: Record<string, number> = {}
    for (const r of outstanding) counts[r.kind] = (counts[r.kind] ?? 0) + 1
    const parts = Object.entries(counts).map(([k, n]) => `${n} ${k}`).join(', ')
    return `${this.name}: ${outstanding.length} residue record(s) (${parts}) — never claiming a ` +
      "dead closure ran; see 'outstanding' for what to check by hand."
  }
}

// ---------------------------------------------------------------------------
// Adapter glue

export interface ConfigFieldSpec {
  required?: boolean
  default?: unknown
  /** The author declared this field `Secret[T]` (item 256 Slice 3 / 421 F6).
   *  The component still receives the real value — it was granted it; the
   *  `<Component>.config` trace line does not. */
  secret?: boolean
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
  // item 256 Slice 3 / 421 F6: a field the author declared `Secret[T]` is a
  // credential the component was granted and the trace was not. Remember the
  // value (so it is scrubbed wherever else it surfaces) and render the field as
  // the placeholder — still named, still in position, so the line keeps saying
  // which fields resolved and which defaulted.
  const secretFields = new Set(
    Object.entries(spec).filter(([, f]) => f.secret).map(([field]) => field))
  for (const field of secretFields) rememberSecret(config[field])
  // `JSON.stringify` THROWS on a BigInt, and an `Int` config field is a bigint
  // on this tier — so the trace line has to render one itself. Digits with no
  // `n` suffix and no quotes: the same text python/rust/java/go write for the
  // same value, which is what keeps the cross-tier trace comparison honest.
  const show = (value: unknown) =>
    typeof value === 'bigint' ? value.toString() : JSON.stringify(value)
  const body = Object.keys(config)
    .sort()
    .map((key) => `${key}=`
      + (secretFields.has(key) ? JSON.stringify(REDACTED_SECRET) : show(config[key])))
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
  // time as a coeffect (item 57): a timer body compiles to `host.scheduleEvery`
  // / `host.scheduleAfter`, whose handle's `cancel()` is the schedule's inverse.
  scheduleEvery,
  scheduleAfter,
  // item 102: an `advance` lifecycle statement compiles to `host.clockAdvance`,
  // the only in-language way to move the clock coeffect and so exercise a
  // timer's firing; `host.clockReset` isolates one lifecycle test's timeline.
  clockAdvance: (ms: number): number => Clock.advance(ms),
  clockReset: (): void => Clock.reset(),
  applyConfigDefaults,
  // item 421 F6: the two ends of a declared `Secret[T]` marking, called by
  // emitted code — `markSecret` at the head of a provide method that declares a
  // `Secret[T]` parameter (the receiver), `secretResult` around the return of an
  // extern whose declared return was `Secret[T]` (the origin).
  markSecret,
  secretResult,
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
