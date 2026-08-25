// Routed-require lowering on the real cordis v4 runtime (roadmap item 167).
//
// A component that `requires worker in realms("w1","w2","w3") strategy(...)`
// and `provides worker` must, in its EMITTED body, fan each call out across the
// worker realms and fail over when one withdraws — the emitter's realization of
// what src/revl/run.py::_Router does in the py-tier driver. This is the
// end-to-end proof that the emitted Router body itself routes (item 161 left
// the routing in the driver; item 167 moves it into the emitted body).
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { W1, W2, W3, Router } from './generated/router.ts'
import { plug, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

// The single `worker` provider a consumer resolves in the parent realm — the
// routing proxy's provision (G2 holds downstream through failover).
function router(ctx: Context): any {
  return ctx.reflect.get('worker')
}

describe('item 167 routed require', () => {
  it('distributes round-robin from the emitted Router body', async () => {
    const ctx = new Context()
    const fibers = {
      W1: await plug(ctx, W1),
      W2: await plug(ctx, W2),
      W3: await plug(ctx, W3),
      Router: await plug(ctx, Router),
    }
    for (const [name, fiber] of Object.entries(fibers)) {
      expect(fiber.state, `${name} is ${FiberState[fiber.state]}`).toBe(FiberState.ACTIVE)
    }

    const worker = router(ctx)
    expect(worker, 'Router must provide `worker` in the parent realm').toBeTruthy()

    // six calls rotate w1,w2,w3,w1,w2,w3 in declaration order — the emitted
    // body is what fans out (no py driver in this test).
    const got = Array.from({ length: 6 }, (_x, i) => worker.call(String(i)))
    expect(got).toEqual(['w1:0', 'w2:1', 'w3:2', 'w1:3', 'w2:4', 'w3:5'])
  })

  it('fails over when a worker withdraws, and keeps one provider (G2)', async () => {
    const ctx = new Context()
    const fibers = {
      W1: await plug(ctx, W1),
      W2: await plug(ctx, W2),
      W3: await plug(ctx, W3),
      Router: await plug(ctx, Router),
    }
    const worker = router(ctx)

    expect(worker.call('a')).toBe('w1:a')
    // withdraw w2's provider: its realm resolves to a non-ACTIVE handle
    // (reflect.get -> nullish) and drops out of the live set.
    await fibers.W2.dispose()
    expect(fibers.W2.state).not.toBe(FiberState.ACTIVE)

    // the next six calls skip w2 and spread across the survivors, re-resolved
    // per call — reactive failover from the emitted body.
    const got = Array.from({ length: 6 }, (_x, i) => worker.call(String(i)))
    expect(got.every((r) => r.startsWith('w1:') || r.startsWith('w3:'))).toBe(true)
    expect(got.some((r) => r.startsWith('w2:'))).toBe(false)

    // a survivor is still the sole downstream provider (G2 holds through
    // failover): the consumer keeps resolving exactly this one proxy.
    expect(router(ctx)).toBe(worker)
  })

  it('raises when every realm has withdrawn', async () => {
    const ctx = new Context()
    const fibers = {
      W1: await plug(ctx, W1),
      W2: await plug(ctx, W2),
      W3: await plug(ctx, W3),
      Router: await plug(ctx, Router),
    }
    const worker = router(ctx)
    await fibers.W1.dispose()
    await fibers.W2.dispose()
    await fibers.W3.dispose()

    expect(() => worker.call('x')).toThrow(/no live worker/)
  })
})
