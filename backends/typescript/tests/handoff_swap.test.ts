// Verified provider state hand-off on hot-swap, executed on the real cordis v4
// runtime (item 53, docs/state-handoff.md) — the ts peer of py's
// tests/test_state_handoff_exec.py.
//
// This tier has no MCP session engine: a host swaps a provider by replacing its
// fiber, so the swap primitive is `runtime.swapProvider`. These assertions run
// the same exit criterion the py tier does, through that primitive:
//
//   * a stateful provider (a cache holding entries) is swapped to a compatible
//     successor and its entries SURVIVE — a warm start, not a cold restart —
//     while a live `version()` proves the successor's code is running;
//   * a successor that declares no hand-off opts out: it starts cold, no
//     refusal (a valid, if lossy, author choice);
//   * a successor whose runtime resource vector cannot hold the state is
//     rejected AND the whole swap is rolled back: the predecessor is reloaded
//     with its captured state re-seated, so nothing is dropped and the
//     composition keeps serving;
//   * a successor that fails to activate is rolled back the same way;
//   * a successor moved to another realm starts cold: a realm-scoped store is
//     a different provider there, so its entries must not cross.
//
// The fixture `tests/generated/handoff_swap.ts` is emitted from
// tests/fixtures/handoff_swap.ir.json, whose `CacheV2` was admitted against the
// running manifest with `replacing=("Cache",)` (fixtures/_gen_handoff_swap.py):
// the declared-shape §5 gate has already run in the compiler, exactly as it
// does for a py swap, before these modules exist. The runtime vector check and
// rollback below are the defence-in-depth half that a swap engine owns.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import { Cache, CacheV2 } from './generated/handoff_swap.ts'
import {
  captureProviderState,
  Frame,
  MapHandle,
  plug,
  resetHost,
  StateIncompatible,
  SwapRejected,
  swapProvider,
  type HandoffComponent,
} from '../runtime.ts'

beforeEach(() => resetHost())

/** Let every reactive activation and disposal settle. */
async function flush(): Promise<void> {
  for (let i = 0; i < 24; i++) await Promise.resolve()
}

/** Load the running generation and seed two entries. */
async function seeded() {
  const ctx = new Context()
  const fiber = await ctx.plugin(Cache)
  await flush()
  ctx.cache.put('alice', '42')
  ctx.cache.put('bob', '7')
  return { ctx, fiber }
}

/** A successor that re-provides `cache` but declares no hand-off: the author
 *  opted out of inheriting state, so it must start cold. */
const ColdCache: HandoffComponent = {
  name: 'ColdCache',
  inject: [],
  provide: ['cache'],
  apply(ctx: Context) {
    const m = new globalThis.Map<string, string>()
    ctx.effect(() => ctx.provide('cache', {
      get: (k: string) => m.get(k),
      put: (k: string, v: string) => { m.set(k, v) },
      version: () => 2n,
    }))
  },
}

/** A successor that DECLARES the hand-off but acquires no stateful resource:
 *  its vector is empty, so the captured Map cannot migrate and the swap must
 *  roll back rather than drop the predecessor's entries. */
const ZeroStateCache: HandoffComponent = {
  name: 'ZeroStateCache',
  inject: [],
  provide: ['cache'],
  handoff: { key: 'cache', type: 'Map[Str, Str]' },
  apply(ctx: Context) {
    ctx.effect(() => ctx.provide('cache', {
      get: (_k: string) => undefined,
      put: (_k: string, _v: string) => {},
      version: () => 9n,
    }))
  },
}

describe('state hand-off on a ts provider swap', () => {
  it('seats the declared state in a compatible successor (warm start)', async () => {
    const { ctx, fiber } = await seeded()
    expect(ctx.cache.version()).toBe(1n) // old code is live
    expect(captureProviderState(fiber)).toHaveLength(1) // the declared Map

    const { fiber: successor, handoff } = await swapProvider(ctx, {
      from: Cache,
      fiber,
      to: CacheV2,
    })

    // the swap reports what state it carried across
    expect(handoff).toEqual({
      cache: { component: 'CacheV2', migrated: true, resources: 1 },
    })
    // ... the successor's CODE is live ...
    expect(ctx.cache.version()).toBe(2n)
    // ... AND the entries survived (warm, not cold-restarted)
    expect(ctx.cache.get('alice')).toBe('42')
    expect(ctx.cache.get('bob')).toBe('7')

    await successor.dispose()
    await flush()
  })

  it('starts cold when the successor declares no hand-off', async () => {
    const { ctx, fiber } = await seeded()

    const { fiber: cold, handoff } = await swapProvider(ctx, {
      from: Cache,
      fiber,
      to: ColdCache,
    })

    // nothing threaded, and the successor runs its own code with no entries
    expect(handoff).toBeUndefined()
    expect(ctx.cache.version()).toBe(2n)
    expect(ctx.cache.get('alice')).toBeUndefined()

    await cold.dispose()
    await flush()
  })

  it('rolls back when the successor cannot hold the captured state', async () => {
    const { ctx, fiber } = await seeded()

    await expect(swapProvider(ctx, {
      from: Cache,
      fiber,
      to: ZeroStateCache,
    })).rejects.toBeInstanceOf(StateIncompatible)

    // the predecessor is serving again, on its own code, state intact —
    // nothing dropped, nothing half-migrated
    expect(ctx.cache.version()).toBe(1n)
    expect(ctx.cache.get('alice')).toBe('42')
    expect(ctx.cache.get('bob')).toBe('7')
  })

  it('rolls back when the successor fails to activate', async () => {
    const { ctx, fiber } = await seeded()

    const Exploding: HandoffComponent = {
      name: 'Exploding',
      inject: [],
      provide: ['cache'],
      handoff: { key: 'cache', type: 'Map[Str, Str]' },
      apply(_ctx: Context) {
        throw new Error('successor activation failed')
      },
    }

    await expect(swapProvider(ctx, {
      from: Cache,
      fiber,
      to: Exploding,
    })).rejects.toBeInstanceOf(SwapRejected)

    // the rollback reloaded the predecessor and re-seated its state
    expect(ctx.cache.version()).toBe(1n)
    expect(ctx.cache.get('alice')).toBe('42')
    expect(ctx.cache.get('bob')).toBe('7')
  })
})

/** A stateful cache component a test can place in a named realm by hand. It
 *  tracks its Map on the activation frame exactly as the emitter does, so the
 *  swap primitive sees a real state vector. */
function placedCache(
  name: string,
  isolate: Record<string, string> | undefined,
  version: bigint,
): HandoffComponent {
  return {
    name,
    inject: [],
    provide: ['cache'],
    ...(isolate === undefined ? {} : { isolate }),
    handoff: { key: 'cache', type: 'Map[Str, Str]' },
    apply(ctx: Context) {
      const frame = new Frame(ctx, name)
      const m = frame.track(new MapHandle())
      ctx.effect(() => ctx.provide('cache', {
        get: (k: string) => m.get(k),
        put: (k: string, v: string) => { m.insert(k, v) },
        version: () => version,
      }))
    },
  }
}

describe('state hand-off and realm placement', () => {
  it('starts cold when the successor moved to another realm', async () => {
    const ctx = new Context()
    const running = placedCache('TenantA', { cache: 'tenant_a' }, 1n)
    const fiber = await plug(ctx, running)
    await flush()
    fiber.ctx.cache.put('alice', '42')

    const moved = placedCache('TenantB', { cache: 'tenant_b' }, 2n)
    const { fiber: successor, handoff } = await swapProvider(ctx, {
      from: running,
      fiber,
      to: moved,
    })

    expect(handoff).toBeUndefined()
    expect(successor.ctx.cache.version()).toBe(2n)
    expect(successor.ctx.cache.get('alice')).toBeUndefined()

    await successor.dispose()
    await flush()
  })

  it('carries state between two components placed in the same realm', async () => {
    const ctx = new Context()
    const running = placedCache('TenantA', { cache: 'tenant_a' }, 1n)
    const fiber = await plug(ctx, running)
    await flush()
    fiber.ctx.cache.put('alice', '42')

    const same = placedCache('TenantA2', { cache: 'tenant_a' }, 2n)
    const { fiber: successor, handoff } = await swapProvider(ctx, {
      from: running,
      fiber,
      to: same,
    })

    expect(handoff).toEqual({
      cache: { component: 'TenantA2', migrated: true, resources: 1 },
    })
    expect(successor.ctx.cache.version()).toBe(2n)
    expect(successor.ctx.cache.get('alice')).toBe('42')

    await successor.dispose()
    await flush()
  })
})
