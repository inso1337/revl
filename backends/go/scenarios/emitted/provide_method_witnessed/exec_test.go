package providemethodwitnessed

// The per-tool-call H1 proof (item 318, THE agent gate) — the go mirror of
// tests/test_provide_method_witnessed.py on the py reference tier. It drives
// the EMITTED Agent component (gen_provide_method_witnessed_test.go) against
// the real stc-go runtime and REAL files on disk:
//
//   * activation does nothing; the frame is empty until a tool call fires;
//   * each `ops.Touch(path)` runs the provide-method, registering ONE
//     transactional inverse into the component's activation frame (the
//     per-tool-call seam, RevlFrame.registerMethodWitnessed);
//   * a CLEAN unload commits — every per-call mutation PERSISTS, residue-free;
//   * an ABORT (RevlFrame.Abort() before unload) reverts EVERY per-call
//     mutation, residue-free, all-or-nothing across independent calls;
//   * the residue is ENUMERABLE: the frame's deferred entries account for every
//     crossing, and the abort surfaces no restore-residue.
//
// White-box (same package) so it can reach the sole activation Frame via
// RevlFrames() — the go seam that mirrors the py test's `_sole_frame(session)`.

import (
	stdctx "context"
	"os"
	"path/filepath"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func waitGone(f *stc.Fiber) {
	for i := 0; i < 400 && f.State() != stc.StateGone && f.State() != stc.StateFailed; i++ {
		time.Sleep(5 * time.Millisecond)
	}
}

// mutated: the witnessed rename ran — original gone, backup present.
func mutated(path string) bool {
	_, origErr := os.Stat(path)
	_, bakErr := os.Stat(path + ".bak")
	return os.IsNotExist(origErr) && bakErr == nil
}

// pristine: the world is as it started — original present, no backup residue.
func pristine(path string) bool {
	_, origErr := os.Stat(path)
	_, bakErr := os.Stat(path + ".bak")
	return origErr == nil && os.IsNotExist(bakErr)
}

// makeFiles creates three distinct target files, one per simulated tool call.
func makeFiles(t *testing.T) []string {
	t.Helper()
	dir := t.TempDir()
	paths := make([]string, 0, 3)
	for i := 0; i < 3; i++ {
		p := filepath.Join(dir, "artifact_"+string(rune('0'+i))+".txt")
		if err := os.WriteFile(p, []byte("deliverable"), 0o644); err != nil {
			t.Fatalf("seed %s: %v", p, err)
		}
		paths = append(paths, p)
	}
	return paths
}

func loadAgentWithOps(t *testing.T) (*stc.Context, *stc.Fiber, Ops) {
	t.Helper()
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

func soleFrame(t *testing.T) *RevlFrame {
	t.Helper()
	frames := RevlFrames()
	if len(frames) != 1 {
		t.Fatalf("want exactly one activation frame, got %d", len(frames))
	}
	return frames[0]
}

// 1. per-tool-call witnessed mutation PERSISTS on a clean unload (commit).
func TestPerToolCallMutationsPersistOnCleanUnload(t *testing.T) {
	files := makeFiles(t)
	root, f, ops := loadAgentWithOps(t)

	// activation did nothing; the frame is empty until a tool call fires.
	frame := soleFrame(t)
	if len(frame.deferred) != 0 {
		t.Fatalf("frame not empty before any tool call: %d parked", len(frame.deferred))
	}

	// each tool call runs the provide-method, registering ONE inverse.
	for _, p := range files {
		ops.Touch(p)
		if !mutated(p) {
			t.Fatalf("the witnessed mutation did not apply on the call: %s", p)
		}
	}
	if len(frame.deferred) != len(files) {
		t.Fatalf("want %d parked inverses, got %d", len(files), len(frame.deferred))
	}

	f.Dispose() // clean unload == implicit commit
	waitGone(f)

	// the deliverable persists on every path; nothing reverted.
	for _, p := range files {
		if !mutated(p) {
			t.Fatalf("clean unload wrongly reverted a per-call mutation: %s", p)
		}
	}
	// discharged: the parked inverses are disposed and dropped (witness GC).
	if len(frame.deferred) != 0 {
		t.Fatalf("commit left %d parked inverses undisposed", len(frame.deferred))
	}
	if got := frame.Residue(); len(got) != 0 {
		t.Fatalf("clean commit surfaced residue: %v", got)
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("residue: %d fiber(s) still registered", got)
	}
}

// 2. per-tool-call witnessed mutation REVERTS on abort, residue-free.
func TestPerToolCallMutationsRevertOnAbort(t *testing.T) {
	files := makeFiles(t)
	root, f, ops := loadAgentWithOps(t)
	frame := soleFrame(t)

	for _, p := range files {
		ops.Touch(p)
		if !mutated(p) {
			t.Fatalf("mutation did not apply: %s", p)
		}
	}

	// abort the session's work (item 245's reject drives this seam): the next
	// teardown reverts instead of committing.
	frame.Abort()
	f.Dispose()
	waitGone(f)

	// every per-call mutation reverted, and the teardown left no residue.
	for _, p := range files {
		if !pristine(p) {
			t.Fatalf("abort did not revert a per-call mutation: %s", p)
		}
	}
	if got := frame.Residue(); len(got) != 0 {
		t.Fatalf("abort left teardown residue: %v", got)
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("residue: %d fiber(s) still registered", got)
	}
}

// 3. abort is all-or-nothing across independent per-call mutations.
func TestAbortRevertsEveryCallNotJustTheLast(t *testing.T) {
	files := makeFiles(t)
	_, f, ops := loadAgentWithOps(t)
	frame := soleFrame(t)
	for _, p := range files {
		ops.Touch(p)
	}

	frame.Abort()
	f.Dispose()
	waitGone(f)

	// all three, in one abort — the activation frame is the shared accumulator.
	for _, p := range files {
		if !pristine(p) {
			t.Fatalf("abort reverted only some calls; still mutated: %s", p)
		}
	}
}

// 4. the residue is ENUMERABLE: the frame's deferred entries account for every
//    per-call crossing before the fate is decided; a commit disposes them all,
//    an abort reverts them all — neither leaves an un-accounted crossing.
func TestFrameEnumeratesEveryPerCallCrossing(t *testing.T) {
	files := makeFiles(t)
	_, f, ops := loadAgentWithOps(t)
	frame := soleFrame(t)

	for i, p := range files {
		ops.Touch(p)
		// each crossing is enumerated the instant it registers, well before
		// commit — one parked inverse per tool call.
		if len(frame.deferred) != i+1 {
			t.Fatalf("after %d calls want %d parked crossings, got %d",
				i+1, i+1, len(frame.deferred))
		}
	}

	f.Dispose() // clean commit
	waitGone(f)

	// the commit disposed (discharged) every enumerated crossing.
	if len(frame.deferred) != 0 {
		t.Fatalf("commit left %d crossings un-discharged", len(frame.deferred))
	}
	if got := frame.Residue(); len(got) != 0 {
		t.Fatalf("commit surfaced residue over discharged crossings: %v", got)
	}
}
