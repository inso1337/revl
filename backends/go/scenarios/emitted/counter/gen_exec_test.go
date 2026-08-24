package counter

// Executes the EMITTED Counter component (gen.go, produced by backends/go/emit.py
// from counter.ir.json) against the REAL stc-go runtime — the executable proof
// for roadmap item 113 (the host `Map.new()` value type on the go tier).
//
// The counter stores its per-key tallies in a host `Map.new()` whose declared
// value type is `Int`, so emit instantiates the generic host Map as `Map[int]`
// and Insert/Get carry Int values. Before item 113 the host Map held String
// values only, so `store.insert(key, amount)` failed `go build`
// (`cannot use amount (int) as string value`). White-box (same package) so it
// can read the emitted unexported provision key.

import (
	stdctx "context"
	"testing"

	stc "github.com/0xdenny218/stc-go"
)

// TestEmitted_Counter_HostMapIntValues drives the Int-valued host Map end to
// end: Int values inserted under string keys come back as the same Int (no
// String round-trip), a missing key yields the `?? 0` fallback, and a second
// insert on a key overwrites its value.
func TestEmitted_Counter_HostMapIntValues(t *testing.T) {
	HostReset()
	g := stdctx.Background()
	root := stc.New()
	defer root.Close()

	fiber := LoadCounter(root)
	if err := fiber.Ready(g); err != nil {
		t.Fatalf("Counter: %v", err)
	}

	tally, err := stc.Service[Tally](root, _keyTally)
	if err != nil {
		t.Fatalf("resolve tally: %v", err)
	}

	// Empty map: total is 0, a missing key reads the `?? 0` fallback.
	if n := tally.Total(); n != 0 {
		t.Fatalf("empty Total() = %d, want 0", n)
	}
	if v := tally.Get("missing"); v != 0 {
		t.Fatalf("Get(missing) = %d, want 0 (the ?? 0 fallback)", v)
	}

	// Insert Int values under string keys — this is the round-trip that a
	// String-only host Map could not compile, let alone run.
	tally.Bump("apples", 5)
	tally.Bump("bananas", 7)

	if n := tally.Total(); n != 2 {
		t.Fatalf("Total() = %d, want 2", n)
	}
	if v := tally.Get("apples"); v != 5 {
		t.Fatalf("Get(apples) = %d, want 5", v)
	}
	if v := tally.Get("bananas"); v != 7 {
		t.Fatalf("Get(bananas) = %d, want 7", v)
	}

	// A second insert on a key overwrites its Int value.
	tally.Bump("apples", 9)
	if v := tally.Get("apples"); v != 9 {
		t.Fatalf("Get(apples) after re-bump = %d, want 9", v)
	}
}
