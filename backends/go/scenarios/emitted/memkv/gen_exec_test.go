package memkv

// Executes the EMITTED MemKV component (gen.go, produced by backends/go/emit.py
// from memkv.ir.json) against the REAL stc-go runtime — the executable proof for
// roadmap item 88 (host `Map.new()` iteration surface on the go tier).
//
// The value-Map builtins `size()`/`keys()` (docs/stdlib-2.0.md §Map) type-check
// on a host `Map.new()` receiver too, and emit lowers both as plain method calls
// on the runtime object (`s.store.Size()`, `s.store.Keys()`). Before the host
// `type Map struct` backed Size/Keys, this component would not compile
// (`s.store.Size undefined`). White-box (same package) so it can read the
// emitted unexported service key.

import (
	stdctx "context"
	"testing"

	stc "github.com/0xdenny218/stc-go"
)

// TestEmitted_MemKV_HostMapIteration drives the host Map's keys()/size() surface
// end to end: keys inserted out of order come back sorted in canonical (UTF-8
// byte, i.e. code-point) order, size counts the live entries, and both are
// read-only (no host trace record like insert/remove/new/drop leave).
func TestEmitted_MemKV_HostMapIteration(t *testing.T) {
	HostReset()
	g := stdctx.Background()
	root := stc.New()
	defer root.Close()

	fiber := LoadMemKV(root)
	if err := fiber.Ready(g); err != nil {
		t.Fatalf("MemKV: %v", err)
	}

	kv, err := stc.Service[KV](root, _keyKv)
	if err != nil {
		t.Fatalf("resolve kv: %v", err)
	}

	// Empty map: size is 0, keys is empty.
	if n := kv.Count(); n != 0 {
		t.Fatalf("empty Count() = %d, want 0", n)
	}
	if ks := kv.AllKeys(); len(ks) != 0 {
		t.Fatalf("empty AllKeys() = %v, want []", ks)
	}

	// Insert out of order (and past U+FFFF) to exercise canonical ordering.
	kv.Put("banana", "yellow")
	kv.Put("apple", "red")
	kv.Put("cherry", "dark")
	kv.Put("Z", "upper")   // ASCII upper 'Z' (0x5A) sorts before lowercase
	kv.Put("𐐷", "deseret") // U+10437: a 4-byte rune, must sort after BMP keys

	if n := kv.Count(); n != 5 {
		t.Fatalf("Count() = %d, want 5", n)
	}

	got := kv.AllKeys()
	want := []string{"Z", "apple", "banana", "cherry", "𐐷"}
	if len(got) != len(want) {
		t.Fatalf("AllKeys() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("AllKeys()[%d] = %q, want %q (full: %v)", i, got[i], want[i], got)
		}
	}

	// keys()/size() are read-only queries — they must not append a host op the
	// way new/insert/remove/drop do. Only the 5 inserts (map.new is unrecorded
	// at insert time; the acquisition records map.new once) should be present,
	// and no "map.keys"/"map.size" markers exist at all.
	for _, m := range HostMarks() {
		if m == "map.keys" || m == "map.size" {
			t.Fatalf("keys()/size() left a host trace: %v", HostMarks())
		}
	}
}
