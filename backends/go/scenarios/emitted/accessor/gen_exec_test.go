package accessor

// Executes the EMITTED instance-accessor scenario (gen.go, produced by
// backends/go/emit.py from accessor.ir.json — itself compiled from
// ../../accessor.rvl) against the REAL stc-go runtime. White-box (same
// package) so it can read the emitted unexported service keys.
//
// The instance accessor `s.<key>` (docs/design-v2-instances.md, "Instance
// accessor — frozen") closes phase 1's missing positive direction: a spawner
// reading a provision back from its OWN instance, gated to the handle it alone
// holds. App spawns two Cell instances of the same template (ids 1 and 2),
// each providing `counter` in its own FRESH LOCAL realm, and reads each back
// through the handle it holds. This proves the DoD by RUNNING:
//
//  1. positive: `s.<key>.method(..)` returns THAT spawned instance's provision
//     — read_a resolves cell A's realm (id 1), read_b resolves cell B's (id 2);
//     a read that crossed handles or realms would return the wrong number, so
//     the two distinct ids pin per-handle resolution;
//  2. negative: the root — a stand-in for any sibling or outside party —
//     cannot resolve `counter`, so each provision stayed private to its
//     instance's local realm (supervision-tree addressing);
//  3. two instances of one template coexist without a duplicate-provide
//     collision, which is only possible because each `counter` lives in its
//     own local realm.

import (
	stdctx "context"
	"testing"

	stc "github.com/0xdenny218/stc-go"
)

func TestEmitted_InstanceAccessor_ReadsTheSpawnedInstancesOwnProvision(t *testing.T) {
	g := stdctx.Background()
	root := stc.New()
	defer root.Close()

	app := root.Load(App())
	if err := app.Ready(g); err != nil {
		t.Fatalf("App activation: %v", err)
	}

	// The accessor is exercised through the `reader` service the App provides
	// in the root realm; its methods read each spawned Cell's `counter` back
	// through the handle App alone holds.
	reader, err := stc.Service[Reader](root, _keyReader)
	if err != nil {
		t.Fatalf("resolve reader (provided by App in the root realm): %v", err)
	}

	// (1) positive: each handle reaches its OWN instance's provision, and no
	// other's — the distinct ids prove the reads did not cross instances.
	if got := reader.ReadA(); got != 1 {
		t.Fatalf("read_a() = %d, want 1 (cell A's own provision through its handle)", got)
	}
	if got := reader.ReadB(); got != 2 {
		t.Fatalf("read_b() = %d, want 2 (cell B's own provision through its handle)", got)
	}

	// (2/3) negative: neither instance's `counter` escaped to the root realm,
	// so the root (a sibling/outside party) cannot resolve it. Two providers of
	// the SAME key coexisting without a collision is only possible because each
	// lives in its own local realm.
	if _, err := stc.Service[Counter](root, _keyCounter); err == nil {
		t.Fatal("an instance's `counter` must stay private to its local realm; " +
			"the root (a sibling/outside party) resolved it")
	}

	if app.State() != stc.StateActive {
		t.Fatalf("App state = %v, want active", app.State())
	}
}
