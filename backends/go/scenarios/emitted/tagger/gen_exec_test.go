package tagger

// Executes the EMITTED Tagger component (gen.go, produced by backends/go/emit.py
// from tagger.ir.json) against the REAL stc-go runtime — the executable proof
// for roadmap item 113 with a COMPOUND host Map value (`Map[Str, List[Str]]`).
//
// The store's declared value type is `List[Str]`, so emit instantiates the
// generic host Map as `Map[[]string]` and Insert/Get carry whole slices. Before
// item 113 the host Map held String values only, so `store.insert(key, tags)`
// failed `go build`. White-box (same package) so it can read the emitted
// unexported provision key.

import (
	stdctx "context"
	"testing"

	stc "github.com/0xdenny218/stc-go"
)

// TestEmitted_Tagger_HostMapListValues proves a List[Str] round-trips through
// the host Map unchanged: the slice inserted under a key comes back element for
// element, and a missing key returns the caller's fallback slice.
func TestEmitted_Tagger_HostMapListValues(t *testing.T) {
	HostReset()
	g := stdctx.Background()
	root := stc.New()
	defer root.Close()

	fiber := LoadTagger(root)
	if err := fiber.Ready(g); err != nil {
		t.Fatalf("Tagger: %v", err)
	}

	groups, err := stc.Service[Groups](root, _keyG)
	if err != nil {
		t.Fatalf("resolve g: %v", err)
	}

	eq := func(a, b []string) bool {
		if len(a) != len(b) {
			return false
		}
		for i := range a {
			if a[i] != b[i] {
				return false
			}
		}
		return true
	}

	// A missing key returns the caller's fallback (the `?? fallback` path).
	if got := groups.GetOr("missing", []string{"none"}); !eq(got, []string{"none"}) {
		t.Fatalf("GetOr(missing) = %v, want [none]", got)
	}

	// Insert a whole slice under a key and read it back element for element.
	groups.Set("colors", []string{"red", "green", "blue"})
	got := groups.GetOr("colors", nil)
	want := []string{"red", "green", "blue"}
	if !eq(got, want) {
		t.Fatalf("GetOr(colors) = %v, want %v", got, want)
	}

	// A second Set overwrites the list value wholesale.
	groups.Set("colors", []string{"amber"})
	if got := groups.GetOr("colors", nil); !eq(got, []string{"amber"}) {
		t.Fatalf("GetOr(colors) after overwrite = %v, want [amber]", got)
	}
}
