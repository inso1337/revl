// Instance-parametric components (`spawn`), executed on the real cordis v4
// runtime (docs/design-v2-instances.md phase 2, cordis (TS) tier).
//
// A `spawn` is an acquisition whose inverse is the instance's own teardown; the
// instance lives in its own fresh LOCAL realm and its own nested teardown scope
// (a child fiber). This drives an emitted revl composition on real cordis and
// asserts, by RUNNING, the four properties the feature exists to provide:
//
//   1. two live instances of one component coexist in DISTINCT local realms
//      (both provide the same key `counter`, non-colliding);
//   2. disposing one runs ITS LIFO teardown and leaves the others live;
//   3. a request-scoped instance is reclaimed at `dispose()`, NOT deferred to
//      the parent component's teardown (the anti-leak property, proven directly
//      by driving the supervisor's `retire_a` operation);
//   4. supervision-tree addressing — the spawner reaches its instance through
//      the handle it alone holds; the root (a stand-in for any sibling) cannot.
//
// The composition is emitted from tests/fixtures/spawn.ir.json (the frozen
// phase-1 IR) by the vitest global-setup, exactly like every other generated
// fixture. Worker holds a traced host Map (its new/drop mark birth and
// reclamation) and provides `counter` into its own realm; Supervisor spawns two
// workers in its activation body and exposes `retire_a`, which disposes worker A
// through the handle it alone holds.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { Supervisor } from './generated/spawn.ts'
import { onHostEvent, plug, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

/** Let every reactive/child-fiber activation and disposal settle. */
async function flush(): Promise<void> {
  for (let i = 0; i < 24; i++) await Promise.resolve()
}

/** Run the composition and return an observation record built from the host
 * event stream and live-fiber introspection. */
async function drive() {
  const events: string[] = []
  const off = onHostEvent((e) => events.push(e))
  try {
    const ctx = new Context()
    const supervisor = await plug(ctx, Supervisor)
    await flush()

    // (1) both instances live: two distinct Maps were created, one per worker.
    // Map serials are process-global, so identify the two workers' resources by
    // spawn order — the first `.new` is worker A's, the second is worker B's.
    const news = events.filter((e) => e.endsWith('.new'))
    const mapA = news[0]?.split('.')[0]
    const mapB = news[1]?.split('.')[0]

    const obs = {
      newCount: news.length,
      mapA,
      mapB,
      // (1b)/(4) each worker's `counter` is in its OWN local realm, so the
      // shared realm (root ctx) never sees it — no collision between providers,
      // and no sibling/root addressing.
      sharedCounter: (ctx as any).counter as unknown,
      supervisorActiveAfterLoad: supervisor.state === FiberState.ACTIVE,
      afterRetire: [] as string[],
      supervisorActiveAfterRetire: false,
      afterSupervisorDispose: [] as string[],
    }

    // (3)/(2) retire worker A through the supervisor (the only holder of its
    // handle). Its teardown must run NOW, not at supervisor teardown, and must
    // leave the supervisor and worker B untouched.
    events.length = 0
    await (ctx as any).ctl.retire_a()
    await flush()
    obs.afterRetire = [...events]
    obs.supervisorActiveAfterRetire = supervisor.state === FiberState.ACTIVE

    // (2) disposing the supervisor reclaims the remaining worker (worker B) —
    // no leak — and only it (worker A is already gone).
    events.length = 0
    await supervisor.dispose()
    await flush()
    obs.afterSupervisorDispose = [...events]

    return obs
  } finally {
    off()
  }
}

describe('instance-parametric components (spawn) on real cordis', () => {
  it('(1) two instances coexist in distinct local realms (non-colliding)', async () => {
    const obs = await drive()
    // two live instances — two distinct Maps, one per worker
    expect(obs.newCount).toBe(2)
    expect(obs.mapA).toBeDefined()
    expect(obs.mapB).toBeDefined()
    expect(obs.mapA).not.toBe(obs.mapB)
    // each worker's provision is in its OWN fresh local realm, so the shared
    // realm never sees `counter` — the two providers cannot have collided
    expect(obs.sharedCounter).toBeUndefined()
    expect(obs.supervisorActiveAfterLoad).toBe(true)
  })

  it('(3)/(2) a request-scoped instance is reclaimed at dispose(), not deferred', async () => {
    const obs = await drive()
    // (3) worker A's teardown (its Map.drop) ran at retire_a(), NOT at
    // supervisor teardown — the anti-leak property, proven directly
    expect(obs.afterRetire).toEqual([`${obs.mapA}.drop`])
    // (2) the supervisor and the sibling instance stayed live
    expect(obs.supervisorActiveAfterRetire).toBe(true)
    // the remaining worker (B) is reclaimed only when the supervisor tears down
    expect(obs.afterSupervisorDispose).toEqual([`${obs.mapB}.drop`])
  })

  it('(4) supervision-tree addressing — only the spawner reaches its instance', async () => {
    const obs = await drive()
    // the root context — a stand-in for any sibling or outside party — cannot
    // resolve the worker's provision: it lives in a private local realm
    expect(obs.sharedCounter).toBeUndefined()
    // two workers, two disjoint realms: neither collided on `counter`, which is
    // only possible if each is a distinct local realm
    expect(obs.newCount).toBe(2)
  })
})
