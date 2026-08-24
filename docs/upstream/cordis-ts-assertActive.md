# Upstream PR draft — cordis (TS): refuse effect registration while a fiber is UNLOADING

> **Status: DRAFT — do NOT open this PR without the coordinator's explicit
> confirmation.** This is the roadmap 74(a) deliverable: the fix is pinned in
> the revl fork (`inso1337/cordis@harden-assert-active`, commit
> `c8b94b2`), the PR text below is what would go upstream once confirmed.
> It feeds the review of [cordiverse/cordis#39](https://github.com/cordiverse/cordis/pull/39)
> (reentrant fiber lifecycle).

---

## Title

`fix(core): refuse effect registration while a fiber is UNLOADING`

## Summary

`Fiber.assertActive()` only checked `uid !== null` — "not disposed", not
"not unloading". During a *deactivation* (a requirement is withdrawn and the
fiber survives as `PENDING`), an undo that calls `ctx.effect(...)` was
accepted, and the resulting disposer leaked permanently:

1. the new effect executed and its disposer was pushed into
   `fiber._disposables` — *after* `_unload` already `clear()`ed the list;
2. the fiber ended `PENDING` while still holding an effect
   (`fiber.getEffects().length > 0` on an inactive fiber);
3. even a later `fiber.dispose()` never ran that disposer, because the epoch
   is already `INACTIVE` and no further unload is triggered — **permanent
   residue**.

The fix adds the `UNLOADING` lifecycle state to the guard:

```ts
assertActive() {
  if (this.uid !== null && this.state !== FiberState.UNLOADING) return
  throw new CordisError('INACTIVE_EFFECT')
}
```

`effect()`, `ctx.on()`, `ctx.plugin()`, `restart()` and `update()` all route
through `assertActive`, so every effect/listener/plugin registration during
teardown is now refused with `INACTIVE_EFFECT` — the same error a
fully-disposed fiber already raises, closing the reentrancy asymmetry where
the guard worked on full disposal but not on deactivation.

## Repro

```ts
import { Context, FiberState } from 'cordis'

let leaked = false
let leakDisposed = false

const Provider = {
  name: 'Provider',
  apply(ctx: any) {
    ctx.provide('svc', {})
  },
}
const Rogue = {
  name: 'Rogue',
  inject: ['svc'],
  apply(ctx: any) {
    ctx.effect(function* () {
      yield () => {
        // Teardown registering a new effect.
        ctx.effect(() => {
          leaked = true
          return () => {
            leakDisposed = true
          }
        })
      }
    })
  },
}

const ctx = new Context()
const provider = await ctx.plugin(Provider)
const rogue = await ctx.plugin(Rogue)

await provider.dispose() // withdraw svc -> Rogue deactivates -> undo runs

console.log('leaked:', leaked)             // before fix: true (accepted)
console.log('effects:', rogue.getEffects().length) // before fix: 1 (residue)
console.log('state:', FiberState[rogue.state])     // PENDING both ways

await rogue.dispose()
console.log('leakDisposed:', leakDisposed) // before fix: false — permanent residue
```

After the fix the undo's `ctx.effect` throws `INACTIVE_EFFECT` (swallowed
into the fiber logger by the unload pass, matching how disposer errors are
handled), `leaked` stays `false`, and `rogue.getEffects().length` is `0`.
Pinned in revl as `backends/typescript/tests/upstream.test.ts` ("finding 2"),
which was a red-on-fix characterization test and now pins the fixed behavior.

## Notes for reviewers

- The existing `uid !== null` check is kept: it is the DISPOSED guard, and it
  also covers the synchronous `uid = null` prefix of the disposal disposer
  before the state transition lands. The new check is additive.
- A fiber in `FAILED` state still passes (as before): `update()` on a failed
  fiber is a legitimate recovery path and must not start throwing.
- The one-generator-per-effect lowering that revl's emitter uses is
  unaffected; this closes the runtime-side hole for hand-written plugins.
- Related: `feat/reentrant-fiber-lifecycle` (PR #39) puts an equivalent
  `UNLOADING` guard inside `effect()`; this change places it in
  `assertActive()` so `ctx.on()`, `ctx.plugin()`, `restart()` and `update()`
  inherit the same protection. The two approaches can be reconciled during
  the #39 review.
