package tenants

// Executes the EMITTED tenants components (gen.go, produced by
// backends/go/emit.py from tenants.ir.json) against the REAL stc-go runtime.
// Proves v2 realm placement end-to-end: two providers of the SAME key in
// disjoint realms coexist (per-(key,realm) disjointness, G2), and each app
// reactively links to — and writes into — its own realm's store, with no
// cross-realm leak.

import (
	stdctx "context"
	"testing"

	stc "github.com/0xdenny218/stc-go"
)

func TestEmitted_Tenants_RealmPlacement(t *testing.T) {
	g := stdctx.Background()
	root := stc.New()
	defer root.Close()

	// Both stores provide "kv" — isolation into distinct realms is what keeps
	// this from failing ErrDuplicateProvide (Def.58 well-formedness).
	as := LoadTenantAStore(root)
	bs := LoadTenantBStore(root)
	if err := as.Ready(g); err != nil {
		t.Fatalf("TenantAStore: %v", err)
	}
	if err := bs.Ready(g); err != nil {
		t.Fatalf("TenantBStore: %v", err)
	}

	// Each app requires "kv" isolated in its realm; it must reactively link to
	// its own realm's store and reach Active.
	aa := LoadTenantAApp(root)
	ba := LoadTenantBApp(root)
	if err := aa.Ready(g); err != nil {
		t.Fatalf("TenantAApp: %v", err)
	}
	if err := ba.Ready(g); err != nil {
		t.Fatalf("TenantBApp: %v", err)
	}

	// Read each realm's store through an isolated context and check the app
	// wrote into the correct one — with no cross-realm leak.
	readRealm := func(realm string) (string, bool) {
		ctx := root.Child()
		if err := ctx.Isolate(_keyKv, _revlRealm(realm)); err != nil {
			t.Fatal(err)
		}
		kv, err := stc.Service[Kv](ctx, _keyKv)
		if err != nil {
			t.Fatalf("resolve kv in %s: %v", realm, err)
		}
		return kv.Get("who")
	}

	if who, ok := readRealm("tenant_a"); !ok || who != "alice" {
		t.Fatalf("tenant_a who = %q (ok=%v), want alice", who, ok)
	}
	if who, ok := readRealm("tenant_b"); !ok || who != "bob" {
		t.Fatalf("tenant_b who = %q (ok=%v), want bob", who, ok)
	}
}
