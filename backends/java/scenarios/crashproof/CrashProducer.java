// The java half of the crash-recovery proof (roadmap item 322, Slice 2): the
// java analog of the py tier's tests/test_crash_recovery.py "simulated kill -9"
// case and the go tier's crash_proof_test.go, but driven by a REAL JVM process
// that writes a durable WAL and dies.
//
// This is a manual driver, not a JUnit test: it is built and driven by
// tests/test_java_crash_recovery.py (the python half), which sets the
// environment, runs it as a subprocess, then reads the WAL back through the
// tier-agnostic `revl recover`. Run standalone with no REVL_WAL it self-skips.
//
// It boots the witnessed composition (Crasher) on the in-repo cordis4j STUB
// Context — the same seam RunMethodWitnessedH1 uses — whose activation registers
// a witnessed transactional mutation. The recording preamble (emit --record)
// writes that mutation's discharge-descriptor durably to $REVL_WAL and fsyncs it
// (FileChannel.force) BEFORE apply() returns, so the inverse is re-issuable from
// the log alone after the process dies.

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;

public final class CrashProducer {
    // Two modes, selected by env (set by the python driver):
    //
    //   - REVL_CRASH_BEFORE_COMMIT=1 — simulate an abrupt process crash:
    //     Runtime.halt the moment the mutation is durable, BEFORE the discharge /
    //     activation-complete markers. The WAL carries the descriptor but no
    //     `discharge` / `activation-complete`: recover rolls back.
    //   - unset — the clean control: dispose (the in-process frame committed at
    //     activation-end, so the witnessed inverse discharges rather than
    //     replaying), then stamp `discharge` + `activation-complete` exactly as
    //     the py driver does: recover skips the committed seq / rolls forward.
    public static void main(String[] args) throws Exception {
        String wal = System.getenv("REVL_WAL");
        if (wal == null || wal.isEmpty()) {
            System.err.println("REVL_WAL not set: this producer runs under the "
                    + "python crash-recovery driver");
            System.exit(2);
        }

        Context root = new Context();
        Disposable activation = new revl.Components.CrasherPlugin().apply(root);
        // The witnessed mutation ran during apply() and its discharge-descriptor
        // is now durable on disk (revlRecordTransactional fsynced it before
        // apply() returned).

        if ("1".equals(System.getenv("REVL_CRASH_BEFORE_COMMIT"))) {
            // Abrupt death mid-session: no discharge record, no terminal marker.
            // halt() (unlike System.exit) skips shutdown hooks and finalizers —
            // the fsynced descriptor is all that survives, exactly what recover
            // must roll back from.
            Runtime.getRuntime().halt(137);
        }

        // Clean control: unload (dispose runs the in-process LIFO teardown; the
        // frame committed at activation-end so the witnessed inverse discharges
        // rather than replaying), then stamp the commit-path proof and the
        // terminal marker, mirroring the py driver's discharge + commit_activation
        // and go's revlRecordDischarge / revlRecordActivationComplete.
        activation.dispose();
        revl.Components.revlRecordDischarge();
        revl.Components.revlRecordActivationComplete();
    }

    private CrashProducer() {}
}
