// Consumer-side REACTIVE realm resolution on the real cordis v4 runtime.
//
// tests/v2_realms.test.ts already proves realm SEPARATION and realm-local
// withdrawal, but it plugs every provider BEFORE its consumer — so the
// consumer's requirement is already satisfied at plugin time (eager
// resolution). The cross-tier gate (tests/test_realm_conformance.py) likewise
// asserts only PROVIDER-side separation.
//
// The behaviour neither exercises is the reactive path: a consumer plugged
// FIRST stays PENDING, and then ACTIVATES when — and only when — a provider
// appears in ITS OWN realm. This is exactly the behaviour reported broken on
// the rust tier, so pinning it down on ts fixes the cross-tier expectation:
// an isolated consumer in realm("tenant_a") must resolve the isolated
// provider in realm("tenant_a"), and must NOT resolve one in realm("tenant_b").
//
// Fixture: tests/fixtures/tenants.ir.json (TenantAApp requires kv @tenant_a;
// TenantAStore provides kv @tenant_a; TenantBStore provides kv @tenant_b).
import { beforeEach, describe, expect, it } from 'vitest'
import { Context, FiberState } from 'cordis'
import { TenantAApp, TenantAStore, TenantBStore } from './generated/tenants.ts'
import { fiberStateName, plug, resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

describe('v2 realms — consumer-side reactive resolution', () => {
  it('activates a consumer plugged BEFORE its same-realm provider appears', async () => {
    const ctx = new Context()

    // Consumer first: nothing provides kv@tenant_a yet, so it must wait.
    const app = await plug(ctx, TenantAApp)
    expect(app.state, `expected PENDING, got ${fiberStateName(app.state)}`).toBe(
      FiberState.PENDING,
    )

    // The same-realm provider appears -> the consumer resolves and activates
    // reactively, and its body ran against THAT provider (who == 'alice').
    const store = await plug(ctx, TenantAStore)
    await store.await()
    await app.await()
    expect(app.state, `expected ACTIVE, got ${fiberStateName(app.state)}`).toBe(
      FiberState.ACTIVE,
    )
    expect(app.ctx.kv.get('who')).toBe('alice')

    await app.dispose()
    await store.dispose()
  })

  it('does NOT resolve a consumer to a DIFFERENT-realm provider', async () => {
    const ctx = new Context()

    // Consumer isolated into realm tenant_a.
    const app = await plug(ctx, TenantAApp)
    expect(app.state).toBe(FiberState.PENDING)

    // A provider of the same KEY but in realm tenant_b must not satisfy it:
    // realm isolation is observed on the CONSUMER side, not just the provider.
    const otherRealm = await plug(ctx, TenantBStore)
    await otherRealm.await()
    expect(
      app.state,
      `consumer wrongly resolved across realms (${fiberStateName(app.state)})`,
    ).toBe(FiberState.PENDING)

    // Now the correct-realm provider arrives -> it finally activates.
    const sameRealm = await plug(ctx, TenantAStore)
    await sameRealm.await()
    await app.await()
    expect(app.state).toBe(FiberState.ACTIVE)
    expect(app.ctx.kv.get('who')).toBe('alice')

    await app.dispose()
    await sameRealm.dispose()
    await otherRealm.dispose()
  })

  it('re-suspends a consumer when its same-realm provider withdraws, then re-activates on re-provision', async () => {
    const ctx = new Context()
    const store = await plug(ctx, TenantAStore)
    const app = await plug(ctx, TenantAApp)
    await app.await()
    expect(app.state).toBe(FiberState.ACTIVE)

    // Withdrawal deactivates the dependent consumer (the withdrawal half of
    // reactive resolution).
    await store.dispose()
    expect(app.state).not.toBe(FiberState.ACTIVE)

    // Re-provision in the same realm re-activates it — reactive resolution is
    // not a one-shot latch.
    const store2 = await plug(ctx, TenantAStore)
    await store2.await()
    await app.await()
    expect(app.state).toBe(FiberState.ACTIVE)
    expect(app.ctx.kv.get('who')).toBe('alice')

    await app.dispose()
    await store2.dispose()
  })
})
