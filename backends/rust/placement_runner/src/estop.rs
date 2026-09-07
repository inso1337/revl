//! The operator E-Stop's shared vocabulary on the rust tier — roadmap item 443,
//! issue #122. The rust twin of `backends/typescript/estop.ts` and
//! `src/revl/estop.py`.
//!
//! `docs/design/443-estop.md` is the reasoning of record. Item 443 landed the
//! halt on the py reference tier: a latch file, a crossing seam that refuses
//! once it is armed, and an in-flight inventory. The non-py tiers kept their
//! cooperative teardown and had NO E-Stop, so a placement halt SIGKILLed a rust
//! child and reported its residue UNKNOWN. This module is the rust tier honoring
//! the latch:
//!
//!   * the latch READER (`latch_path`, `read_latch`, `estop_engaged`),
//!     byte-for-byte the rule `src/revl/estop.py::read_latch` applies — including
//!     the fail-closed rule that a malformed latch still reads as HALTED — so the
//!     tiers cannot drift on what an armed (or corrupted) latch means;
//!   * the in-flight crossing REGISTRY (`begin_crossing`/`end_crossing`/
//!     `in_flight_crossings`): a crossing still executing when the button is hit
//!     is the AMBIGUOUS one (item 440);
//!   * the halt INVENTORY (`estop_inventory`/`estop_halt_line`), shaped into the
//!     merged residue schema `src/revl/placement.py::_estop_halt_report` reads.
//!
//! The accept seam (`main.rs::handle_conn`) consults `estop_engaged` and records
//! crossings; the idle watcher (`main.rs`) prints the halt line and exits. This
//! module is the vocabulary those share.

use serde_json::{json, Value};
use std::sync::{Mutex, OnceLock};

/// The ambient latch path, equivalent to `--estop-latch FILE`. The conductor
/// (`src/revl/placement.py`) hands a honoring child the latch in its spec; the
/// runner publishes it here so the seams and the watcher read one latch. Kept
/// identical to `estop.py::LATCH_ENV` / `estop.ts::LATCH_ENV`.
pub const LATCH_ENV: &str = "REVL_ESTOP_LATCH";

/// What a latch-honoring child prints when the latch trips: its own in-flight
/// inventory, on one line, so the conductor merges it without a second channel.
/// Kept identical to `estop.py::HALTED_LINE`.
pub const HALTED_LINE: &str = "HALTED";

/// The latch file to act on: an explicit path, else `<wal>.estop`, else the
/// ambient `REVL_ESTOP_LATCH`. Mirrors `estop.py::latch_path`.
pub fn latch_path(latch: Option<&str>, wal: Option<&str>, env: bool) -> Option<String> {
    if let Some(l) = latch {
        if !l.is_empty() {
            return Some(l.to_string());
        }
    }
    if let Some(w) = wal {
        if !w.is_empty() {
            return Some(format!("{w}.estop"));
        }
    }
    if env {
        return std::env::var(LATCH_ENV).ok().filter(|v| !v.is_empty());
    }
    None
}

/// The halt an operator wrote at `path`, or `None` when the latch is absent.
///
/// A latch that EXISTS but does not parse still reads as HALTED. Failing open on
/// a malformed emergency stop is the one failure mode this feature exists to
/// prevent, so every reader — the py runtime seam, the CLI, the conductor, the
/// ts tier and now this — applies the same rule. A latch the OS refuses to open
/// at all (missing file, permission error) reads as absent, matching
/// `estop.py::read_latch` (FileNotFoundError/OSError -> None).
pub fn read_latch(path: Option<&str>) -> Option<Value> {
    let path = path?;
    if path.is_empty() {
        return None;
    }
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        // ENOENT or any other OS-level read failure: the latch is not readable,
        // so it is not a halt. (A malformed BUT readable latch is handled below.)
        Err(_) => return None,
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(v) if v.is_object() => Some(v),
        // A parse failure, or a JSON value that is not an object (a bare
        // array/number/string), still halts.
        _ => Some(unreadable()),
    }
}

fn unreadable() -> Value {
    json!({
        "halted": true,
        "reason": "operator halt (unreadable latch)",
        "operator": "unknown",
    })
}

/// Whether a halt is in force on the latch at `path`.
pub fn estop_engaged_at(path: Option<&str>) -> bool {
    read_latch(path).is_some()
}

/// Whether a halt is in force on the latch this process watches (the ambient
/// `REVL_ESTOP_LATCH`). The accept seam consults this on each incoming crossing:
/// the cost is one file read per crossing WHILE a latch is armed, and nothing at
/// all when none is — the default — because `latch_path` short-circuits to None.
pub fn estop_engaged() -> bool {
    estop_engaged_at(latch_path(None, None, true).as_deref())
}

// --- the in-flight crossing registry (item 443, issue #122) ------------------

/// One boundary crossing recorded while it is in flight. A crossing still in the
/// registry when the latch trips is AMBIGUOUS: its at-most-once attempt may or
/// may not have landed (item 440).
#[derive(Clone)]
pub struct Crossing {
    pub key: String,
    pub method: String,
    /// "accept" (an incoming call the serve seam is answering) or "dispatch".
    pub direction: String,
    pub seq: i64,
}

struct Registry {
    in_flight: Vec<Crossing>,
    seq: i64,
}

fn registry() -> &'static Mutex<Registry> {
    static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(Registry { in_flight: Vec::new(), seq: 0 }))
}

/// Record a crossing as in flight and return its sequence number. The seam pairs
/// it with `end_crossing` (via a drop guard) so an unwinding handler still leaves
/// the registry clean.
pub fn begin_crossing(key: &str, method: &str, direction: &str) -> i64 {
    let mut reg = registry().lock().unwrap();
    reg.seq += 1;
    let seq = reg.seq;
    reg.in_flight.push(Crossing {
        key: key.to_string(),
        method: method.to_string(),
        direction: direction.to_string(),
        seq,
    });
    seq
}

/// Clear a recorded crossing once its handler returns.
pub fn end_crossing(seq: i64) {
    let mut reg = registry().lock().unwrap();
    reg.in_flight.retain(|c| c.seq != seq);
}

/// A snapshot of the crossings executing right now, ordered by sequence so the
/// inventory is deterministic.
pub fn in_flight_crossings() -> Vec<Crossing> {
    let reg = registry().lock().unwrap();
    let mut out = reg.in_flight.clone();
    out.sort_by_key(|c| c.seq);
    out
}

/// An RAII guard that ends the crossing when it drops, so the accept seam cannot
/// leak a registry entry on an early return or a panic.
pub struct CrossingGuard(i64);

impl CrossingGuard {
    pub fn new(key: &str, method: &str, direction: &str) -> Self {
        CrossingGuard(begin_crossing(key, method, direction))
    }
}

impl Drop for CrossingGuard {
    fn drop(&mut self) {
        end_crossing(self.0);
    }
}

// --- the halt inventory (item 443, issue #122) -------------------------------

fn string_field(record: &Option<Value>, key: &str, fallback: &str) -> String {
    record
        .as_ref()
        .and_then(|r| r.get(key))
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback)
        .to_string()
}

/// Shape the crossings that were in flight when the button was hit into the
/// merged residue schema (`src/revl/placement.py::_estop_halt_report`),
/// byte-compatible with the shape the py runner and the ts tier emit.
///
/// A crossing still executing when the operator armed the latch is AMBIGUOUS —
/// its at-most-once attempt may or may not have landed (item 440), the designed
/// outcome of an operator halt, not an edge case. This tier keeps no
/// witnessed-inverse ledger, so `stranded` is empty and HONESTLY so: the halt
/// reports what it can name (the crossings in flight) rather than inventing a
/// book it does not keep, and the conductor never reads that empty list as
/// "nothing was owed" because the ambiguous crossings are still reported.
pub fn estop_inventory(process: &str, crossings: &[Crossing], record: &Option<Value>) -> Value {
    let in_flight: Vec<Value> = crossings
        .iter()
        .map(|c| {
            json!({
                "kind": "estop-ambiguous",
                "state": "unresolved",
                "component": c.key,
                "method": c.method,
                "seq": c.seq,
                "entry": "crossing",
                "direction": c.direction,
                "attemptedFlag": true,
                "outcome": "unknown",
            })
        })
        .collect();
    json!({
        "process": process,
        "verdict": "halted",
        "reason": string_field(record, "reason", "operator halt"),
        "operator": string_field(record, "operator", "unknown"),
        "activations": [],
        "inFlight": in_flight,
        "stranded": [],
        "resumable": false,
    })
}

/// The single line a latch-honoring child prints when the button is hit:
/// `[name] HALTED {inventory}`. The conductor parses it off stdout by the
/// `HALTED_LINE` prefix (`src/revl/placement.py::pump`) and merges the inventory
/// into the halt report without a second channel — the contract the py runner
/// and the ts tier already meet.
pub fn estop_halt_line(process: &str, crossings: &[Crossing], record: &Option<Value>) -> String {
    let inv = estop_inventory(process, crossings, record);
    format!("[{process}] {HALTED_LINE} {}", serde_json::to_string(&inv).unwrap_or_else(|_| "{}".into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str, body: &str) -> String {
        let dir = std::env::temp_dir().join(format!("revl_estop_rs_{}_{}", std::process::id(), name));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("halt.estop");
        std::fs::write(&path, body).unwrap();
        path.to_string_lossy().into_owned()
    }

    #[test]
    fn absent_latch_is_not_halted() {
        let missing = std::env::temp_dir().join("revl_estop_rs_nope.estop");
        let p = missing.to_string_lossy();
        assert!(read_latch(Some(&p)).is_none());
        assert!(!estop_engaged_at(Some(&p)));
        assert!(read_latch(None).is_none());
    }

    #[test]
    fn armed_latch_carries_fields() {
        let path = tmp("armed", r#"{"halted":true,"reason":"runaway loop","operator":"ops@example"}"#);
        let record = read_latch(Some(&path)).expect("armed latch must halt");
        assert_eq!(record["reason"], "runaway loop");
        assert_eq!(record["operator"], "ops@example");
        assert!(estop_engaged_at(Some(&path)));
    }

    #[test]
    fn fails_closed_on_malformed_latch() {
        // The one failure mode this feature exists to prevent.
        let path = tmp("garbage", "{ this is not json");
        let record = read_latch(Some(&path)).expect("a malformed latch must still halt");
        assert_eq!(record["halted"], true);
        assert!(estop_engaged_at(Some(&path)));

        let arr = tmp("arr", "[1, 2, 3]");
        assert!(estop_engaged_at(Some(&arr)));
    }

    #[test]
    fn latch_path_precedence() {
        assert_eq!(latch_path(Some("/a/b.estop"), None, false).as_deref(), Some("/a/b.estop"));
        assert_eq!(latch_path(None, Some("/run/session.wal"), false).as_deref(), Some("/run/session.wal.estop"));
        assert_eq!(latch_path(None, None, false), None);
    }

    #[test]
    fn crossing_registry_records_and_clears() {
        let seq = begin_crossing("db", "query", "accept");
        assert!(in_flight_crossings().iter().any(|c| c.seq == seq && c.key == "db" && c.method == "query"));
        end_crossing(seq);
        assert!(!in_flight_crossings().iter().any(|c| c.seq == seq));
    }

    #[test]
    fn crossing_guard_clears_on_drop() {
        let seq;
        {
            let g = CrossingGuard::new("work", "compute", "accept");
            seq = g.0;
            assert!(in_flight_crossings().iter().any(|c| c.seq == seq));
        }
        assert!(!in_flight_crossings().iter().any(|c| c.seq == seq));
    }

    #[test]
    fn inventory_and_halt_line_shape() {
        let seq = begin_crossing("db", "write", "accept");
        let record = Some(json!({"reason": "runaway loop", "operator": "ops@example"}));
        let crossings = in_flight_crossings();
        let inv = estop_inventory("edge", &crossings, &record);
        assert_eq!(inv["verdict"], "halted");
        assert_eq!(inv["resumable"], false);
        assert_eq!(inv["reason"], "runaway loop");
        let in_flight = inv["inFlight"].as_array().expect("inFlight is a list");
        assert!(!in_flight.is_empty());
        assert_eq!(in_flight[0]["kind"], "estop-ambiguous");
        assert_eq!(in_flight[0]["outcome"], "unknown");
        assert_eq!(inv["stranded"].as_array().expect("stranded is a list").len(), 0);

        let line = estop_halt_line("edge", &crossings, &record);
        assert!(line.starts_with("[edge] HALTED "));
        let payload = line.trim_start_matches("[edge] HALTED ");
        let parsed: Value = serde_json::from_str(payload).expect("halt line payload is JSON");
        assert_eq!(parsed["verdict"], "halted");
        end_crossing(seq);
    }
}
