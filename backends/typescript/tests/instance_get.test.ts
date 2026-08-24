// Instance accessor (`s.<key>`) executed on the real cordis v4 runtime
// (docs/design-v2-instances.md "Instance accessor — frozen", cordis (TS) tier).
//
// `s.<key>` reads a provision back off a spawn handle: it resolves `key`
// through the instance's OWN private local realm — the realm the matching
// `spawn` isolated it into — yielding THAT instance's provision and no other's.
// The emitter lowers the frozen `instance-get` IR node to `<handle>.get(key)`
// (runtime `SpawnHandle.get`, ../runtime.ts). This drives an emitted
// composition on real cordis and asserts, by RUNNING, the three DoD properties:
//
//   1. positive supervision direction — `s.<key>.method(..)` through a spawn
//      handle returns THAT spawned instance's provision value;
//   2. negative — root (a stand-in for any sibling) cannot resolve the
//      instance's provision;
//   3. two live instances resolve their OWN realm, not each other's (the
//      handles stay on the supervision tree, disjoint by construction).
//
// Worker provides `counter` into its own fresh local realm and its `value()`
// returns the instance's `seed` config, so two workers spawned with distinct
// seeds are distinguishable through their handles. Supervisor spawns w1 (seed 7)
// and w2 (seed 9) and exposes `read_a`/`read_b`, each reading its own worker's
// `counter` back through the handle it alone holds.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import { Supervisor } from './generated/instance_get.ts'
import { plug, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

async function flush(): Promise<void> {
  for (let i = 0; i < 24; i++) await Promise.resolve()
}

describe('instance accessor (s.<key>) on real cordis', () => {
  it('reads THAT instance provision back, stays on the supervision tree', async () => {
    const ctx = new Context()
    await plug(ctx, Supervisor)
    await flush()

    // (2) negative: the provision is isolated into each worker's own local
    // realm, so the root context (a stand-in for any sibling) never sees
    // `counter` — supervision-tree addressing, not a global.
    expect((ctx as any).counter).toBeUndefined()

    // (1) positive: reading `counter` back through the handle the supervisor
    // alone holds resolves THAT instance's provision. read_a goes through w1's
    // handle, read_b through w2's.
    const a = (ctx as any).ctl.read_a() as bigint
    const b = (ctx as any).ctl.read_b() as bigint

    // (1)/(3) each handle resolves its OWN realm: w1 returns its seed (7),
    // w2 returns its seed (9). Neither reaches the other's provision — if the
    // accessor resolved a shared/root realm both would collide on one value.
    expect(a).toBe(7n)
    expect(b).toBe(9n)
    expect(a).not.toBe(b)
  })
})
