// v2 realms & interception on the real cordis v4 runtime: two providers of
// one key in two realms; each consumer observes ITS OWN provider; intercept
// metadata is visible on the consumer's context chain.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState, symbols } from 'cordis'
import {
  TenantAApp,
  TenantAStore,
  TenantBApp,
  TenantBStore,
} from './generated/tenants.ts'
import { plug, realmLabel, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

describe('v2 realms', () => {
  it('gives each consumer its own provider and keeps withdrawal realm-local', async () => {
    const ctx = new Context()
    const fibers = {
      TenantAStore: await plug(ctx, TenantAStore),
      TenantBStore: await plug(ctx, TenantBStore),
      TenantAApp: await plug(ctx, TenantAApp),
      TenantBApp: await plug(ctx, TenantBApp),
    }
    for (const [name, fiber] of Object.entries(fibers)) {
      expect(fiber.state, `${name} is ${FiberState[fiber.state]}`).toBe(FiberState.ACTIVE)
    }

    // Each app wrote through ITS realm's provider: the stores disagree.
    const aKv = fibers.TenantAApp.ctx.kv
    const bKv = fibers.TenantBApp.ctx.kv
    expect(aKv.get('who')).toBe('alice')
    expect(bKv.get('who')).toBe('bob')
    expect(aKv).not.toBe(bKv)

    // Withdrawal stays realm-local: unloading A's store deactivates A's app
    // only; tenant B is untouched (reactive resolution per realm).
    await fibers.TenantAStore.dispose()
    expect(fibers.TenantAApp.state).not.toBe(FiberState.ACTIVE)
    expect(fibers.TenantBApp.state).toBe(FiberState.ACTIVE)
    expect(bKv.get('who')).toBe('bob')

    await fibers.TenantBApp.dispose()
    await fibers.TenantBStore.dispose()
  })

  it('makes intercept metadata visible on the consumer context chain', async () => {
    const ctx = new Context()
    const store = await plug(ctx, TenantAStore)
    const app = await plug(ctx, TenantAApp)
    expect(app.state).toBe(FiberState.ACTIVE)

    // cordis v4's equivalent of cordis-py's _effective_intercept(): the
    // dict-form inject entries are copied onto the fiber context's intercept
    // chain (root -> leaf), so a consumer observes its own declared metadata.
    expect(app.ctx[symbols.intercept].kv).toEqual({ quota: 5, tags: ['tenant_a'] })

    await app.dispose()
    await store.dispose()
  })

  it('shares realm labels by value', () => {
    expect(realmLabel('t')).toBe(realmLabel('t'))
    expect(realmLabel('t')).not.toBe(realmLabel('u'))
  })
})
