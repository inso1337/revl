package stc

import (
	stdctx "context"
	"testing"
)

// item 173: ServiceInRealm resolves STRICTLY in one realm and does NOT fall
// back up the realm chain — the property the router's emitted body needs so a
// withdrawn worker realm drops out of the live set instead of resolving to the
// router's own parent-realm provision.
func TestServiceInRealmStrictNoFallback(t *testing.T) {
	root := New()
	defer root.Close()
	k := NewKey[string]("worker")

	// The router provides the bare key in the ROOT realm (well-formedness: one
	// provider downstream). A worker provides it in an isolated named realm.
	outer, err := root.Provide(k, "router")
	if err != nil {
		t.Fatal(err)
	}
	_ = outer

	w1 := NewRealm(RootRealm(), "w1")
	zone := root.Child()
	if err := zone.Isolate(k, w1); err != nil {
		t.Fatal(err)
	}
	inW1, err := zone.Provide(k, "w1-worker")
	if err != nil {
		t.Fatal(err)
	}

	// Strict read finds exactly the w1 provider.
	if v, ok := ServiceInRealm[string](root, k, w1); !ok || v != "w1-worker" {
		t.Fatalf("ServiceInRealm(w1) = (%q, %v), want (w1-worker, true)", v, ok)
	}
	if !LiveInRealm(root, k, w1) {
		t.Fatal("LiveInRealm(w1) = false, want true")
	}

	// A realm that never provided the key is absent — NOT a parent fallback.
	w2 := NewRealm(RootRealm(), "w2")
	if v, ok := ServiceInRealm[string](root, k, w2); ok {
		t.Fatalf("ServiceInRealm(w2 empty) = (%q, true), want (_, false) — no fallback", v)
	}
	if LiveInRealm(root, k, w2) {
		t.Fatal("LiveInRealm(w2 empty) = true, want false")
	}

	// Withdraw the w1 worker. The KEY POINT: the strict read must now report
	// absent, NOT fall back to the root "router" provision (which plain
	// resolve() WOULD return — see TestIsolateRealm).
	if err := inW1(); err != nil {
		t.Fatal(err)
	}
	if v, ok := ServiceInRealm[string](root, k, w1); ok {
		t.Fatalf("ServiceInRealm(w1 withdrawn) = (%q, true), want (_, false) — must not fall back to root", v)
	}
	if LiveInRealm(root, k, w1) {
		t.Fatal("LiveInRealm(w1 withdrawn) = true, want false")
	}
	// Sanity: plain resolve DOES fall back (the behavior the router must avoid).
	if v, _ := zone.resolve(k); v != "router" {
		t.Fatalf("resolve after withdrawal = %v, want router (parent fallback) — baseline", v)
	}
}

// A whole three-realm rotation across a live pool, then failover, exercised the
// way the emitted router body drives the primitive.
func TestServiceInRealmPoolFailover(t *testing.T) {
	root := New()
	defer root.Close()
	k := NewKey[string]("worker")

	realms := []*Realm{
		NewRealm(RootRealm(), "w1"),
		NewRealm(RootRealm(), "w2"),
		NewRealm(RootRealm(), "w3"),
	}
	values := []string{"w1", "w2", "w3"}
	inverses := make([]Inverse, len(realms))
	g := stdctx.Background()
	for i, r := range realms {
		zone := root.Child()
		if err := zone.Isolate(k, r); err != nil {
			t.Fatal(err)
		}
		inv, err := zone.Provide(k, values[i])
		if err != nil {
			t.Fatal(err)
		}
		inverses[i] = inv
	}
	_ = g

	live := func() []string {
		var out []string
		for i, r := range realms {
			if v, ok := ServiceInRealm[string](root, k, r); ok {
				if v != values[i] {
					t.Fatalf("realm %d resolved %q, want %q", i, v, values[i])
				}
				out = append(out, v)
			}
		}
		return out
	}

	if got := live(); len(got) != 3 {
		t.Fatalf("live set = %v, want all three", got)
	}
	// Withdraw the middle worker: it drops out, the survivors remain.
	if err := inverses[1](); err != nil {
		t.Fatal(err)
	}
	got := live()
	if len(got) != 2 || got[0] != "w1" || got[1] != "w3" {
		t.Fatalf("live set after w2 withdrawal = %v, want [w1 w3]", got)
	}
}
