//! Timer exit test (item 57, docs/time-coeffect.md): the EMITTED Heartbeat
//! component driven by the REAL cordis-rs runtime. Proves the two properties
//! 57 pins on py/ts:
//!   - deterministic firing: time passes only on `revl_clock_advance`, and a
//!     firing is a reproducible timeline step (the `every` fires on each 30s
//!     boundary; the `after` fires once at 300000ms and does not re-arm);
//!   - unload cancels, residue-free: disposing the component runs the timers'
//!     derived inverses (cancel), so no interval outlives the activation —
//!     `revl_clock_pending()` drops to 0 and a further advance fires nothing.
//! The firing body's emission is observed reaching a recording Log, so the
//! scheduled reach is real, not just a clock tick.

use revl_timer_scn::{
    heartbeat, revl_clock_advance, revl_clock_firings, revl_clock_pending,
    revl_clock_reset, Log,
};
use std::sync::{Arc, Mutex};

struct RecordLog {
    seen: Arc<Mutex<Vec<String>>>,
}

impl Log for RecordLog {
    fn write(&self, msg: String) {
        self.seen.lock().unwrap().push(msg);
    }
}

fn firings_of(serial: u64) -> Vec<i64> {
    revl_clock_firings()
        .into_iter()
        .filter(|(s, _)| *s == serial)
        .map(|(_, at)| at)
        .collect()
}

#[test]
fn timer_deterministic_firing_and_unload_cancels() {
    revl_clock_reset();
    let root = cordis::Context::new();
    let seen = Arc::new(Mutex::new(Vec::<String>::new()));
    let sink: Box<dyn Log> = Box::new(RecordLog { seen: seen.clone() });
    root.provide("log", sink).unwrap();

    // Load Heartbeat: it resolves `log` and arms its two timers at activation.
    let hb = root.plugin(heartbeat(), ());
    hb.wait().expect("Heartbeat did not reach ACTIVE");

    // Arming happened, but nothing fires unbidden — time has not advanced.
    assert_eq!(revl_clock_pending(), 2, "two timers armed at activation");
    assert!(revl_clock_firings().is_empty(), "no firing before advance");

    // Advance 30s: exactly the periodic timer's first firing.
    assert_eq!(revl_clock_advance(30_000), 1, "advance(30s) fires the periodic once");

    // Another 60s: the periodic re-arms across the span (60000, 90000).
    assert_eq!(revl_clock_advance(60_000), 2, "periodic re-arms across the span");
    assert_eq!(
        firings_of(1),
        vec![30_000, 60_000, 90_000],
        "periodic fires on every 30s boundary, deterministically"
    );
    assert!(firings_of(2).is_empty(), "one-shot has not come due yet");
    assert_eq!(
        seen.lock().unwrap().iter().filter(|m| *m == "tick").count(),
        3,
        "the firing body's emission reached the Log sink"
    );

    // Past 5m: the one-shot fires exactly once (at 300000ms) and does not
    // re-arm; the periodic keeps firing on each boundary.
    let periodic_before = firings_of(1).len();
    revl_clock_advance(300_000); // now 390000ms
    assert_eq!(firings_of(2), vec![300_000], "one-shot fires exactly once");
    assert!(firings_of(1).len() > periodic_before, "periodic kept firing across the span");
    assert_eq!(revl_clock_pending(), 1, "one-shot spent; periodic still live");

    // Unload Heartbeat: its timers' derived inverses (cancel) run LIFO — no
    // orphaned interval outlives the activation (residue-free teardown).
    hb.dispose().expect("Heartbeat dispose failed");
    assert_eq!(revl_clock_pending(), 0, "teardown cancelled the periodic — no residue");

    // No orphaned firing: advancing after teardown fires nothing.
    assert_eq!(
        revl_clock_advance(120_000),
        0,
        "no timer fires after unload (no orphaned interval)"
    );
}
