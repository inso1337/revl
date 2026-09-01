package usercache

// Executes the EMITTED user_cache components (gen.go, produced by
// backends/go/emit.py from examples/user_cache.ir.json) against the REAL
// stc-go runtime. White-box (same package) so it can read the emitted
// unexported service keys.

import (
	stdctx "context"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

// Reactive provide/inject + effect/inverse on emitted code:
//   - UserCache (requires db) stays Pending until PgDatabase (provides db)
//     activates.
//   - the provided Cache resolves and its emission calls db.execute.
//   - withdrawing PgDatabase reactively deactivates UserCache (its Map effect
//     inverse, map.drop, runs) with no explicit dispose of UserCache.
func TestEmitted_UserCache_Reactive(t *testing.T) {
	HostReset()
	g := stdctx.Background()
	root := stc.New()
	defer root.Close()

	// UserCache first: db not provided yet, the Inject gate must hold it.
	uc := root.Load(UserCache())
	time.Sleep(40 * time.Millisecond)
	if uc.State() != stc.StatePending {
		t.Fatalf("UserCache state = %v, want pending (db not provided)", uc.State())
	}
	if m := HostMarks(); len(m) != 0 {
		t.Fatalf("no host effect should run before activation, got %v", m)
	}

	// Provide db by loading PgDatabase.
	cfg := DefaultPgDatabaseConfig()
	cfg.Url = "postgres://local"
	pg := LoadPgDatabase(root, cfg)
	if err := pg.Ready(g); err != nil {
		t.Fatalf("PgDatabase: %v", err)
	}
	if err := uc.Ready(g); err != nil {
		t.Fatalf("UserCache: %v", err)
	}

	if m := HostMarks(); !contains(m, "pool.open") || !contains(m, "map.new") {
		t.Fatalf("activation host effects = %v, want pool.open + map.new", m)
	}

	// The Cache provision resolves; its emission drives db.execute.
	cache, err := stc.Service[Cache](root, _keyCache)
	if err != nil {
		t.Fatalf("resolve cache: %v", err)
	}
	cache.Put("k1", "v1")
	if m := HostMarks(); !contains(m, "pool.execute:INSERT INTO cache_log VALUES (k1)") {
		t.Fatalf("put emission did not reach db.execute: %v", m)
	}

	// Withdraw the provider: UserCache must reactively deactivate (its Map
	// effect inverse, map.drop, runs) and settle back to Pending.
	//
	// The teardown marks (map.drop, pool.close) are recorded by inverses the
	// orchestrator runs DURING the Unloading phase; the UserCache fiber only
	// transitions Unloading -> Pending AFTER those inverses complete. Breaking
	// on the marks alone races that final transition and can observe UserCache
	// mid-flight in Unloading, so drive to reactive quiescence: require BOTH
	// teardown marks present AND UserCache actually settled to Pending. The
	// state is the deterministic quiescence signal, not a timed sleep.
	pg.Dispose()
	if err := pg.Gone(g); err != nil {
		t.Fatalf("PgDatabase dispose: %v", err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		m := HostMarks()
		teardownRan := contains(m, "map.drop") && contains(m, "pool.close")
		if teardownRan && uc.State() == stc.StatePending {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("reactive teardown did not reach quiescence: marks=%v state=%v", m, uc.State())
		}
		time.Sleep(10 * time.Millisecond)
	}
	if uc.State() != stc.StatePending {
		t.Fatalf("UserCache state = %v, want pending after db withdrawal", uc.State())
	}
}
