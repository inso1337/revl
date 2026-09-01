package methodcompensate

// The method-body-compensation proof on the go tier (the item-247 method-body
// compensate remainder) — the go mirror of
// tests/test_provide_method_compensate.py and
// backends/typescript/tests/method_compensate.test.ts. It drives the EMITTED
// Agent component (gen_method_compensate_test.go) against the real stc-go
// runtime:
//
//   * activation does nothing; a tool call `ops.Run(p, msg)` fires a witnessed
//     `stash` (a transactional proof inverse) AND an `emit note compensate
//     offset` (a compensation registered onto the activation frame via
//     RevlFrame.registerMethodCompensation);
//   * a CLEAN unload commits — the offset DISCHARGES (never runs; the emission
//     was the deliverable), so the host trace has NO "compensate go";
//   * an ABORT (RevlFrame.Abort() before unload) FIRES the offset in PHASE 2,
//     strictly AFTER the transactional proof inverse ("unstash" precedes
//     "compensate go" in the trace), residue-free.
//
// White-box (same package) so it reaches the sole activation Frame via
// RevlFrames(), mirroring the py test's _sole_frame(session).

import (
	stdctx "context"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func waitGone(f *stc.Fiber) {
	for i := 0; i < 400 && f.State() != stc.StateGone && f.State() != stc.StateFailed; i++ {
		time.Sleep(5 * time.Millisecond)
	}
}

func loadAgentMC(t *testing.T) (*stc.Context, *stc.Fiber, Ops) {
	t.Helper()
	HostReset()
	RevlResetFrames()
	root := stc.New()
	f := LoadAgent(root)
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("load Agent: %v", err)
	}
	ops, err := stc.Service[Ops](root, _keyOps)
	if err != nil {
		t.Fatalf("resolve ops: %v", err)
	}
	return root, f, ops
}

func soleFrameMC(t *testing.T) *RevlFrame {
	t.Helper()
	frames := RevlFrames()
	if len(frames) != 1 {
		t.Fatalf("want exactly one activation frame, got %d", len(frames))
	}
	return frames[0]
}

func contains(marks []string, want string) bool {
	for _, m := range marks {
		if m == want {
			return true
		}
	}
	return false
}

// 1. clean unload (commit): the compensation DISCHARGES — the offset never runs.
func TestMethodCompensationDischargesOnCleanUnload(t *testing.T) {
	root, f, ops := loadAgentMC(t)

	ops.Run("/artifact.txt", "go")

	// two entries parked on the frame: the witnessed inverse and the
	// compensation closure (both live in `deferred`, disposed by commit()).
	frame := soleFrameMC(t)
	if len(frame.deferred) != 2 {
		t.Fatalf("want 2 parked entries after the call, got %d", len(frame.deferred))
	}

	f.Dispose() // clean unload == implicit commit
	waitGone(f)

	marks := HostMarks()
	if contains(marks, "compensate go") {
		t.Fatalf("clean commit wrongly fired the offset: %v", marks)
	}
	if contains(marks, "unstash") {
		t.Fatalf("clean commit wrongly reverted the witnessed mutation: %v", marks)
	}
	if got := frame.Residue(); len(got) != 0 {
		t.Fatalf("clean commit surfaced residue: %v", got)
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("residue: %d fiber(s) still registered", got)
	}
}

// 2. abort: the compensation FIRES in PHASE 2, strictly after the proof inverse.
func TestMethodCompensationFiresInPhase2OnAbort(t *testing.T) {
	root, f, ops := loadAgentMC(t)
	frame := soleFrameMC(t)

	ops.Run("/artifact.txt", "go")

	frame.Abort() // item 245's reject seam
	f.Dispose()
	waitGone(f)

	// the phase-order proof: the transactional inverse's "unstash" precedes the
	// compensation's "compensate go" — Phase 1 completed before Phase 2 started.
	marks := HostMarks()
	unstashAt, compAt := -1, -1
	for i, m := range marks {
		if m == "unstash" {
			unstashAt = i
		}
		if m == "compensate go" {
			compAt = i
		}
	}
	if unstashAt < 0 {
		t.Fatalf("abort did not replay the witnessed inverse: %v", marks)
	}
	if compAt < 0 {
		t.Fatalf("abort did not fire the compensation: %v", marks)
	}
	if unstashAt >= compAt {
		t.Fatalf("Phase 2 ran before Phase 1 (compensation before proof inverse): %v", marks)
	}
	if got := frame.Residue(); len(got) != 0 {
		t.Fatalf("abort surfaced residue: %v", got)
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("residue: %d fiber(s) still registered", got)
	}
}
