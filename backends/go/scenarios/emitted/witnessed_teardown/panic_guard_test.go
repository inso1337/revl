package witnessedteardown

// Direct unit tests of the RevlFrame teardown preamble emitted by
// backends/go/emit.py's `_TEARDOWN_PREAMBLE` (docs/design/teardown-contract.md,
// go per-tier obligations). These exercise the goroutine-abandon /
// panic-guard / concurrency machinery in isolation from the full emitted
// component pipeline (gen_witnessed_teardown_test.go / exec_test.go prove the
// end-to-end persist-on-commit / revert-on-abort / discharge behavior; this
// file proves the specific GO per-tier rules the task called out: a panicking
// compensation must not crash the process, and abandoned compensations may
// run concurrently).

import (
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestPanicGuardDoesNotCrashTheProcess is the load-bearing proof: a
// compensation whose forward call panics must be CONTAINED and turned into a
// residue record, not an unrecovered panic in the detached goroutine (which
// would kill the whole process — go's per-tier obligation, docs/design/
// teardown-contract.md, "The guarded goroutine MUST `defer recover()`").
// The very fact this test function returns normally (instead of the whole
// `go test` binary crashing with a panic stack trace and a non-zero exit
// from an unrecovered goroutine panic) IS the proof.
func TestPanicGuardDoesNotCrashTheProcess(t *testing.T) {
	os.Unsetenv("REVL_COMPENSATION_BUDGET_MS")
	os.Unsetenv("REVL_COMPENSATION_PER_CALL_MS")

	f := newRevlFrame()
	f.enqueue("mail", "recall", func() error {
		panic("boom: the compensation itself panics")
	})

	done := make(chan struct{})
	go func() {
		f.runCompensationPhase()
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("runCompensationPhase hung — the panic escaped the guard")
	}

	residue := f.Residue()
	if len(residue) != 1 {
		t.Fatalf("expected exactly one residue record, got %d: %+v", len(residue), residue)
	}
	rec := residue[0]
	if rec.Kind != "compensation-residue" {
		t.Fatalf("expected kind compensation-residue, got %q", rec.Kind)
	}
	if rec.Outcome != "failed" {
		t.Fatalf("expected outcome failed (the panic was caught synchronously, "+
			"not abandoned), got %q", rec.Outcome)
	}
	if rec.CrossingKey != "mail" || rec.CrossingMethod != "recall" {
		t.Fatalf("residue crossing mismatch: %+v", rec)
	}
}

// TestCommittedFrameNeverRunsQueuedCompensations is item 247's a5a: a frame
// that committed must discharge every queued compensation — never run it,
// regardless of what is queued (a panicking one included, so a committed
// frame with a "landmine" compensation queued is still perfectly safe).
func TestCommittedFrameNeverRunsQueuedCompensations(t *testing.T) {
	f := newRevlFrame()
	ran := false
	f.enqueue("mail", "recall", func() error {
		ran = true
		panic("must never run")
	})
	f.commit()
	f.runCompensationPhase()
	if ran {
		t.Fatal("a committed frame ran a queued compensation (a5a violation)")
	}
	if got := len(f.Residue()); got != 0 {
		t.Fatalf("a committed frame's discharge produced residue: %d records", got)
	}
}

// TestAbandonedCompensationsRunConcurrently proves the go-specific relaxation
// the contract calls out: "after the loop abandons one compensation and
// starts the next, the abandoned call is still running, so two compensations
// may run CONCURRENTLY." Two entries each sleep past the per-call bound (so
// both are abandoned by runOneCompensation's select) and both increment a
// shared atomic counter from their still-running goroutine; if the runtime
// wrongly serialized them (waiting for the first to truly finish before
// abandoning and starting the second), this test would still pass but take
// ~2x as long — the tight overall deadline below is what actually catches
// serialization.
func TestAbandonedCompensationsRunConcurrently(t *testing.T) {
	t.Setenv("REVL_COMPENSATION_BUDGET_MS", "5000")
	t.Setenv("REVL_COMPENSATION_PER_CALL_MS", "20")

	f := newRevlFrame()
	var completed int32
	var wg sync.WaitGroup
	wg.Add(2)
	for i := 0; i < 2; i++ {
		f.enqueue("svc", fmt.Sprintf("op%d", i), func() error {
			defer wg.Done()
			time.Sleep(150 * time.Millisecond) // > the 20ms per-call bound
			atomic.AddInt32(&completed, 1)
			return nil
		})
	}

	start := time.Now()
	f.runCompensationPhase()
	elapsed := time.Since(start)

	// Both abandoned (per-call bound is 20ms, the work takes 150ms): the
	// phase itself must return promptly (start-order pinned, not
	// mutual-exclusion — draining the loop does not wait for either call to
	// actually finish). A serialized implementation would take >= 300ms here
	// (20ms wait + 150ms full run, twice); a concurrent one returns in well
	// under 150ms total for BOTH abandon decisions.
	if elapsed >= 150*time.Millisecond {
		t.Fatalf("runCompensationPhase took %v — looks serialized, not "+
			"concurrent (two abandoned compensations must overlap)", elapsed)
	}

	residue := f.Residue()
	if len(residue) != 2 {
		t.Fatalf("expected 2 abandoned records, got %d: %+v", len(residue), residue)
	}
	for _, rec := range residue {
		if rec.Outcome != "unknown" {
			t.Fatalf("an abandoned in-flight call must record outcome:unknown, got %q", rec.Outcome)
		}
	}

	// Now prove the two detached goroutines really did run concurrently
	// (not one, then the other, after the phase already returned): wait for
	// both to finish and confirm neither goroutine leaked/deadlocked, and
	// that the total wall time for BOTH to complete is close to 150ms (one
	// sleep), not ~300ms (two sequential sleeps).
	waitStart := time.Now()
	wg.Wait()
	waitElapsed := time.Since(waitStart) + elapsed
	if waitElapsed >= 280*time.Millisecond {
		t.Fatalf("the two abandoned compensations did not overlap: both "+
			"finished after %v (expected ~150ms if concurrent, ~300ms if serial)",
			waitElapsed)
	}
	if atomic.LoadInt32(&completed) != 2 {
		t.Fatalf("expected both abandoned compensations to eventually complete, got %d", completed)
	}
}

// TestPhaseTwoDeadlineSkipsRemainingEntries: once the phase-2 budget has
// expired, every remaining queued compensation is recorded skipped
// (`deadline-expired`, `attempted: false`) — not silently dropped, and none
// of them run.
func TestPhaseTwoDeadlineSkipsRemainingEntries(t *testing.T) {
	t.Setenv("REVL_COMPENSATION_BUDGET_MS", "30")
	t.Setenv("REVL_COMPENSATION_PER_CALL_MS", "0") // no per-call bound

	f := newRevlFrame()
	var ran int32
	f.enqueue("svc", "first", func() error {
		time.Sleep(50 * time.Millisecond) // burns the whole 30ms budget
		atomic.AddInt32(&ran, 1)
		return nil
	})
	f.enqueue("svc", "second", func() error {
		atomic.AddInt32(&ran, 1)
		return nil
	})
	f.runCompensationPhase()

	residue := f.Residue()
	var sawSkip bool
	for _, rec := range residue {
		if rec.CrossingMethod == "second" {
			sawSkip = true
			if rec.ErrorType != "deadline-expired" {
				t.Fatalf("expected deadline-expired, got %q", rec.ErrorType)
			}
			if rec.Outcome != "not-attempted" {
				t.Fatalf("expected not-attempted, got %q", rec.Outcome)
			}
		}
	}
	if !sawSkip {
		t.Fatalf("the second (never-started) compensation was not recorded skipped: %+v", residue)
	}
}
