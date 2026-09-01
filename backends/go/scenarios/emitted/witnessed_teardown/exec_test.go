package witnessedteardown

import (
	stdctx "context"
	"strings"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func waitGone(f *stc.Fiber) {
	for i := 0; i < 200 && f.State() != stc.StateGone && f.State() != stc.StateFailed; i++ {
		time.Sleep(5 * time.Millisecond)
	}
}

// TestCommitPersistsWitnessedDischargesCompensation drives the COMMIT path
// directly (the emitted lifecycle test already covers this via `revl test`
// semantics; this direct drive additionally inspects the host trace order).
func TestCommitPersistsWitnessedDischargesCompensation(t *testing.T) {
	HostReset()
	root := stc.New()
	f := LoadC(root)
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("load C: %v", err)
	}
	marks := HostMarks()
	joined := strings.Join(marks, " | ")
	if !strings.Contains(joined, "lock") {
		t.Fatalf("bracket acquire did not run: %v", marks)
	}
	if !strings.Contains(joined, "stash") {
		t.Fatalf("witnessed acquire did not run: %v", marks)
	}
	if !strings.Contains(joined, "write insert") {
		t.Fatalf("emission did not run: %v", marks)
	}

	f.Dispose()
	waitGone(f)

	marks = HostMarks()
	joined = strings.Join(marks, " | ")
	// bracket ALWAYS reverts, clean unload or not.
	if !strings.Contains(joined, "unlock") {
		t.Fatalf("bracket did not revert on clean unload: %v", marks)
	}
	// witnessed DISCHARGES on a clean commit: the inverse must NOT replay.
	if strings.Contains(joined, "unstash") {
		t.Fatalf("witnessed inverse wrongly replayed on a clean commit (a5a "+
			"violation): %v", marks)
	}
	// compensation DISCHARGES on a clean commit: it must NEVER run.
	if strings.Contains(joined, "write delete") {
		t.Fatalf("compensation wrongly ran on a clean commit (item 247 a5a "+
			"violation): %v", marks)
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("residue: %d fiber(s) still registered", got)
	}
}

// TestAbortRevertsWitnessedThenFiresCompensation drives the ABORT path: a
// mid-body `fail` in CAbort. The go lifecycle-test DSL has no "expect this
// load to fail" assertion, so this is driven directly against LoadCAbort,
// asserting the two-phase contract (docs/design/teardown-contract.md):
//   - Phase 1 (bracket + witnessed, LIFO) replays IN FULL before Phase 2
//     (compensation) starts;
//   - the compensation actually fires on abort (the opposite of the commit
//     case above) — this is TCK A5's respec, the exact inversion of the old
//     single-phase assertion.
func TestAbortRevertsWitnessedThenFiresCompensation(t *testing.T) {
	HostReset()
	root := stc.New()
	f := LoadCAbort(root)
	waitGone(f)
	if f.State() != stc.StateFailed {
		t.Fatalf("CAbort should have landed FAILED (its `fail` step), got %s", f.State())
	}

	// Phase 2 runs on its own goroutine (runCompensationPhase is driven from
	// the LAST-registered ctx.Effect inverse, itself run inside stc-go's own
	// unwind goroutine) and the compensation itself is abandon-on-timeout —
	// give it a moment to land before asserting the trace is complete.
	var marks []string
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		marks = HostMarks()
		if strings.Contains(strings.Join(marks, " | "), "write delete") {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}

	idx := map[string]int{}
	for i, m := range marks {
		if _, ok := idx[m]; !ok {
			idx[m] = i
		}
	}
	unlockAt, hasUnlock := idx["unlock"]
	unstashAt, hasUnstash := idx["unstash artifact"]
	deleteAt, hasDelete := idx["write delete"]

	if !hasUnlock {
		t.Fatalf("bracket did not revert on abort: %v", marks)
	}
	if !hasUnstash {
		t.Fatalf("witnessed inverse did not replay on abort (A8 violation): %v", marks)
	}
	if !hasDelete {
		t.Fatalf("compensation did not fire on abort (item 247 violation): %v", marks)
	}
	// mixed-entry LIFO within Phase 1: stash was registered AFTER lock, so
	// its inverse (unstash) runs BEFORE lock's inverse (unlock).
	if !(unstashAt < unlockAt) {
		t.Fatalf("phase 1 LIFO violated: unstash at %d, unlock at %d (%v)",
			unstashAt, unlockAt, marks)
	}
	// the two-phase contract: Phase 1 (both bracket and witnessed) completes
	// in full before Phase 2 (compensation) starts.
	if !(unlockAt < deleteAt && unstashAt < deleteAt) {
		t.Fatalf("phase ordering violated: phase-1 entries must precede the "+
			"phase-2 compensation, got unstash=%d unlock=%d delete=%d (%v)",
			unstashAt, unlockAt, deleteAt, marks)
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("residue: %d fiber(s) still registered", got)
	}
}
