//! In-process admission gate for an agent framework, on rust (roadmap item 333,
//! Slice 2) - the twin of `bench/inprocess_gate_harness.py`.
//!
//! An agent tool-generation loop written in rust links the revl admission gate
//! as a LIBRARY and screens every component it proposes IN ITS OWN PROCESS,
//! before that component can run: no `revl mcp serve` subprocess, no IPC, no
//! wire, and - the half the py harness cannot claim - no Python anywhere. This
//! program is that embed made concrete. It holds a batch of proposed
//! candidates, calls `revl_gate::admit` on each in-process - and
//! `revl_gate::admit_into` on the ones that are proposed against the running
//! composition - checks the invariants a host may actually rely on, and
//! measures the per-candidate round-trip as a distribution.
//!
//! # What this gate decides, and the one sentence that matters
//!
//! `revl-gate` is the SELF-HOST front end compiled to rust. It decides the
//! COMPOSITION AND GUARANTEE layer (`G1`..`G4`, `A1`, `PRELUDE`, and parse
//! failures as `BAD`). It runs NO type layer, so it has **no admission arm at
//! all**: the three verdicts are `Refused`, `NoObjection` and
//! `OutsideFrontier`, and `to_json` reports `"admitted": false` on every one of
//! them.
//!
//! It DOES have a manifest arm (`revl_gate::admit_into`, issue #346): the same
//! fold over the UNION of the running composition's provisions and the
//! candidate's. That closes the ambient half of G2/G3 - a candidate that
//! collides with what is already running is refused here, with the fold's own
//! why-trace - and it still issues no admission: resolving the candidate's
//! `requires` against a live provider and type-checking it is the reference
//! type layer, which this gate does not run.
//!
//! So the rust harness is NOT a translation of the py harness's claim. The py
//! harness proves an IDENTITY (`revl.gate.admit` is the reference admission
//! path, so its verdict IS the reference verdict). This harness proves a
//! DIFFERENTIAL, and an asymmetric one:
//!
//! * a native REFUSAL is worth acting on - it byte-agrees with the reference
//!   compiler on the covered corpus, and it is free, local and Python-free;
//! * a native NO-OBJECTION is **not** an admission and a host that treats it as
//!   one ships the exact defect the whole arc exists to prevent. Before running
//!   anything, the host still needs a reference verdict (`revl compile`, or
//!   `revl.gate.admit` on py).
//!
//! This is the fail-closed direction 332's release gate fixes and 333's design
//! (`docs/design/333-inprocess-gate.md`, "Differential agreement (rust)")
//! inherits verbatim: refusing what the reference admits is an inconvenience;
//! admitting what the reference refuses is a security hole. The crate closes
//! the second direction STRUCTURALLY (there is nothing to return), and this
//! harness holds the first one over its batch.
//!
//! # What the harness MEASURES rather than assumes
//!
//! The batch is the py harness's own candidates plus composition-layer probes,
//! and the point of running it is to price the gap between the two tiers per
//! candidate rather than to restate the design's expectations. Three things the
//! measurement says, none of which was safe to assume:
//!
//! 1. **The gate catches the composition layer and nothing else.** Of the seven
//!    batch candidates the py admission gate REFUSES, this gate refuses two
//!    (`G2` provision conflict, `G4` undeclared emission). The other five - an
//!    unresolved `requires`, an incomplete `provide`, a genuine parse failure, a
//!    draft with an open hole, a type error - come back as no-objections. Every
//!    one is in the tolerated direction (a no-objection is never an admission),
//!    and every one is a reason a host may not treat a no-objection as a green.
//! 2. **The manifest arm refuses what py refuses, and admits nothing.** The
//!    realistic agent shape - admit a candidate AGAINST the running
//!    composition - is available here, and the batch carries the two
//!    candidates that price it. A candidate that re-provides a key the running
//!    composition already holds is REFUSED here (`G2`, the same code and the
//!    same why-trace the py gate gives). The py harness's `cache_layer`,
//!    whose `requires` RESOLVES against the running provider, is ADMITTED by
//!    py and gets a no-objection here: that is the half of the question this
//!    gate does not answer, and it is reported as a gap rather than as an
//!    agreement.
//! 3. **The screen is not cheap, and its cost is super-linear in candidate
//!    size.** The py in-process round-trip is tenths of a millisecond. This one
//!    is milliseconds at 218 bytes and grows roughly with the SQUARE of the
//!    source size. The cost section measures it across sizes and shapes rather
//!    than inheriting py's headline, because inheriting it would be false.
//!
//! # The honest boundary (design section 4)
//!
//! This is a COMPILE-TIME screen, not a sandbox. A component this gate refuses
//! never runs in the embedder's process. A component it does not refuse has
//! only been screened at the composition/guarantee layer - it has not been
//! type-checked, admitted, or confined, and no manifest parameter changes that:
//! on the manifest arm a non-refusal means "nothing in the running composition
//! collides with this", not "this can join it". `admitted` is not a thing this
//! gate issues, and even a py admission is not "safe to run unwitnessed": the
//! reversible-run half is item 334.
//!
//! # Usage
//!
//! Always `--release`: a debug build's timings are a fiction, and the numbers
//! are half of what this harness is for.
//!
//! ```text
//! cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml
//! cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml -- --iters 100
//! cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml -- --json
//! cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml -- --write
//! ```
//!
//! `--json` emits the whole report, INCLUDING each candidate's source, so
//! `tests/test_inprocess_gate_rust.py` can re-derive the py verdict for the
//! identical bytes and hold the two harnesses against each other. Nothing about
//! the default run needs Python.

use revl_gate::{admit, admit_into, compile_to, gate_version, Tier, Verdict, MAX_SOURCE_BYTES};
use std::process::ExitCode;
use std::time::Instant;

// --------------------------------------------------------------------------- //
// The batch of proposed candidates.
//
// Five of these are the py harness's candidates BYTE FOR BYTE
// (`bench/inprocess_gate_harness.py`); `tests/test_inprocess_gate_rust.py`
// asserts that equality, so a py-side edit reds the driver instead of silently
// letting the two harnesses screen different programs.
//
// Two of them are also proposed AGAINST the running composition (`into:
// Some(HELD_MANIFEST)`), which is the manifest arm of issue #346.
// --------------------------------------------------------------------------- //

/// The running composition every manifest-arm candidate is admitted INTO.
///
/// The same composition the py harness holds: `bench/admission_latency.py::
/// RUNNING`, compiled and then flattened by `revl.manifest_wire` - `Kv`
/// provides `store`, `App` provides `app` and requires `store`, joined by `;`.
/// `tests/test_inprocess_gate_rust.py` recomputes that wire from the py
/// harness's own `base_manifest()`, so a py-side change to the running
/// composition reds here instead of leaving the two tiers admitting into
/// different worlds.
const HELD_MANIFEST: &str = "Kv/store/;App/app/;App<store";

/// `bench/admission_latency.py::CANDIDATE_STANDALONE`, the py harness's
/// `standalone_twin`: standalone-valid, `Store` inlined. py ADMITS it.
const STANDALONE_TWIN: &str = r#"
service Store {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
  emission fn put(key: Str, value: Str)
}
service Cache { fn lookup(key: Str) -> Str }
component CacheLayer requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
"#;

/// `bench/admission_latency.py::CANDIDATE`, the py harness's `cache_layer`. py
/// ADMITS it INTO the running composition (`admit_into`) and REFUSES it
/// standalone. Standalone this gate raises no objection to a `requires` that
/// resolves to nothing; admitted INTO the held composition it still raises no
/// objection, because the fold has nothing to refuse - `cache_layer` neither
/// collides with `Kv`/`App` nor closes a cycle. That is the still-open half of
/// the manifest question, priced rather than papered over: RESOLVING the
/// `requires` and ADMITTING is the reference type layer.
const CACHE_LAYER: &str = r#"
service Cache { fn lookup(key: Str) -> Str }
component CacheLayer requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
"#;

/// A candidate that COLLIDES with the running composition: it provides `store`
/// in the shared realm, which `Kv` already provides in the held manifest. Its
/// `Store` is declared with the running provider's own surface, so the
/// interface-drift check (a reference check this gate does not run) has nothing
/// to say and the collision is the only verdict in play.
///
/// This is the manifest arm's negative exit case (issue #346): the py gate
/// refuses it `G2` with `provision conflict: key \`store\` is provided by both
/// Kv and Rogue (G2)`, and this gate must refuse it with the SAME code and the
/// same sentence. It is deliberately NOT in `bench/inprocess_gate_harness.py`:
/// the py harness's two manifest negatives exercise the A6/interface-drift
/// refusals, which live in the type layer this gate does not run. This one is
/// built to land on the leg the two tiers DO share.
const AMBIENT_PROVISION_CONFLICT: &str = r#"
service Store {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
  emission fn put(key: Str, value: Str)
}
component Rogue provides store: Store {
  provide store {
    fn get(key) = key
    fn bump(n) = n
    fn put(key, value) = value
  }
}
"#;

/// The py harness's `incomplete_provide`: provides a service but omits one of
/// its declared methods.
const INCOMPLETE_PROVIDE: &str = r#"
service Two { fn a() -> Str  fn b() -> Str }
component Half provides two: Two { provide two { fn a() = "x" } }
"#;

/// The py harness's `syntax_error`: a genuine parse failure.
const SYNTAX_ERROR: &str = "component X provides { fn = }\n";

/// The py harness's `hole_draft`. py REFUSES it (`T3`: an open typed hole may
/// never run). This gate has no hole check, so it raises no objection - which
/// is a NO-OBJECTION and never an admission. See the crate docs.
const HOLE_DRAFT: &str = r#"service Lookup { fn lookup(key: Str) -> Str }
component Drafty provides lk: Lookup {
  provide lk { fn lookup(key) = hole "look it up in the store" }
}
"#;

/// Two components provide the same service: the composition layer this gate
/// DOES decide (`G2`).
const PROVISION_CONFLICT: &str = r#"service S { fn op(x: Str) -> Str }
component A provides s: S { provide s { fn op(x) { return x } } }
component B provides s: S { provide s { fn op(x) { return x } } }
"#;

/// An emission called from a body without being declared: `G4`.
const UNDECLARED_EMISSION: &str = r#"extern emission fn audit_write(msg: Str) -> Int = @py { return 1 }
service Cache { fn put(key: Str) }
component C provides cache: Cache {
  provide cache { fn put(key) { let n = audit_write(key) } }
}
"#;

/// A program the REFERENCE refuses on the type layer and this gate cannot see.
/// Its presence in the batch is the point: it is the shape a host must not read
/// as an admission.
const TYPE_LAYER_MISS: &str = "fn f() -> Int { return \"s\" }\n";

/// A source the crate declines to decide because it is over the size bound.
///
/// This used to be an `.is_digit()` call — a reference builtin the self-host
/// lowering did not carry, so it sat in the generated frontier table. Item 391
/// PORTED it (and every other builtin the table named), which emptied the
/// lexical tables and left the size bound as the only always-live fail-closed
/// trigger. The batch still has to exercise that arm, so it exercises the one
/// that is left: a single tiny `fn` behind a comment large enough to cross
/// `revl_gate::MAX_SOURCE_BYTES`. The reference ADMITS it (a comment is not a
/// program) and this gate declines to decide it, which is exactly the shape a
/// host must not read as an admission.
fn frontier_oversized() -> &'static str {
    let mut source = String::with_capacity(revl_gate::MAX_SOURCE_BYTES + 128);
    source.push_str("// ");
    while source.len() <= revl_gate::MAX_SOURCE_BYTES {
        source.push_str("padding ");
    }
    source.push_str("\nfn id(x: Int) -> Int { return x }\n");
    Box::leak(source.into_boxed_str())
}

struct Candidate {
    /// The py harness's name where the candidate is shared, so the driver can
    /// pair them without a second table.
    name: &'static str,
    source: &'static str,
    /// Documentation for the report; the verdict is decided by the gate, never
    /// by this string.
    note: &'static str,
    /// True where this exact source also appears in
    /// `bench/inprocess_gate_harness.py`.
    shared_with_py: bool,
    /// The running composition to admit this candidate INTO, or `None` for the
    /// standalone question alone. `Some(rows)` adds a SECOND screen
    /// (`revl_gate::admit_into`) whose verdict the report carries alongside the
    /// standalone one - the manifest arm of issue #346.
    into: Option<&'static str>,
}

fn batch() -> Vec<Candidate> {
    vec![
        Candidate {
            name: "standalone_twin",
            source: STANDALONE_TWIN,
            note: "standalone-valid, Store inlined; py ADMITS it",
            shared_with_py: true,
            into: None,
        },
        Candidate {
            name: "cache_layer",
            source: CACHE_LAYER,
            note: "requires a Store not in the source; py refuses it standalone and ADMITS it into the running manifest",
            shared_with_py: true,
            into: Some(HELD_MANIFEST),
        },
        Candidate {
            name: "ambient_provision_conflict",
            source: AMBIENT_PROVISION_CONFLICT,
            note: "provides a key the running composition already provides; py refuses it into the manifest (G2) and ADMITS it standalone",
            shared_with_py: false,
            into: Some(HELD_MANIFEST),
        },
        Candidate {
            name: "incomplete_provide",
            source: INCOMPLETE_PROVIDE,
            note: "provides a service but omits a declared method; py refuses",
            shared_with_py: true,
            into: None,
        },
        Candidate {
            name: "provision_conflict",
            source: PROVISION_CONFLICT,
            note: "two components provide the same service; py refuses (G2)",
            shared_with_py: false,
            into: None,
        },
        Candidate {
            name: "undeclared_emission",
            source: UNDECLARED_EMISSION,
            note: "an undeclared emission is called from a body; py refuses (G4)",
            shared_with_py: false,
            into: None,
        },
        Candidate {
            name: "syntax_error",
            source: SYNTAX_ERROR,
            note: "a genuine parse failure; py refuses",
            shared_with_py: true,
            into: None,
        },
        Candidate {
            name: "hole_draft",
            source: HOLE_DRAFT,
            note: "a draft with an open typed hole; py refuses (T3)",
            shared_with_py: true,
            into: None,
        },
        Candidate {
            name: "type_layer_miss",
            source: TYPE_LAYER_MISS,
            note: "a type error; py refuses (T1)",
            shared_with_py: false,
            into: None,
        },
        Candidate {
            name: "frontier_oversized",
            source: frontier_oversized(),
            note: "a source over the size bound; py ADMITS, this gate is not entitled to decide",
            shared_with_py: false,
            into: None,
        },
    ]
}

// --------------------------------------------------------------------------- //
// The in-process screen: one native call per candidate, no subprocess, no IPC.
// --------------------------------------------------------------------------- //

/// The `(kind, code)` pair a host branches on. This is the rust twin of the py
/// harness's `(admitted, code)`: there is no `admitted` here because this gate
/// issues none, so the ARM is the signal.
fn screen(source: &str) -> (&'static str, Option<String>) {
    let verdict = admit(source);
    (verdict.kind(), verdict.code().map(|c| c.to_string()))
}

/// The manifest arm's twin: the same pair, for `admit_into` against the running
/// composition's rows. A separate call and a separate record on purpose - the
/// two questions have different answers and a report that merged them could not
/// tell an agreement from a gap.
fn screen_into(source: &str, manifest: &str) -> (&'static str, Option<String>) {
    let verdict = admit_into(source, manifest);
    (verdict.kind(), verdict.code().map(|c| c.to_string()))
}

/// One screened arm, standalone or manifest.
struct Arm {
    kind: &'static str,
    code: Option<String>,
    message: Option<String>,
    json: String,
}

impl Arm {
    fn of(verdict: &Verdict) -> Arm {
        Arm {
            kind: verdict.kind(),
            code: verdict.code().map(|c| c.to_string()),
            message: verdict.message().map(|m| m.to_string()),
            json: verdict.to_json(),
        }
    }
}

struct Record {
    name: &'static str,
    note: &'static str,
    shared_with_py: bool,
    source: String,
    kind: &'static str,
    code: Option<String>,
    message: Option<String>,
    json: String,
    /// The rows this candidate was admitted into, and the manifest arm's
    /// verdict - `None` for a standalone-only candidate.
    into: Option<(&'static str, Arm)>,
}

fn screen_batch(batch: &[Candidate]) -> Vec<Record> {
    batch
        .iter()
        .map(|c| {
            let verdict = admit(c.source);
            Record {
                name: c.name,
                note: c.note,
                shared_with_py: c.shared_with_py,
                source: c.source.to_string(),
                kind: verdict.kind(),
                code: verdict.code().map(|c| c.to_string()),
                message: verdict.message().map(|m| m.to_string()),
                json: verdict.to_json(),
                into: c
                    .into
                    .map(|manifest| (manifest, Arm::of(&admit_into(c.source, manifest)))),
            }
        })
        .collect()
}

// --------------------------------------------------------------------------- //
// Invariants a host may actually rely on. Each returns the offenders, so a
// failure names what broke rather than just going red.
// --------------------------------------------------------------------------- //

/// THE security clause, held over the batch: nothing a host can branch on may
/// read as an admission. `admitted` is false on the wire for every arm, the arm
/// itself is one of the three known names, and no arm claims otherwise. The
/// manifest arm is held to it too - a non-refusal there is a statement about
/// collisions with the running composition, never an admission into it.
fn admission_offenders(records: &[Record]) -> Vec<String> {
    let mut bad: Vec<String> = records
        .iter()
        .filter(|r| {
            !r.json.contains("\"admitted\":false")
                || !matches!(r.kind, "refused" | "no_objection" | "outside_frontier")
        })
        .map(|r| format!("{}: {}", r.name, r.json))
        .collect();
    for r in records {
        if let Some((manifest, arm)) = &r.into {
            if !arm.json.contains("\"admitted\":false")
                || !matches!(arm.kind, "refused" | "no_objection" | "outside_frontier")
            {
                bad.push(format!("{} into {}: {}", r.name, manifest, arm.json));
            }
        }
    }
    bad
}

/// The wire shape fails closed: a refusal and a frontier gap both carry a code
/// AND a non-empty why-trace (a refusal without one is not actionable, and an
/// unactionable refusal gets ignored); a no-objection carries neither. Held on
/// the manifest arm as well, so the two arms cannot drift apart in the shape a
/// host parses.
fn shape_offenders(records: &[Record]) -> Vec<String> {
    let mut bad = Vec::new();
    let check = |label: String, kind: &str, code: &Option<String>,
                 message: &Option<String>, bad: &mut Vec<String>| {
        if kind == "no_objection" {
            if code.is_some() || message.is_some() {
                bad.push(format!("{}: a no-objection must carry no code/message", label));
            }
        } else {
            if code.as_deref().unwrap_or("").is_empty() {
                bad.push(format!("{}: {} carries no code", label, kind));
            }
            if message.as_deref().unwrap_or("").is_empty() {
                bad.push(format!("{}: {} carries no why-trace", label, kind));
            }
        }
    };
    for r in records {
        check(r.name.to_string(), r.kind, &r.code, &r.message, &mut bad);
        if let Some((manifest, arm)) = &r.into {
            check(format!("{} (into {})", r.name, manifest), arm.kind, &arm.code,
                  &arm.message, &mut bad);
        }
    }
    bad
}

/// A deterministic shuffle with no `rand` dependency (the harness's dependency
/// graph is the crate and nothing else, by design). A 64-bit LCG, seeded to
/// match the py harness's `random.Random(1729)` in SPIRIT - the claim is
/// order-independence, which any permutation tests, not a shared permutation.
fn shuffled_order(len: usize, seed: u64) -> Vec<usize> {
    let mut order: Vec<usize> = (0..len).collect();
    let mut state = seed;
    for i in (1..len).rev() {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let j = ((state >> 33) as usize) % (i + 1);
        order.swap(i, j);
    }
    order
}

/// A2, the rust twin: screening the batch in a fixed order and in a shuffled
/// order IN THE SAME PROCESS yields identical per-candidate verdicts. This is
/// the property that proves the gate is stateless - if any per-process cache
/// made screen N depend on screen N-1, a candidate's verdict would differ
/// between the two orderings. Both arms are screened: a statelessness bug that
/// only showed up once a manifest was in play would be the worst kind here,
/// since the manifest is the parameter being threaded. Returns the candidates
/// that drifted.
fn order_dependence(batch: &[Candidate]) -> Vec<String> {
    let fixed: Vec<Vec<(&'static str, Option<String>)>> =
        batch.iter().map(screens).collect();
    let mut shuffled: Vec<Option<Vec<(&'static str, Option<String>)>>> = vec![None; batch.len()];
    for index in shuffled_order(batch.len(), 1729) {
        shuffled[index] = Some(screens(&batch[index]));
    }
    batch
        .iter()
        .enumerate()
        .filter_map(|(i, c)| {
            let second = shuffled[i].clone().expect("every index screened");
            if second == fixed[i] {
                None
            } else {
                Some(format!("{}: {:?} then {:?}", c.name, fixed[i], second))
            }
        })
        .collect()
}

/// Every arm a candidate is screened on, in a fixed order: the standalone
/// question first, the manifest question second when the candidate is proposed
/// against a running composition.
fn screens(candidate: &Candidate) -> Vec<(&'static str, Option<String>)> {
    let mut out = vec![screen(candidate.source)];
    if let Some(manifest) = candidate.into {
        out.push(screen_into(candidate.source, manifest));
    }
    out
}

/// The fail-closed paths, from the consumer side: an oversized source is
/// DECLINED rather than risked (the emitted front end is deeply recursive and a
/// stack exhaustion aborts, which cannot be turned back into a refusal), and
/// `compile_to` refuses on both tiers (Stage 4: the self-host emitters still
/// carry `@py`-only helper externs, so there is no native emitter to call).
struct FailClosed {
    oversized_declined: bool,
    oversized_code: Option<String>,
    compile_to_refuses_both_tiers: bool,
}

fn fail_closed() -> FailClosed {
    let big = "fn id(x: Int) -> Int { return x } ".repeat(20_000);
    assert!(big.len() > MAX_SOURCE_BYTES, "the oversize probe must be oversized");
    let oversized = admit(&big);
    let both = [Tier::Py, Tier::Rust].iter().all(|tier| {
        matches!(
            compile_to("fn id(x: Int) -> Int { return x }", *tier),
            Err(Verdict::OutsideFrontier { .. })
        )
    });
    FailClosed {
        oversized_declined: oversized.is_undecided(),
        oversized_code: oversized.code().map(|c| c.to_string()),
        compile_to_refuses_both_tiers: both,
    }
}

// --------------------------------------------------------------------------- //
// Cost: a distribution over candidate size, not one reasserted headline.
// --------------------------------------------------------------------------- //

/// A standalone candidate providing a service with `methods` methods - the knob
/// on candidate source size, and the same knob the py twin
/// (`make_candidate_source`) turns, so the two curves are comparable.
///
/// The py twin ALSO varies the running-manifest size. This harness measures the
/// standalone screen only: the cost section below says nothing about the
/// manifest arm, and does not get to imply that it is free. What it costs is a
/// measurement nobody has taken here - a gap in this harness, not a claim.
fn sized_candidate(methods: usize) -> String {
    let decls: Vec<String> = (0..methods)
        .map(|i| format!("  fn m{}(key: Str) -> Str", i))
        .collect();
    let impls: Vec<String> = (0..methods)
        .map(|i| format!("    fn m{}(key) = key", i))
        .collect();
    format!(
        "service SvcSized {{\n{}\n}}\ncomponent Sized provides c: SvcSized {{\n  provide c {{\n{}\n  }}\n}}\n",
        decls.join("\n"),
        impls.join("\n")
    )
}

struct Stats {
    n: usize,
    min: f64,
    median: f64,
    p90: f64,
    p99: f64,
    mean: f64,
}

fn time_ms(source: &str, iters: usize) -> Stats {
    let mut samples = Vec::with_capacity(iters);
    for _ in 0..iters {
        let start = Instant::now();
        let verdict = admit(source);
        samples.push(start.elapsed().as_secs_f64() * 1000.0);
        // Keep the verdict observable so no optimiser is tempted to drop the
        // call the whole measurement is about.
        std::hint::black_box(verdict.kind());
    }
    samples.sort_by(|a, b| a.partial_cmp(b).expect("no NaN in a duration"));
    let n = samples.len();
    let median = if n % 2 == 0 {
        (samples[n / 2 - 1] + samples[n / 2]) / 2.0
    } else {
        samples[n / 2]
    };
    Stats {
        n,
        min: samples[0],
        median,
        p90: samples[(n * 90 / 100).min(n - 1)],
        p99: samples[(n * 99 / 100).min(n - 1)],
        mean: samples.iter().sum::<f64>() / n as f64,
    }
}

/// A candidate of roughly `target_bytes` whose bulk is STATEMENTS inside one
/// method body: many tokens, one declaration. Half of the shape probe below.
fn statement_heavy(target_bytes: usize) -> String {
    let head = "service SvcStmt { fn only(key: Str) -> Str }\ncomponent Stmt provides c: SvcStmt {\n  provide c {\n    fn only(key) {\n";
    let tail = "      return key\n    }\n  }\n}\n";
    let mut body = String::new();
    let mut i = 0usize;
    while head.len() + body.len() + tail.len() < target_bytes {
        body.push_str(&format!("      let a{} = key\n", i));
        i += 1;
    }
    format!("{}{}{}", head, body, tail)
}

/// A candidate of roughly `target_bytes` whose bulk is COMMENT text: many
/// source bytes, few tokens, three declarations. The other half of the probe.
fn comment_padded(target_bytes: usize) -> String {
    let base = sized_candidate(3);
    let mut out = String::new();
    let mut i = 0usize;
    while out.len() + base.len() < target_bytes {
        out.push_str(&format!(
            "// padding line {}: lexed and discarded, declares nothing at all\n",
            i
        ));
        i += 1;
    }
    out.push_str(&base);
    out
}

struct Cell {
    label: &'static str,
    methods: usize,
    bytes: usize,
    stats: Stats,
}

/// A same-size, different-shape probe: three candidates of roughly equal BYTE
/// length whose token and declaration counts differ. It answers the question the
/// size curve alone cannot - whether the cost tracks source bytes (the lexer),
/// tokens (the parser), or declarations (the composition gate) - so the cost
/// finding is attributed rather than guessed at.
struct Shape {
    label: &'static str,
    bytes: usize,
    verdict: &'static str,
    stats: Stats,
}

const CELL_SIZES: [(&str, usize); 3] = [("small", 3), ("medium", 12), ("large", 48)];

/// The byte length all three shape probes are built to. Kept modest on purpose:
/// the screen is milliseconds-to-seconds at these sizes, so a larger probe would
/// price the harness out of CI without changing what it shows.
const SHAPE_BYTES: usize = 1200;

struct Cost {
    iters: usize,
    warmup: usize,
    cells: Vec<Cell>,
    shapes: Vec<Shape>,
    representative: Stats,
    representative_bytes: usize,
}

fn timed(source: &str, iters: usize, warmup: usize) -> Stats {
    for _ in 0..warmup {
        std::hint::black_box(admit(source).kind());
    }
    time_ms(source, iters)
}

fn measure(iters: usize, warmup: usize) -> Cost {
    let cells = CELL_SIZES
        .iter()
        .map(|(label, methods)| {
            let source = sized_candidate(*methods);
            Cell {
                label,
                methods: *methods,
                bytes: source.len(),
                stats: timed(&source, iters, warmup),
            }
        })
        .collect();

    // The shape probe runs fewer iterations than the size cells: it is a
    // question about ATTRIBUTION (which stage the cost lives in), and the
    // separation it looks for is orders of magnitude, not percent.
    let shape_iters = (iters / 4).max(3);
    let shape_sources: [(&'static str, String); 3] = [
        ("declaration-heavy", sized_candidate(24)),
        ("statement-heavy", statement_heavy(SHAPE_BYTES)),
        ("comment-padded", comment_padded(SHAPE_BYTES)),
    ];
    let shapes = shape_sources
        .iter()
        .map(|(label, source)| Shape {
            label,
            bytes: source.len(),
            verdict: admit(source).kind(),
            stats: timed(source, shape_iters, warmup.min(2)),
        })
        .collect();

    Cost {
        iters,
        warmup,
        cells,
        shapes,
        representative: timed(STANDALONE_TWIN, iters, warmup),
        representative_bytes: STANDALONE_TWIN.len(),
    }
}

// --------------------------------------------------------------------------- //
// Reporting.
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

fn stats_json(stats: &Stats) -> String {
    format!(
        "{{\"n\":{},\"min\":{:.6},\"median\":{:.6},\"p90\":{:.6},\"p99\":{:.6},\"mean\":{:.6}}}",
        stats.n, stats.min, stats.median, stats.p90, stats.p99, stats.mean
    )
}

/// The whole report as JSON, sources included, for
/// `tests/test_inprocess_gate_rust.py`. The driver re-derives the py verdict
/// for these exact bytes, so the two harnesses are held against each other on
/// the same programs rather than on two tables that can drift apart.
fn report_json(records: &[Record], cost: &Cost, order_drift: &[String], closed: &FailClosed) -> String {
    let version = gate_version();
    let arm_json = |arm: &Arm| {
        format!(
            "{{\"verdict\":{},\"code\":{},\"message\":{},\"wire\":{}}}",
            json_string(arm.kind),
            arm.code.as_deref().map(json_string).unwrap_or_else(|| "null".into()),
            arm.message.as_deref().map(json_string).unwrap_or_else(|| "null".into()),
            json_string(&arm.json),
        )
    };
    let candidates: Vec<String> = records
        .iter()
        .map(|r| {
            format!(
                "{{\"name\":{},\"note\":{},\"shared_with_py\":{},\"source\":{},\"verdict\":{},\"code\":{},\"message\":{},\"wire\":{},\"manifest\":{},\"into\":{}}}",
                json_string(r.name),
                json_string(r.note),
                r.shared_with_py,
                json_string(&r.source),
                json_string(r.kind),
                r.code.as_deref().map(json_string).unwrap_or_else(|| "null".into()),
                r.message.as_deref().map(json_string).unwrap_or_else(|| "null".into()),
                json_string(&r.json),
                r.into
                    .as_ref()
                    .map(|(manifest, _)| json_string(manifest))
                    .unwrap_or_else(|| "null".into()),
                r.into
                    .as_ref()
                    .map(|(_, arm)| arm_json(arm))
                    .unwrap_or_else(|| "null".into()),
            )
        })
        .collect();
    let cells: Vec<String> = cost
        .cells
        .iter()
        .map(|c| {
            format!(
                "{{\"label\":{},\"methods\":{},\"bytes\":{},\"stats\":{}}}",
                json_string(c.label),
                c.methods,
                c.bytes,
                stats_json(&c.stats)
            )
        })
        .collect();
    let shapes: Vec<String> = cost
        .shapes
        .iter()
        .map(|s| {
            format!(
                "{{\"label\":{},\"bytes\":{},\"verdict\":{},\"stats\":{}}}",
                json_string(s.label),
                s.bytes,
                json_string(s.verdict),
                stats_json(&s.stats)
            )
        })
        .collect();
    let drift: Vec<String> = order_drift.iter().map(|d| json_string(d)).collect();
    format!(
        "{{\"gate_version\":{{\"api\":{},\"language\":{},\"frontier\":{},\"layer\":{}}},\
\"held_manifest\":{},\
\"candidates\":[{}],\
\"cost\":{{\"iters\":{},\"warmup\":{},\"cells\":[{}],\"shapes\":[{}],\"representative\":{},\"representative_bytes\":{}}},\
\"order_drift\":[{}],\
\"fail_closed\":{{\"oversized_declined\":{},\"oversized_code\":{},\"compile_to_refuses_both_tiers\":{}}},\
\"max_source_bytes\":{}}}",
        json_string(version.api),
        json_string(version.language),
        json_string(version.frontier),
        json_string(version.layer),
        json_string(HELD_MANIFEST),
        candidates.join(","),
        cost.iters,
        cost.warmup,
        cells.join(","),
        shapes.join(","),
        stats_json(&cost.representative),
        cost.representative_bytes,
        drift.join(","),
        closed.oversized_declined,
        closed
            .oversized_code
            .as_deref()
            .map(json_string)
            .unwrap_or_else(|| "null".into()),
        closed.compile_to_refuses_both_tiers,
        MAX_SOURCE_BYTES,
    )
}

/// The arm, rendered for a human reader of the markdown report.
fn arm_cell(kind: &str, code: Option<&str>) -> String {
    match kind {
        "refused" => format!("refuse ({})", code.unwrap_or("?")),
        "no_objection" => "no objection".to_string(),
        _ => "declined (FRONTIER)".to_string(),
    }
}

fn verdict_cell(record: &Record) -> String {
    arm_cell(record.kind, record.code.as_deref())
}

/// The manifest arm's cell: `not asked` for a candidate screened standalone
/// only, and the arm's own verdict (never a bare "ok") otherwise.
fn into_cell(record: &Record) -> String {
    match &record.into {
        None => "not asked".to_string(),
        Some((_, arm)) => arm_cell(arm.kind, arm.code.as_deref()),
    }
}

fn render_markdown(records: &[Record], cost: &Cost, order_drift: &[String], closed: &FailClosed) -> String {
    let version = gate_version();
    let rows: String = records
        .iter()
        .map(|r| {
            format!(
                "| `{}` | {} | {} | {} | {} |\n",
                r.name,
                verdict_cell(r),
                into_cell(r),
                if r.shared_with_py { "yes" } else { "no" },
                r.note
            )
        })
        .collect();
    let cell_rows: String = cost
        .cells
        .iter()
        .map(|c| {
            format!(
                "| {} ({} methods) | {} | {:.3} | {:.3} | {:.3} | {} |\n",
                c.label, c.methods, c.bytes, c.stats.median, c.stats.p90, c.stats.p99, c.stats.n
            )
        })
        .collect();
    let shape_rows: String = cost
        .shapes
        .iter()
        .map(|s| {
            format!(
                "| {} | {} | {} | {:.3} | {} |\n",
                s.label, s.bytes, s.verdict, s.stats.median, s.stats.n
            )
        })
        .collect();
    let refused = records.iter().filter(|r| r.kind == "refused").count();
    let no_objection = records.iter().filter(|r| r.kind == "no_objection").count();
    let declined = records.iter().filter(|r| r.kind == "outside_frontier").count();
    let rep = &cost.representative;
    let small = cost.cells.first().expect("a small cell");
    let large = cost.cells.last().expect("a large cell");
    let byte_growth = large.bytes as f64 / small.bytes as f64;
    let time_growth = large.stats.median / small.stats.median;
    let shape_median = |label: &str| {
        cost.shapes
            .iter()
            .find(|s| s.label == label)
            .map(|s| s.stats.median)
            .unwrap_or(f64::NAN)
    };
    let comment_speedup =
        shape_median("declaration-heavy") / shape_median("comment-padded");

    format!(
        r#"# In-process admission gate (item 333, Slice 2, rust)

An agent tool-generation loop written in rust links the revl gate as a LIBRARY
and screens every component it proposes IN ITS OWN PROCESS - no `revl mcp
serve`, no IPC, no wire, and no Python anywhere. This file records what that
buys and, just as load-bearing, what it does not. Produced by
`bench/inprocess_gate_rust` (`cargo run --release --manifest-path
bench/inprocess_gate_rust/Cargo.toml -- --write`).

Gate surface: `api={api}`, `language={language}`, `frontier={frontier}`.
Layer decided: {layer}.

## This gate issues no admissions - read this before wiring it in

The py harness (`bench/results/inprocess-gate.md`) proves an IDENTITY:
`revl.gate.admit` IS the reference admission path, so the in-process verdict IS
the reference verdict. **This harness cannot and does not claim that.**
`revl-gate` is the self-host front end compiled to rust; it decides the
composition/guarantee layer and runs NO type layer, so it has no admission arm
at all. Its three verdicts are `refused`, `no_objection` and `outside_frontier`,
and the wire reports `"admitted": false` on every one of them.

What the rust embed buys is the other direction: a local, Python-free REFUSAL
that agrees with the reference compiler on the covered corpus. A refusal is
worth acting on. A no-objection is NOT an admission - before running anything,
get a reference verdict (`revl compile`, or `revl.gate.admit` on py).

## The manifest arm (issue #346): half of it closed here

`revl_gate::admit_into(source, manifest)` is real as of issue #346: it folds
G2/G3 over the UNION of the running composition's rows and the candidate, so a
rust embed can ask the py loop's admitting question locally. It is a REFUSAL
arm, not an admission arm - it still reports `"admitted": false`, and it refuses
what the py gate refuses, with the same code and the same why-trace.

What that closes and what it does not, against `held_manifest` =
`{held}` (the py harness's `base_manifest()` wire):

* **closed** - the ambient half of G2/G3: a candidate whose provides collide
  with a RUNNING key is refused. `ambient_provision_conflict` below is the exit
  case: py's `revl.gate.admit_into` says `G2`, this gate says `G2` with the same
  sentence, and py's STANDALONE `admit` ADMITS the same bytes - so the manifest
  is demonstrably being read, not ignored.
* **still open** - requirement RESOLUTION. py's `admit_into` resolves a
  candidate's `requires` against the running providers; the self-host fold only
  checks disjointness, acyclicity and route realms, so it cannot. `cache_layer`
  is that half made concrete: py ADMITS it into the running manifest, this gate
  can only decline to object. Closing it is the self-host TYPE LAYER's job, its
  own roadmap lane, and is deliberately NOT attempted here.

## The batch, screened in-process

| candidate | standalone | into the held manifest | shared with the py harness | note |
|---|---|---|---|---|
{rows}
{refused} refused, {no_objection} no-objection, {declined} declined. Every one of them
serialises as `"admitted": false`; nothing in this batch produced anything a
host could read as an admission, and every refusal it did issue is a refusal the
py admission gate also issues, with the same guarantee tag
(`tests/test_inprocess_gate_rust.py`).

Verdicts are order-independent: screening the batch in a fixed order and in a
shuffled order in the same process yields identical per-candidate verdicts on
BOTH arms ({order}), which is the property that proves the gate is stateless.

### What the screen catches, measured

Seven of these candidates are ones the py admission gate REFUSES. This gate
refuses **two** of them: the `G2` provision conflict and the `G4` undeclared
emission. The other five come back as no-objections:

* `cache_layer` - a `requires store: Store` that resolves to nothing (py refuses
  it standalone, and ADMITS it into the running composition);
* `incomplete_provide` - a `provide` block missing a declared method;
* `syntax_error` - a source the reference parser rejects outright. The crate
  documents parse failures as `BAD`, and that arm is real (`@@@ not revl @@@`
  gets it), but the self-host parser is more permissive than the reference and
  this program walks through it;
* `hole_draft` - a draft with an open typed hole (py refuses `T3`);
* `type_layer_miss` - a return-type mismatch (py refuses `T1`).

Every one of those five is in the TOLERATED direction: a no-objection is never
an admission, so none of them is a false admit. Together they are the reason the
crate has no `Admitted` arm, and the reason an embedder that reads a
no-objection as a green ships an unsafe host. The one candidate this gate
declines outright (`frontier_oversized`) is the fail-closed path working: `py`
ADMITS it, and rather than decide a construct it does not cover, the gate says
so.

### The `admit_into` gap, priced - and one leg of it closed

Before #346 the realistic agent shape - admit a candidate AGAINST the running
composition - was not available on rust at all. It now is for the ambient half:
`ambient_provision_conflict` is a candidate that is HARMLESS standalone (py
ADMITS the same bytes) and is refused `G2` once the running composition is in
the fold, on both arms, with the same why-trace. That is the half that closed.

The other half is what `cache_layer` still prices. py's `admit_into` ADMITS it
into the running manifest - it RESOLVES the `requires store: Store` against the
running `Kv` provider - and the only thing this gate can say about it, with or
without the manifest, is that it does not object. Requirement resolution is the
self-host type layer's job, and this file is where the remaining distance is
measured, not smoothed over.

## Fail closed

* an oversized source (over `MAX_SOURCE_BYTES` = {max_bytes}) is DECLINED
  ({oversized}) rather than risked: the emitted front end is deeply recursive and
  a stack exhaustion aborts, which cannot be turned back into a refusal;
* the same source, as a batch CANDIDATE (`frontier_oversized` above), is declined
  with code `FRONTIER` — the batch's fail-closed arm. The generated frontier
  table's two LEXICAL rows (excluded keywords, excluded builtins) are both empty
  at this generation: item 391 ported the last builtins they named, so the size
  bound is the trigger a batch can still demonstrate;
* `compile_to` refuses on both tiers ({compile_to}) - the self-host emitters
  still carry `@py`-only helper externs, so there is no native emitter to call
  (item 332 Stage 4).

## Cost: milliseconds, and super-linear in candidate size

The timed section is one `revl_gate::admit` call on an in-memory string: no disk
I/O, no network, no toolchain, no Python, no process hop. Nothing else is in it.

| candidate size | bytes | median (ms) | p90 (ms) | p99 (ms) | samples |
|---|---|---|---|---|---|
{cell_rows}
The representative scenario (the py harness's `standalone_twin`, {rep_bytes} B)
measured median **{rep_median:.3} ms** (p90 {rep_p90:.3} ms, p99 {rep_p99:.3} ms,
n={rep_n}).

**This does not inherit the py headline, and it must not be reported as if it
did.** The py in-process round-trip is tenths of a millisecond and grows roughly
with candidate size. This one starts in the milliseconds and grows far faster
than the source does: {byte_growth:.1}x the bytes costs {time_growth:.0}x the time
across the size cells, which is quadratic-shaped, not linear. At a few kilobytes
- an ordinary model-authored component - a single screen costs on the order of a
second. An agent loop that screens every candidate inline would feel that.

### Where the cost lives

Three candidates of roughly equal BYTE length and very different token and
declaration counts, timed the same way. If the cost tracked source bytes it
would be flat across these rows; it is not.

| shape | bytes | verdict | median (ms) | samples |
|---|---|---|---|---|
{shape_rows}
The comment-padded shape - the same byte count, a fraction of the tokens - is
roughly {comment_speedup:.0}x cheaper than the declaration-heavy one, while the
statement-heavy shape, which carries ONE declaration and a body full of
statements, costs the same order as the declaration-heavy one. So the cost
tracks TOKENS: it lives in the emitted lexer/parser, not in the composition gate
walking declarations, and not in raw source bytes. That is an attribution, not a
fix - the fix belongs to whoever owns the emitted self-host front end's
algorithmic shape (items 391/336), and this file is the measurement they should
start from.

### Methodology, and how much to trust the absolute figures

- **Build:** release. A debug build's numbers are a fiction, so the harness
  documents the release invocation and nothing else.
- **Platform:** {os}/{arch}. Sampling: `--iters {iters}` per size cell with
  `--warmup {warmup}`, and a quarter of that per shape probe (the shape question
  is orders of magnitude, not percent).
- **Timed section:** one `revl_gate::admit` call on an in-memory string.
- **Variance:** these medians move by a FACTOR of a few between runs on a shared
  machine - the p90/p99 columns show it. So read the SHAPE of the result, which
  is stable and is the finding: milliseconds at the floor, super-linear in
  candidate size, token-bound. Do not quote a single figure from this table as
  "the rust gate's latency".

## The honest boundary

This is a COMPILE-TIME screen, not a sandbox. A component this gate refuses
never runs in the embedder's process. A component it does not refuse has been
screened at the composition/guarantee layer ONLY: not type-checked, not
admitted, not confined. `admitted` is not something this gate issues - not
standalone and not from the manifest arm, which is a refusal arm that reads the
running composition, not an admission into it. And even a py admission is not
"safe to run unwitnessed" - the reversible-run half is item 334.

## Re-run

```
cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml
cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml -- --write
```

Timings above come from `--iters {iters} --warmup {warmup}` in a release build; a
debug build's numbers are a fiction and the harness says so rather than
publishing them.

A guard test, `tests/test_inprocess_gate_rust.py`, builds and runs this harness
in CI (the `backend-rust` job), re-derives the PY verdict for each candidate's
exact source bytes - both standalone and against the held manifest - and holds
the two harnesses against each other: every rust refusal must be a real py
refusal with the same code, every rust `admit_into` refusal must be a real
`revl.gate.admit_into` refusal with the same code AND the same why-trace, no arm
may read as an admission, the measured layer gap must still be a gap, and the py
harness's own candidates must still be the bytes screened here.
"#,
        api = version.api,
        language = version.language,
        frontier = version.frontier,
        layer = version.layer,
        held = HELD_MANIFEST,
        rows = rows,
        refused = refused,
        no_objection = no_objection,
        declined = declined,
        order = if order_drift.is_empty() { "holds" } else { "FAILED" },
        max_bytes = MAX_SOURCE_BYTES,
        oversized = if closed.oversized_declined { "holds" } else { "FAILED" },
        compile_to = if closed.compile_to_refuses_both_tiers { "holds" } else { "FAILED" },
        cell_rows = cell_rows,
        shape_rows = shape_rows,
        rep_bytes = cost.representative_bytes,
        rep_median = rep.median,
        rep_p90 = rep.p90,
        rep_p99 = rep.p99,
        rep_n = rep.n,
        byte_growth = byte_growth,
        time_growth = time_growth,
        comment_speedup = comment_speedup,
        iters = cost.iters,
        warmup = cost.warmup,
        os = std::env::consts::OS,
        arch = std::env::consts::ARCH,
    )
}

// --------------------------------------------------------------------------- //
// CLI.
// --------------------------------------------------------------------------- //

fn main() -> ExitCode {
    // A screen costs milliseconds here, and seconds at a few kilobytes (see
    // the cost section), so the default sample count is small on purpose: this
    // is a deterministic CPU-bound call whose variance is tiny, and a py-sized
    // 2000-iteration default would price the harness out of a CI job.
    let mut iters = 25usize;
    let mut warmup = 3usize;
    let mut as_json = false;
    let mut write = false;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--iters" => {
                iters = args
                    .next()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or_else(|| panic!("--iters needs a positive integer"));
            }
            "--warmup" => {
                warmup = args
                    .next()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or_else(|| panic!("--warmup needs an integer"));
            }
            "--json" => as_json = true,
            "--write" => write = true,
            "--help" | "-h" => {
                println!(
                    "usage: inprocess_gate_rust [--iters N] [--warmup N] [--json] [--write]\n\
                     \n\
                     the rust twin of bench/inprocess_gate_harness.py: screens a batch of\n\
                     proposed components in-process through revl_gate::admit, screens the\n\
                     manifest-aware candidates again through revl_gate::admit_into against\n\
                     the held composition (issue #346), checks the invariants a host may\n\
                     rely on, and measures the standalone round-trip."
                );
                return ExitCode::SUCCESS;
            }
            other => {
                eprintln!("unknown argument: {}", other);
                return ExitCode::FAILURE;
            }
        }
    }
    assert!(iters > 0, "--iters must be positive");

    let candidates = batch();
    let records = screen_batch(&candidates);
    let order_drift = order_dependence(&candidates);
    let closed = fail_closed();
    let cost = measure(iters, warmup);

    let admission = admission_offenders(&records);
    let shape = shape_offenders(&records);
    let ok = admission.is_empty()
        && shape.is_empty()
        && order_drift.is_empty()
        && closed.oversized_declined
        && closed.compile_to_refuses_both_tiers;

    if as_json {
        println!("{}", report_json(&records, &cost, &order_drift, &closed));
        return if ok { ExitCode::SUCCESS } else { ExitCode::FAILURE };
    }

    let version = gate_version();
    println!(
        "gate surface: api={} language={} frontier={}",
        version.api, version.language, version.frontier
    );
    println!("layer decided: {}", version.layer);
    println!("held manifest: {}", HELD_MANIFEST);
    println!("\nin-process screen (revl_gate::admit, no subprocess, no IPC, no Python):");
    for r in &records {
        println!(
            "  [{}] {:20} {:22} {}",
            if r.kind == "refused" { "refuse" } else { "     -" },
            r.name,
            verdict_cell(r),
            r.note
        );
    }
    println!("\nagainst the held manifest (revl_gate::admit_into, issue #346):");
    for r in &records {
        match &r.into {
            None => println!("  [     -] {:20} not asked", r.name),
            Some((_, arm)) => println!(
                "  [{}] {:20} {}",
                if arm.kind == "refused" { "refuse" } else { "     -" },
                r.name,
                arm_cell(arm.kind, arm.code.as_deref())
            ),
        }
    }
    println!(
        "\nno arm reads as an admission: {}",
        if admission.is_empty() { "holds" } else { "FAILED" }
    );
    for offender in &admission {
        println!("  OFFENDER {}", offender);
    }
    println!(
        "wire shape (code + why-trace on every non-no-objection): {}",
        if shape.is_empty() { "holds" } else { "FAILED" }
    );
    for offender in &shape {
        println!("  OFFENDER {}", offender);
    }
    println!(
        "order-independence (fixed vs shuffled batch order): {}",
        if order_drift.is_empty() { "holds" } else { "FAILED" }
    );
    for drifted in &order_drift {
        println!("  DRIFT {}", drifted);
    }
    println!(
        "fail closed: oversized declined={} compile_to refuses both tiers={}",
        closed.oversized_declined, closed.compile_to_refuses_both_tiers
    );

    println!("\ncost distribution (in-process round-trip):");
    for c in &cost.cells {
        println!(
            "  {:6} candidate ({:2} methods, {:5} B) : median {:.4} ms  p90 {:.4} ms  p99 {:.4} ms  (n={})",
            c.label, c.methods, c.bytes, c.stats.median, c.stats.p90, c.stats.p99, c.stats.n
        );
    }
    println!(
        "  representative (standalone_twin)      : median {:.4} ms  p90 {:.4} ms  (n={})",
        cost.representative.median, cost.representative.p90, cost.representative.n
    );

    println!("\nwhere the cost lives (same bytes, different shape):");
    for shape in &cost.shapes {
        println!(
            "  {:18} ({:5} B, {:16}) : median {:.4} ms  (n={})",
            shape.label, shape.bytes, shape.verdict, shape.stats.median, shape.stats.n
        );
    }

    if write {
        let out = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("results")
            .join("inprocess-gate-rust.md");
        std::fs::write(&out, render_markdown(&records, &cost, &order_drift, &closed))
            .expect("write the report");
        println!("\nwrote {}", out.display());
    }

    println!(
        "\n{}: no admission issued + wire shape + order-independence + fail-closed",
        if ok { "PASS" } else { "FAIL" }
    );
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
