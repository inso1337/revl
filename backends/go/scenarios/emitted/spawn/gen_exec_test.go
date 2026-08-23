package spawn

// Executes the EMITTED instance-parametric spawn scenario (gen.go, produced by
// backends/go/emit.py from spawn.ir.json — itself compiled from
// ../../spawn.rvl) against the REAL stc-go runtime. White-box (same package)
// so it can read the emitted unexported service keys.
//
// The Supervisor's activation body spawns two Workers; each `spawn` isolates
// the Worker's `counter` provision into a FRESH LOCAL realm (a distinct
// *stc.Realm per spawn, no label) and plugs it as a CHILD FIBER of the
// supervisor. This proves the four DoD properties by RUNNING:
//
//  1. two live instances coexist in distinct local realms (non-colliding);
//  2. disposing one runs ITS LIFO teardown and leaves the others live;
//  3. a request-scoped instance is reclaimed at dispose(), NOT deferred to the
//     parent component's teardown (the anti-leak property, proven directly);
//  4. supervision-tree addressing: the spawner reaches its instance; a sibling
//     (here the root, standing in for any outside party) cannot.

import (
	stdctx "context"
	"reflect"
	"sort"
	"sync"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func bg() stdctx.Context { return stdctx.Background() }

// probeRec implements the emitted Probe interface as an ordered recorder.
type probeRec struct {
	mu  sync.Mutex
	log []string
}

func (r *probeRec) Mark(m string) int64 {
	r.mu.Lock()
	r.log = append(r.log, m)
	r.mu.Unlock()
	return 0
}

func (r *probeRec) marks() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, len(r.log))
	copy(out, r.log)
	return out
}

func (r *probeRec) reset() {
	r.mu.Lock()
	r.log = nil
	r.mu.Unlock()
}

// waitMarks polls until the recorded marks equal want, or fails after 2s.
// stc-go activates and disposes fibers asynchronously (single orchestrator
// goroutine, effects run off-lock), so per-instance marks land after the
// triggering call returns — exactly as the reference reactive test polls.
func waitMarks(t *testing.T, rec *probeRec, want []string, msg string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		got := rec.marks()
		if reflect.DeepEqual(got, want) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s: marks = %v, want %v", msg, got, want)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// waitMarksSet polls until the recorded marks, as a multiset, equal want. Used
// only for the two-instances-coexist phase: the two Worker fibers activate in
// separate goroutines off the orchestrator lock, so their marks interleave in
// a non-deterministic order (only within a single fiber is the order fixed).
func waitMarksSet(t *testing.T, rec *probeRec, want []string, msg string) {
	t.Helper()
	wantSorted := append([]string(nil), want...)
	sort.Strings(wantSorted)
	deadline := time.Now().Add(2 * time.Second)
	for {
		got := rec.marks()
		gotSorted := append([]string(nil), got...)
		sort.Strings(gotSorted)
		if reflect.DeepEqual(gotSorted, wantSorted) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s: marks (multiset) = %v, want %v", msg, got, want)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestEmitted_Spawn_InstancesCoexist_DisposeScopedAndLIFO(t *testing.T) {
	g := bg()
	root := stc.New()
	defer root.Close()

	rec := &probeRec{}
	if _, err := root.Provide(_keyProbe, Probe(rec)); err != nil {
		t.Fatalf("provide probe: %v", err)
	}

	sup := root.Load(Supervisor())
	if err := sup.Ready(g); err != nil {
		t.Fatalf("Supervisor activation: %v", err)
	}

	// (1) Two live instances, each carrying the config that flowed through its
	// spawn: worker A (tag "a") then worker B (tag "b") each activated and ran
	// both effects, in spawn order. Two providers of the SAME `counter` key
	// coexisting without a duplicate-provide collision is only possible because
	// each lives in its OWN fresh local realm (disjoint by construction).
	waitMarksSet(t, rec, []string{"up:a", "up2", "up:b", "up2"},
		"both instances must activate with their spawned configs")
	if sup.State() != stc.StateActive {
		t.Fatalf("supervisor state = %v, want active", sup.State())
	}

	// (1/4) Distinct local realms + supervision-tree addressing: neither
	// worker's `counter` provision escaped to the root realm, so the root — a
	// stand-in for any sibling or outside party — cannot resolve it. (The
	// spawner reaches its OWN instances through the handles it alone holds;
	// retire_a below exercises that reach.)
	if _, err := stc.Service[Counter](root, _keyCounter); err == nil {
		t.Fatal("an instance's `counter` must stay private to its local realm; " +
			"the root (a sibling/outside party) resolved it")
	}

	// (2/3/4) Retire worker A through the supervisor's `ctl` service — the
	// spawner reaching its OWN instance through the handle it alone holds.
	// Worker A's teardown runs NOW and in LIFO order (d2 before d1:a), not
	// deferred to the supervisor's teardown, and leaves the supervisor and
	// worker B live.
	ctl, err := stc.Service[Ctl](root, _keyCtl)
	if err != nil {
		t.Fatalf("resolve ctl (provided by the supervisor in the root realm): %v", err)
	}
	rec.reset()
	if got := ctl.RetireA(); got != 1 {
		t.Fatalf("retire_a() = %d, want 1", got)
	}
	waitMarks(t, rec, []string{"d2", "d1:a"},
		"worker A's own LIFO teardown must run at retire, not at supervisor teardown")
	if sup.State() != stc.StateActive {
		t.Fatalf("supervisor state = %v, want active after retiring A "+
			"(the sibling instance B stays live)", sup.State())
	}

	// (3) The remaining worker (B) is reclaimed ONLY when the supervisor tears
	// down — its `undo w.dispose()` inverse, the safety net that stops an
	// un-disposed instance leaking past its spawner — and only it: worker A is
	// already gone, so its teardown does not run a second time (idempotent
	// dispose). Exactly [d2, d1:b], no repeat of A's marks.
	rec.reset()
	sup.Dispose()
	if err := sup.Gone(g); err != nil {
		t.Fatalf("supervisor dispose: %v", err)
	}
	waitMarks(t, rec, []string{"d2", "d1:b"},
		"only worker B is reclaimed at supervisor teardown (A already gone, dispose idempotent)")
}
