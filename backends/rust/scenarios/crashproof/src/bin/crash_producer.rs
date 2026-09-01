//! The rust half of the crash-recovery proof (roadmap item 322, Slice 2): the
//! rust analog of the py tier's tests/test_crash_recovery.py "simulated kill -9"
//! case, but driven by a REAL process that writes a durable WAL and dies.
//!
//! Built and driven by tests/test_rust_crash_recovery.py (the python half),
//! which sets the environment, runs this binary as a subprocess, then reads the
//! WAL back through `revl recover`. Run standalone with no REVL_WAL it self-skips
//! (exit 3, the runtime-unavailable convention — never a green run that recorded
//! nothing).
//!
//! Boots the witnessed composition (Crasher) whose activation registers a
//! witnessed transactional mutation. The recording preamble (emit --record)
//! writes that mutation's discharge-descriptor durably to $REVL_WAL and fsyncs
//! it (`File::sync_all`) BEFORE `wait()` returns, so the inverse is re-issuable
//! from the log alone after this process dies.
//!
//! Two modes, selected by env (set by the python driver):
//!
//!   - REVL_CRASH_BEFORE_COMMIT=1 — simulate an abrupt process crash:
//!     `process::exit(137)` the moment the mutation is durable, BEFORE commit.
//!     The WAL carries the descriptor but no `discharge` / `activation-complete`:
//!     recover rolls back.
//!   - unset — the clean control: dispose (commit -> the witnessed inverse
//!     discharges, never replays), then stamp `discharge` + `activation-complete`
//!     exactly as the py driver does: recover skips the committed seq / rolls
//!     forward.

use std::process::exit;

fn main() {
    if std::env::var("REVL_WAL")
        .ok()
        .filter(|v| !v.is_empty())
        .is_none()
    {
        eprintln!(
            "REVL_WAL not set: this producer runs under the python crash-recovery driver"
        );
        exit(3);
    }

    let root = cordis::Context::new();
    let cfg = serde_json::json!({ "Crasher": {} });
    let f = revl_crashproof::_revl_load(&root, "crasher", &cfg).expect("load Crasher");
    f.wait().expect("Crasher did not reach ACTIVE");
    // The witnessed mutation ran in-process and its discharge-descriptor is now
    // durable on disk (revl_record_transactional fsynced it before returning).

    if std::env::var("REVL_CRASH_BEFORE_COMMIT").ok().as_deref() == Some("1") {
        // Abrupt death mid-session: no discharge record, no terminal marker.
        // The fsynced descriptor is all that survives — exactly what recover
        // must roll back from.
        exit(137);
    }

    // Clean control: unload LIFO (commit flips the frame, so the witnessed
    // inverse discharges rather than replaying), then stamp the commit-path
    // proof and the terminal marker, mirroring the py driver's discharge +
    // commit_activation.
    f.dispose().expect("dispose Crasher");
    revl_crashproof::revl_record_discharge();
    revl_crashproof::revl_record_activation_complete();
}
