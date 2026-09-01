# stc-go fork — `ServiceInRealm` / `LiveInRealm` (revl item 173)

This directory is a WRITABLE fork of the upstream go runtime
`github.com/0xdenny218/stc-go` (pinned in `backends/go/placement_runner/go.mod`),
copied from the read-only module cache so the item-173 routing primitive can be
built and tested here. Only ONE new file is added; nothing existing is touched.

## The primitive (`route.go`)

`ServiceInRealm[T](c *Context, key Key, r *Realm) (T, bool)` and its liveness
half `LiveInRealm(c, key, r) bool`: a STRICT, single-realm, liveness-checked
read that resolves `key` only in realm `r` with **no parent-chain fallback**.

### Why upstream needs it

`Context.resolve` (context.go) walks the realm chain to the root:

    for r := c.realmForLocked(key); r != nil; r = r.parent { ... }

A router provides the bare key in the parent (root) realm so its consumers see
one provider (Definition 58 well-formedness). So when a worker realm's provider
withdraws, `resolve` falls back up the chain and finds the ROUTER's own
provision — the router routes to itself instead of dropping the dead realm out
of its live set. `ServiceInRealm` reads `provKey{realm, key}` directly (the same
committed table, pinned to one realm), and map membership is liveness (Provide
inserts, its Inverse/removeProvide deletes), so a withdrawn realm reports
`(zero, false)` and drops out — reactive failover from the router's emitted body.

`TestIsolateRealm` in the upstream suite documents the fallback this avoids;
`route_test.go` here proves the strict behavior and a 3-realm pool failover.

## Build / test status HERE

Built and tested with go1.26.5:

    cd forks/stc-go && go test -run TestServiceInRealm ./     # ok

## PR spec (upstream `github.com/0xdenny218/stc-go`)

- Title: `feat: ServiceInRealm — strict single-realm liveness-checked read`
- Add `route.go` (this file's `ServiceInRealm` + `LiveInRealm`) and
  `route_test.go`. No change to existing exported API; purely additive.
- Rationale: enables a multi-realm router's emitted body to fail over per realm
  without the parent-chain fallback re-entering the router's own provision.
- Once released, bump the pin in `backends/go/placement_runner/go.mod` and drop
  the local `replace` directive the revl build uses to consume this fork.
