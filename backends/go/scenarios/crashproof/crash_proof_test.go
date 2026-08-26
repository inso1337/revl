package crashrecovery

// The go half of the crash-recovery proof (roadmap item 322, Slice 1): the go
// analog of the py tier's tests/test_crash_recovery.py "simulated kill -9"
// case, but driven by a REAL process that writes a durable WAL and dies.
//
// This is a manual test binary, not part of the scenario's own assertions: it
// is built and driven by tests/test_go_crash_recovery.py (the python half),
// which sets the environment, runs it as a subprocess, then reads the WAL back
// through `revl recover`. Run standalone with no REVL_WAL it self-skips.

import (
	stdctx "context"
	"os"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

// TestGoCrashRecoveryProducer boots the witnessed composition (Crasher) whose
// activation registers a witnessed transactional mutation. The recording
// preamble (emit --record) writes that mutation's discharge-descriptor durably
// to $REVL_WAL and fsyncs it BEFORE this function proceeds, so the inverse is
// re-issuable from the log alone after the process dies.
//
// Two modes, selected by env (set by the python driver):
//
//   - REVL_CRASH_BEFORE_COMMIT=1 — simulate an abrupt process crash: os.Exit
//     the moment the mutation is durable, BEFORE commit. The WAL carries the
//     descriptor but no `discharge` / `activation-complete`: recover rolls back.
//   - unset — the clean control: dispose (commit -> the witnessed inverse
//     discharges, never replays), then stamp `discharge` + `activation-complete`
//     exactly as the py driver does: recover skips the committed seq / rolls
//     forward.
func TestGoCrashRecoveryProducer(t *testing.T) {
	if os.Getenv("REVL_WAL") == "" {
		t.Skip("REVL_WAL not set: this producer runs under the python crash-recovery driver")
	}
	root := stc.New()
	f := LoadCrasher(root)
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("boot Crasher: %v", err)
	}
	// The witnessed mutation ran in-process and its discharge-descriptor is now
	// durable on disk (revlRecordTransactional fsynced it before returning).

	if os.Getenv("REVL_CRASH_BEFORE_COMMIT") == "1" {
		// Abrupt death mid-session: no discharge record, no terminal marker.
		// The fsynced descriptor is all that survives — exactly what recover
		// must roll back from.
		os.Exit(137)
	}

	// Clean control: unload LIFO (commit flips the frame, so the witnessed
	// inverse discharges rather than replaying), then stamp the commit-path
	// proof and the terminal marker, mirroring the py driver's discharge +
	// commit_activation.
	f.Dispose()
	goneCtx, cancel := stdctx.WithTimeout(stdctx.Background(), 5*time.Second)
	defer cancel()
	_ = f.Gone(goneCtx)
	revlRecordDischarge()
	revlRecordActivationComplete()
}
