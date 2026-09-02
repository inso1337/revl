//! agent-prefilter — a THIRD-PARTY rust consumer of `revl-gate` (roadmap item 338).
//!
//! This file is not part of revl. It is what an external project looks like
//! once it depends on revl's native admission gate as a rust library: its own
//! `Cargo.toml` (in this directory) declares `revl-gate` as its only
//! dependency, and the only revl import anywhere in this project is the one
//! below.
//!
//! READ THIS BEFORE YOU COPY IT: THE ASYMMETRIC CONTRACT, RUST EDITION
//! ===================================================================
//! The py gate (`pip install revl`, `revl.gate.admit`) can both refuse and
//! ADMIT: it is the full reference compiler. This crate cannot. It decides the
//! composition and guarantee layer only, so it has three arms and none of them
//! is an admission:
//!
//! * [`Verdict::Refused`] — authoritative and fail-closed. The reference
//!   compiler refuses this source too, with the same code and the same message
//!   verbatim. This project acts on it: REJECT, and nothing about the candidate
//!   is loaded, registered or run, now or later.
//! * [`Verdict::NoObjection`] — **not an admission.** It means "this gate found
//!   nothing it is able to refuse", and a type-incorrect program lands here
//!   because the native gate does not run the reference type layer. This
//!   project ESCALATES: it asks the reference toolchain (`revl compile`, or
//!   `revl.gate.admit` on py) before anything is accepted.
//! * [`Verdict::OutsideFrontier`] — the gate declined to decide at all (a
//!   construct outside its frontier table, an oversized source, an abort in the
//!   native front end). This project ESCALATES for the same reason.
//!
//! So the word "admit" is a decision this project NEVER makes locally, and the
//! strings `REGISTER`/`ADMIT` appear nowhere in its output. What the crate buys
//! is the other direction: a local, in-process, Python-free REFUSAL that agrees
//! with the reference byte for byte on the covered corpus — a cheap pre-filter
//! in front of an expensive authoritative check, never a replacement for it.
//!
//! Refusing what the reference admits is an inconvenience. Admitting what the
//! reference refuses is the defect class the whole admission-gate arc exists to
//! prevent, and a gate with no admission arm cannot commit it.
//!
//! What this project does, concretely
//! -----------------------------------
//! 1. Imports ONLY `revl_gate` (`admit`, `gate_version`) — no serde, no revl
//!    source, no Python on the machine.
//! 2. Walks a directory of agent-authored `.rvl` candidates, pre-filters each
//!    one in-process, logs `gate_version()` once per run and, per candidate,
//!    the arm, the `code`, and the verbatim `message` on a refusal.
//! 3. Keys its verdict cache on the FULL `gate_version()` triple (`api`,
//!    `language`, `frontier`) and stores `frontier` on every record — so a
//!    verdict from THIS gate is never confused with one from the py
//!    reference-full gate, whose `frontier` differs even at the same
//!    `language` (docs/design/338-revl-as-dependency.md, "Frontier skew").
//! 4. Decides REJECT on a refusal and ESCALATE on everything else. There is no
//!    third decision, because a bare layer-1 native gate cannot honestly reach
//!    one.
//!
//! Usage:
//!   agent-prefilter candidates/            # human log
//!   agent-prefilter candidates/ --json     # machine-readable summary

use std::collections::HashMap;
use std::path::{Path, PathBuf};

// The one revl import this project makes.
use revl_gate::{admit, gate_version, GateVersion, Verdict};

/// A refusal is authoritative: this candidate is out, and stays out.
const REJECT: &str = "REJECT";
/// Everything else: this gate is not entitled to accept, so the reference
/// toolchain decides. Never a local admission.
const ESCALATE: &str = "ESCALATE";

/// One candidate's pre-filter outcome, as this project would persist it.
#[derive(Clone)]
struct Record {
    name: String,
    /// `REJECT` or `ESCALATE` — never an admission.
    decision: &'static str,
    /// The crate's arm name: `refused` / `no_objection` / `outside_frontier`.
    verdict: &'static str,
    code: Option<String>,
    message: Option<String>,
    /// The gate that produced this verdict. Recorded on EVERY record, because
    /// a verdict is only a fact together with the frontier it came from.
    frontier: &'static str,
    cached: bool,
}

/// The full `gate_version()` triple as a hashable cache key. All three fields,
/// never just `language`: two gates at the same language but different
/// `frontier` (this crate and the py reference-full gate) disagree on the same
/// source by construction, so a cached verdict is valid only for the exact
/// (api, language, frontier) that produced it.
fn cache_key(version: &GateVersion) -> (String, String, String) {
    (
        version.api.to_string(),
        version.language.to_string(),
        version.frontier.to_string(),
    )
}

/// An in-memory stand-in for what a real CI system would persist.
#[derive(Default)]
struct VerdictCache {
    store: HashMap<(String, String, String), HashMap<String, Record>>,
}

impl VerdictCache {
    fn get(&self, version: &GateVersion, name: &str) -> Option<&Record> {
        self.store.get(&cache_key(version))?.get(name)
    }

    fn put(&mut self, version: &GateVersion, record: Record) {
        self.store
            .entry(cache_key(version))
            .or_default()
            .insert(record.name.clone(), record);
    }
}

/// Map a verdict onto this project's decision. The whole security posture of a
/// native-gate consumer is these five lines: act on a refusal, escalate
/// everything else, and never invent an acceptance the gate did not give.
fn decide(verdict: &Verdict) -> &'static str {
    if verdict.is_refused() {
        REJECT
    } else {
        ESCALATE
    }
}

fn prefilter_candidate(
    path: &Path,
    cache: &mut VerdictCache,
    version: &GateVersion,
) -> std::io::Result<Record> {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();

    if let Some(hit) = cache.get(version, &name) {
        let mut cached = hit.clone();
        cached.cached = true;
        return Ok(cached);
    }

    let source = std::fs::read_to_string(path)?;
    let verdict = admit(&source);
    let record = Record {
        name,
        decision: decide(&verdict),
        verdict: verdict.kind(),
        code: verdict.code().map(|c| c.to_string()),
        // `message` is logged for a human to read (a repair signal), never
        // parsed: it is the compiler's diagnostic verbatim and is NOT part of
        // the versioned API.
        message: verdict.message().map(|m| m.to_string()),
        frontier: version.frontier,
        cached: false,
    };
    cache.put(version, record.clone());
    Ok(record)
}

fn run_prefilter(
    dir: &Path,
    cache: &mut VerdictCache,
    log: &mut dyn FnMut(String),
) -> std::io::Result<Vec<Record>> {
    let version = gate_version();
    log(format!(
        "gate_version: api={} language={} frontier={}",
        version.api, version.language, version.frontier
    ));
    // `layer` is the field a native-gate consumer must read before trusting a
    // non-refusal: it says, in prose, what this gate does and does not decide.
    log(format!("gate_layer: {}", version.layer));

    let mut paths: Vec<PathBuf> = std::fs::read_dir(dir)?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|p| p.extension().map(|e| e == "rvl").unwrap_or(false))
        .collect();
    paths.sort();

    let mut results = Vec::new();
    for path in &paths {
        let record = prefilter_candidate(path, cache, &version)?;
        match record.decision {
            // Clause 1: a refusal is authoritative. Nothing about this
            // candidate runs, now or later, in this process.
            REJECT => log(format!(
                "REJECT   {}  code={} — {}",
                record.name,
                record.code.as_deref().unwrap_or("?"),
                record.message.as_deref().unwrap_or("")
            )),
            // NOT an acceptance. The reference toolchain decides; this project
            // has only established that the native gate had no refusal to make.
            _ => log(format!(
                "ESCALATE {}  ({}: ask the reference gate — this crate issues no admissions)",
                record.name, record.verdict
            )),
        }
        results.push(record);
    }
    Ok(results)
}

// --------------------------------------------------------------------------- //
// Hand-rolled JSON, so this project's dependency list stays literally one crate.
// --------------------------------------------------------------------------- //

fn json_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn json_opt(value: Option<&str>) -> String {
    match value {
        Some(text) => json_string(text),
        None => String::from("null"),
    }
}

fn to_json(version: &GateVersion, results: &[Record]) -> String {
    let mut out = String::from("{\"gate_version\":{\"api\":");
    out.push_str(&json_string(version.api));
    out.push_str(",\"language\":");
    out.push_str(&json_string(version.language));
    out.push_str(",\"frontier\":");
    out.push_str(&json_string(version.frontier));
    out.push_str(",\"layer\":");
    out.push_str(&json_string(version.layer));
    out.push_str("},\"results\":[");
    for (index, record) in results.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push_str("{\"name\":");
        out.push_str(&json_string(&record.name));
        // Always false, on every arm, exactly as `Verdict::to_json` reports it:
        // a downstream reader of this project's output that branches on
        // `admitted` reads this tier as "never admits" rather than mistaking a
        // no-objection for an admission.
        out.push_str(",\"admitted\":false,\"decision\":");
        out.push_str(&json_string(record.decision));
        out.push_str(",\"verdict\":");
        out.push_str(&json_string(record.verdict));
        out.push_str(",\"code\":");
        out.push_str(&json_opt(record.code.as_deref()));
        out.push_str(",\"message\":");
        out.push_str(&json_opt(record.message.as_deref()));
        out.push_str(",\"frontier\":");
        out.push_str(&json_string(record.frontier));
        out.push_str(",\"cached\":");
        out.push_str(if record.cached { "true" } else { "false" });
        out.push('}');
    }
    out.push_str("]}");
    out
}

fn main() {
    let mut dir: Option<PathBuf> = None;
    let mut as_json = false;
    for arg in std::env::args().skip(1) {
        if arg == "--json" {
            as_json = true;
        } else if dir.is_none() {
            dir = Some(PathBuf::from(arg));
        } else {
            eprintln!("usage: agent-prefilter <candidates-dir> [--json]");
            std::process::exit(2);
        }
    }
    let Some(dir) = dir else {
        eprintln!("usage: agent-prefilter <candidates-dir> [--json]");
        std::process::exit(2);
    };

    let mut cache = VerdictCache::default();
    let mut sink: Box<dyn FnMut(String)> = if as_json {
        Box::new(|_line: String| {})
    } else {
        Box::new(|line: String| println!("{}", line))
    };
    let results = match run_prefilter(&dir, &mut cache, &mut *sink) {
        Ok(results) => results,
        Err(err) => {
            eprintln!("agent-prefilter: {}: {}", dir.display(), err);
            std::process::exit(1);
        }
    };
    drop(sink);

    if as_json {
        println!("{}", to_json(&gate_version(), &results));
    }

    // Exit status is 0 on a completed run: a batch containing a refusal is the
    // expected shape of a pass over mixed agent proposals — the refusal was
    // reported, never swallowed. A caller wanting a hard fail inspects the
    // records (or this project's exit code policy is its own to choose).
}
