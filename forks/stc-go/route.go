package stc

// route.go — the strict single-realm liveness-checked read (revl item 173).
//
// The multi-realm router pattern (paper §load-balancing; revl docs/router.md)
// fans one service key out across N NAMED worker realms and forwards each call
// to a live worker, failing over when one withdraws. The router's own emitted
// body needs a read that answers exactly one question per realm:
//
//	"does realm r ITSELF currently have an ACTIVE provider of key — with NO
//	 fallback to a parent realm?"
//
// The existing resolve() (context.go) cannot answer it: it walks the realm
// chain to the root (`for r := c.realmForLocked(key); r != nil; r = r.parent`).
// A router provides the bare key in the PARENT (root) realm so its consumers
// see one provider (well-formedness, Definition 58). So when a worker realm's
// provider withdraws, resolve() falls back up the chain and finds the ROUTER's
// own provision — the router would route to itself instead of dropping the dead
// realm out of the live set. That is the exact gap this primitive closes.
//
// ServiceInRealm reads provKey{realm, key} DIRECTLY — the same committed table
// resolve() reads, but pinned to one realm with no parent walk. Membership in
// the provides map IS liveness: Provide inserts the entry and its Inverse /
// removeProvide deletes it on withdrawal, so a realm with no live provider is
// simply absent, and ServiceInRealm reports (zero, false). This makes a
// withdrawn worker realm drop out of the router's live set (reactive failover
// from the emitted body) rather than resolve to a parent's provider.
func ServiceInRealm[T any](c *Context, key Key, r *Realm) (T, bool) {
	if r == nil {
		r = rootRealm
	}
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	st := c.sh.provides[provKey{realm: r, key: key}]
	if len(st) == 0 {
		var zero T
		return zero, false
	}
	t, ok := st[len(st)-1].value.(T)
	if !ok {
		// A provider is present but does not satisfy T. Treat as "not live for
		// this typed read" rather than resolving a wrong-typed value — the
		// router skips this realm exactly as it skips an absent one.
		var zero T
		return zero, false
	}
	return t, true
}

// LiveInRealm is the liveness half on its own: true iff realm r has an ACTIVE
// provider of key, strictly (no parent-chain fallback). ServiceInRealm already
// folds this in, but a caller that only needs the boolean (a selector probing
// which realms to consider before it dispatches) can avoid the type assertion.
func LiveInRealm(c *Context, key Key, r *Realm) bool {
	if r == nil {
		r = rootRealm
	}
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	return len(c.sh.provides[provKey{realm: r, key: key}]) > 0
}
