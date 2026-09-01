# cordis4j fork — `Context.serviceInRealm` (revl item 173)

Upstream: `github.com/1na-ko/cordis4j` (the Java realization of the Cordis
paradigm; referenced by `backends/java/emit.py`). Unlike stc-go, no real
cordis4j runtime is reachable in this environment — only the javac stubs under
`backends/java/stubs/io/cordis4j/core/` — and there is NO Java Runtime here
(`java`/`javac` report "Unable to locate a Java Runtime"). So this tier's
runtime primitive is delivered as a PR spec plus a concrete reference
implementation in the stub; it could NOT be built or run here.

## The primitive

`<T> java.util.Optional<T> Context.serviceInRealm(Class<T> type, String realm)`:
a STRICT, single-realm, liveness-checked read that resolves `type` only in
realm `realm`, with **no parent/root fallback**. An empty Optional means that
realm has no ACTIVE provider (map membership is liveness: `provide` inserts, its
`Disposable` removes). This is the read a router's emitted body needs so a
withdrawn worker realm drops out of the live set rather than resolving to the
router's own root provision (which the fallback in `ctx.get` would return — an
infinite self-route).

## Why upstream needs it

The stub's original `Context.get` walked a flat registry with no realm scoping
(`isolate` returned `this`), so a router had no way to (a) place a worker in a
named realm or (b) read one realm strictly. The real cordis4j resolves along a
realm/parent chain like stc-go's `resolve`; a strict per-realm read is the
missing primitive.

## Reference implementation (delivered here)

`backends/java/stubs/io/cordis4j/core/Context.java` is upgraded to a realm-aware
registry (a shared root map + one map per named realm; `isolate` returns a view
that binds a service type to a realm; `provide` publishes into the resolved
realm) and adds `serviceInRealm`. This is the concrete shape the upstream PR
should take. It is type-correct against the emitted router class; it was NOT
javac-compiled or run here (no JRE).

## PR spec (upstream `github.com/1na-ko/cordis4j`)

- Title: `feat: Context.serviceInRealm — strict single-realm liveness-checked read`
- Add `serviceInRealm(Class<T>, String realm) -> Optional<T>` reading the one
  realm's committed provider table with no parent-chain fallback; make
  `isolate`/`provide` realm-scope the provider (if not already), matching the
  reference in the stub `Context.java`.
- Additive: no change to existing `get`/`provide` semantics for the root realm,
  so every non-routing program resolves exactly as before.
- Rationale: enables a multi-realm router's emitted body (backends/java/emit.py,
  `_emit_java_router_class`) to fail over per realm without the parent-chain
  fallback re-entering the router's own provision.

## Build / test status HERE

NOT built or run: no Java Runtime in this environment. The emitter side is
proven by `backends/java/test_router_emit.py` (pure-Python assertions) and the
byte-identity goldens; the runtime side awaits a JRE + the upstream PR.
