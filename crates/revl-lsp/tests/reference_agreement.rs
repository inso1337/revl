//! The item-336 exit test: the native binary's `publishDiagnostics`, `hover`
//! and `definition` are byte-identical to `python -m revl.lsp` over a corpus.
//!
//! The corpus is the load-bearing part. Design A1 (the CRITICAL) says a binary
//! whose diagnostics come from the NATIVE self-host front end silently returns
//! no diagnostic where the reference refuses, because the self-host covers only
//! the conformance `revl` frontier — and that an in-frontier corpus would green
//! exactly that broken binary. So the corpus below is deliberately dominated by
//! constructs OUTSIDE the frontier: component activation bodies, `provide`
//! methods, `effect`/`undo` acquisition, service linking and provision, async
//! reach, and lifecycle scripts. Conformance records those as the `lim` rows
//! ("most component and `fn`-body constructs read `lim` while the function/type
//! surface reads `ok`", `docs/conformance.md`), i.e. exactly where a native-only
//! binary either declines the construct with a different message or has no
//! check at all.
//!
//! The tests fail in different directions on purpose:
//!
//!   * `binary_matches_the_reference_byte_for_byte` compares whole reply
//!     streams. A native engine that answered differently fails here.
//!   * `off_frontier_documents_still_get_their_squiggles` asserts the binary
//!     shows the exact refusal code the reference shows on each off-frontier
//!     document. A native engine that answered NOTHING (the silent
//!     false-admit, the dangerous direction) fails here, loudly.
//!   * `the_binary_never_shows_fewer_squiggles_than_the_reference` is the
//!     slice-2 release blocker as an executable check: every diagnostic the
//!     reference publishes is present in the binary's publish, byte for byte.
//!     Zero rows in the fewer-than-reference direction, ever.
//!   * `native_navigation_answers_and_never_disagrees` isolates the NATIVE
//!     navigation answers (`REVL_LSP_NATIVE_ONLY`, which turns off the
//!     reference fallback) and asserts each is either the reference's answer or
//!     null — never a third thing. It also asserts the native path actually
//!     answers a substantial share of the corpus, so "agrees" cannot be bought
//!     by answering nothing.

use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};

use serde_json::{json, Value};

/// A corpus document: the repo file it comes from, the refusal the reference
/// raises on it (`None` for a document that compiles clean), and why it sits
/// outside the self-host frontier.
struct Doc {
    name: &'static str,
    path: &'static str,
    code: Option<&'static str>,
    outside_frontier: &'static str,
}

const CORPUS: &[Doc] = &[
    Doc {
        name: "g1_undeclared_access",
        path: "examples/rejections/g1_undeclared_access.rvl",
        code: Some("G1"),
        outside_frontier: "declared-access over a component `requires` clause",
    },
    Doc {
        name: "g2_provision_conflict",
        path: "examples/rejections/g2_provision_conflict.rvl",
        code: Some("G2"),
        outside_frontier: "provision disjointness across two components (linking)",
    },
    Doc {
        name: "g3_dependency_cycle",
        path: "examples/rejections/g3_dependency_cycle.rvl",
        code: Some("G3"),
        outside_frontier: "a service dependency cycle (linking)",
    },
    Doc {
        name: "g4_missing_undo",
        path: "examples/rejections/g4_missing_undo.rvl",
        code: Some("G4"),
        outside_frontier: "an `effect` acquisition with no inverse, in an activation body",
    },
    Doc {
        name: "g4_unmarked_emission",
        path: "examples/rejections/g4_unmarked_emission.rvl",
        code: Some("G4"),
        outside_frontier: "emission marking on a service operation",
    },
    Doc {
        name: "g6_impure_statement",
        path: "examples/rejections/g6_impure_statement.rvl",
        code: Some("G6"),
        outside_frontier: "purity outside effect forms, in a component body",
    },
    Doc {
        name: "g7_verified_recursion",
        path: "examples/rejections/v2_verified_direct_recursion.rvl",
        code: Some("G7"),
        outside_frontier: "totality of a `verified fn`",
    },
    Doc {
        name: "g8_extern_unclassified",
        path: "examples/rejections/v2_extern_unclassified.rvl",
        code: Some("G8"),
        outside_frontier: "boundary classification of an `extern` declaration",
    },
    Doc {
        name: "a1_async_effect_not_awaited",
        path: "examples/rejections/a1_async_effect_not_awaited.rvl",
        code: Some("A1"),
        outside_frontier: "async reach through a required key in an acquisition",
    },
    Doc {
        name: "a9_provide_key_not_declared",
        path: "examples/rejections/a9_provide_key_not_declared.rvl",
        code: Some("A9"),
        outside_frontier: "a `provide` block against an undeclared key",
    },
    Doc {
        name: "t1_missing_return",
        path: "examples/rejections/t8_missing_return.rvl",
        code: Some("T1"),
        outside_frontier: "return-path typing inside a fn body",
    },
    Doc {
        name: "t1_match_nonexhaustive",
        path: "examples/rejections/v2_match_nonexhaustive.rvl",
        code: Some("T1"),
        outside_frontier: "match exhaustiveness over a sum type",
    },
    Doc {
        name: "clean_counter_pair",
        path: "examples/counter_pair.rvl",
        code: None,
        outside_frontier: "a clean two-component composition (the empty-diagnostics case)",
    },
    Doc {
        name: "clean_heartbeat",
        path: "examples/heartbeat.rvl",
        code: None,
        outside_frontier: "a clean timer component (the empty-diagnostics case)",
    },
    Doc {
        name: "clean_user_cache",
        path: "examples/user_cache.rvl",
        code: None,
        outside_frontier: "a clean cache composition with externs",
    },
];

/// Documents that exercise the protocol's edges rather than the language's,
/// plus the CLEAN in-frontier declarations slice 2's native navigation is
/// allowed to answer. The clean ones are load-bearing in the other direction
/// from `CORPUS`: `CORPUS` proves the native path cannot hide a squiggle, and
/// these prove it actually answers rather than deferring everything.
const INLINE: &[(&str, &str)] = &[
    ("empty", ""),
    ("syntax_error", "fn broken( {\n"),
    (
        "in_frontier_g1",
        "fn add(a: Int, b: Int) -> Int {\n  return a + c\n}\n",
    ),
    // every declaration kind the native table carries, with the parameter and
    // return spellings (`Opt[T]` sugar, a type application) a native signature
    // has to reproduce exactly
    (
        "clean_declarations",
        // ordered so the probe budget below reaches each declared NAME, not
        // just the keywords that precede it
        "extern pure fn parse_port(raw: Str) -> Int = @py { return int(raw) }\n\
         extern emission async fn publish(topic: Str, payload: Map[Str, Int]) = @py { pass }\n\
         fn pick(rows: List[Str], fallback: Str?) -> Str {\n  return fallback ?? rows[0]\n}\n\
         service Clock {\n  fn now() -> Int\n}\n\
         fn describe(port: Int) -> Str {\n  return \"port\"\n}\n",
    ),
    // a top-level name that is ALSO a parameter: the reference resolves the
    // innermost scope, and the native table cannot see scopes, so it must
    // refuse the name outright rather than jump to the declaration
    (
        "clean_shadowed_name",
        "fn total(total: Int) -> Int {\n  return total\n}\n",
    ),
    // a `type` declaration the self-host parser skips entirely, sharing a name
    // with a `fn` it does parse: the reference resolves the type, so the native
    // table must drop the name
    (
        "clean_type_shadows_fn",
        "type Row = { id: Int }\n\nfn Row() -> Int {\n  return 1\n}\n",
    ),
];

// ------------------------------------------------------------------ the tests

#[test]
fn binary_matches_the_reference_byte_for_byte() {
    let Some(context) = Context::new() else {
        return;
    };
    let session = build_session(&context);
    let input = frame_all(&session);

    let native = context.run_binary(&input);
    let reference = context.run_reference(&input);

    if native != reference {
        panic!(
            "the binary and `python -m revl.lsp` disagree.\nfirst divergence:\n{}",
            first_divergence(&native, &reference)
        );
    }
    // Two streams of nulls would "agree" trivially, so assert the compared
    // stream actually carries the three answers the item names.
    let replies = parse_stream(&native);
    let hovers = replies
        .iter()
        .filter(|m| m["result"].get("contents").is_some())
        .count();
    let definitions = replies
        .iter()
        .filter(|m| m["result"].get("uri").is_some())
        .count();
    let squiggles: usize = replies
        .iter()
        .filter(|m| m.get("method") == Some(&json!("textDocument/publishDiagnostics")))
        .map(|m| m["params"]["diagnostics"].as_array().map_or(0, Vec::len))
        .sum();
    assert!(
        hovers >= 20 && definitions >= 20 && squiggles >= CORPUS.len() / 2,
        "the compared stream is too thin to prove anything: \
         {hovers} hovers, {definitions} definitions, {squiggles} diagnostics \
         in {} bytes",
        native.len()
    );
    // the reference's explain-hover text carries an em dash, and CPython's
    // json.dumps escapes it; a serializer that emitted raw UTF-8 would diverge
    assert!(
        find(&native, b"\\u2014").is_some(),
        "no escaped non-ASCII in the stream — the encoding path went untested"
    );
}

#[test]
fn off_frontier_documents_still_get_their_squiggles() {
    let Some(context) = Context::new() else {
        return;
    };
    let mut messages = vec![request(1, "initialize", json!({}))];
    for doc in CORPUS {
        messages.push(did_open(&uri_for(doc.name), &context.source(doc.path)));
    }
    messages.push(notification("exit", json!({})));

    let replies = parse_stream(&context.run_binary(&frame_all(&messages)));
    let published: Vec<&Value> = replies
        .iter()
        .filter(|m| m.get("method") == Some(&json!("textDocument/publishDiagnostics")))
        .collect();
    assert_eq!(published.len(), CORPUS.len());

    for (doc, message) in CORPUS.iter().zip(published) {
        let diagnostics = message["params"]["diagnostics"].as_array().unwrap();
        match doc.code {
            None => assert!(
                diagnostics.is_empty(),
                "{} should compile clean, got {diagnostics:?}",
                doc.name
            ),
            Some(code) => {
                assert!(
                    !diagnostics.is_empty(),
                    "{} ({}) got NO diagnostic — the missing-squiggle direction, \
                     which is what a native-only engine would produce off the frontier",
                    doc.name,
                    doc.outside_frontier
                );
                assert_eq!(
                    diagnostics[0]["code"], code,
                    "{} reported the wrong refusal",
                    doc.name
                );
                assert_eq!(diagnostics[0]["source"], "revl");
            }
        }
    }
}

#[test]
fn the_gate_version_is_reachable_for_a_skew_check() {
    let Some(context) = Context::new() else {
        return;
    };
    let messages = vec![
        request(1, "revl/gateVersion", json!({})),
        notification("exit", json!({})),
    ];
    let replies = parse_stream(&context.run_binary(&frame_all(&messages)));
    let version = &replies[0]["result"];
    assert_eq!(version["api"], "1");
    // diagnostics — the answers a green depends on — are still the reference's
    // over the whole language, so the binary's own frontier stays "reference"
    assert_eq!(version["frontier"], "reference");
    assert_eq!(version["engine"], "reference-diagnostics + native-navigation");
    assert!(version["language"].is_string(), "{version}");
    // the native engine's pin, which is what a stale-binary audit compares
    let native = &version["native"];
    assert!(
        native["frontier"].as_str().is_some_and(|id| id.starts_with("selfhost-admit:")),
        "{native}"
    );
    assert_eq!(
        native["answers"],
        json!(["textDocument/definition", "textDocument/hover (symbol)"])
    );
    assert!(native["layer"].as_str().is_some_and(|l| l.contains("NOT the reference type layer")),
            "the native pin must say what it does NOT decide: {native}");
}

/// The missing-squiggle rule, as a test. The binary may show MORE than
/// `python -m revl.lsp`; it may never show fewer, and a native result with
/// fewer diagnostics than the reference on the same input is release-blocking.
#[test]
fn the_binary_never_shows_fewer_squiggles_than_the_reference() {
    let Some(context) = Context::new() else {
        return;
    };
    let mut messages = vec![request(1, "initialize", json!({}))];
    for (name, source) in context.documents() {
        messages.push(did_open(&uri_for(&name), &source));
    }
    messages.push(notification("exit", json!({})));
    let input = frame_all(&messages);

    let native = published_diagnostics(&context.run_binary(&input));
    let reference = published_diagnostics(&context.run_reference(&input));
    assert_eq!(native.len(), reference.len(), "the two servers published different documents");

    let mut compared = 0;
    let mut added = 0;
    for ((uri, mine), (_, theirs)) in native.iter().zip(reference.iter()) {
        for diagnostic in theirs {
            assert!(
                mine.contains(diagnostic),
                "{uri} LOST a reference diagnostic — the missing-squiggle direction, \
                 which is release-blocking: {diagnostic}"
            );
            compared += 1;
        }
        added += mine
            .iter()
            .filter(|d| d["source"] == json!("revl-native"))
            .count();
    }
    assert!(compared >= CORPUS.len() / 2,
            "only {compared} reference diagnostics compared — too thin to prove anything");
    // The other half of agree-or-add: on the covered corpus the native gate and
    // the reference agree, so the ADD path stays silent and byte-identity
    // holds. A non-zero count here is not unsound, but it is a divergence worth
    // seeing rather than absorbing.
    assert_eq!(added, 0, "the native gate added {added} diagnostic(s) on the corpus");
}

/// The slice-2 navigation soundness exit test: definition and symbol-hover on
/// the native parser return either the reference's answer or nothing, never a
/// wrong one.
#[test]
fn native_navigation_answers_and_never_disagrees() {
    let Some(context) = Context::new() else {
        return;
    };
    let session = build_session(&context);
    let input = frame_all(&session);

    let reference = by_id(&parse_stream(&context.run_reference(&input)));
    let native = by_id(&parse_stream(&context.run_binary_native_only(&input)));

    let mut answered = 0;
    let mut deferred = 0;
    for (id, mine) in &native {
        let theirs = &reference
            .iter()
            .find(|(other, _)| other == id)
            .expect("the reference answered every request")
            .1;
        if mine.is_null() {
            deferred += 1;
            continue;
        }
        assert_eq!(
            mine, theirs,
            "request {id}: the native navigation answer is not the reference's"
        );
        answered += 1;
    }
    assert!(
        answered >= 40,
        "the native path answered only {answered} of {} navigation requests \
         ({deferred} deferred) — agreement bought by silence proves nothing",
        answered + deferred
    );

    // ... and it answered every declaration kind the native table carries,
    // signature and all. Without this the count above could be met by
    // `service`/`component` hovers alone, leaving the native signature reader
    // (parameters, `Opt[T]` sugar, a type application, `extern` flag order)
    // unproven against the reference's spelling.
    let hovers: Vec<&str> = native
        .iter()
        .filter_map(|(_, value)| value.get("contents")?.get("value")?.as_str())
        .collect();
    for expected in [
        "```revl\nservice Counter\n```",
        "```revl\ncomponent Heartbeat\n```",
        "```revl\nfn pick(rows: List[Str], fallback: Opt[Str]) -> Str\n```",
        "```revl\nextern pure fn parse_port(raw: Str) -> Int\n```",
        "```revl\nextern emission async fn publish(topic: Str, payload: Map[Str, Int])\n```",
    ] {
        assert!(
            hovers.contains(&expected),
            "the native path never answered {expected:?}; it answered {hovers:?}"
        );
    }
}

/// Every `publishDiagnostics` in a stream as `(uri, diagnostics)`.
fn published_diagnostics(bytes: &[u8]) -> Vec<(String, Vec<Value>)> {
    parse_stream(bytes)
        .into_iter()
        .filter(|m| m.get("method") == Some(&json!("textDocument/publishDiagnostics")))
        .map(|m| {
            (
                m["params"]["uri"].as_str().unwrap_or_default().to_string(),
                m["params"]["diagnostics"].as_array().cloned().unwrap_or_default(),
            )
        })
        .collect()
}

/// The `result` of every hover/definition response in a stream, keyed by id.
fn by_id(messages: &[Value]) -> Vec<(i64, Value)> {
    messages
        .iter()
        .filter_map(|m| Some((m.get("id")?.as_i64()?, m.get("result")?.clone())))
        .collect()
}

// ------------------------------------------------------------------ session

fn build_session(context: &Context) -> Vec<Value> {
    let mut messages = vec![
        request(1, "initialize", json!({"processId": Value::Null})),
        notification("initialized", json!({})),
    ];
    let mut id = 100;
    for (name, source) in context.documents() {
        let uri = uri_for(&name);
        messages.push(did_open(&uri, &source));
        for (line, character) in sample_positions(&source) {
            messages.push(request(
                id,
                "textDocument/hover",
                json!({"textDocument": {"uri": uri},
                       "position": {"line": line, "character": character}}),
            ));
            messages.push(request(
                id + 1,
                "textDocument/definition",
                json!({"textDocument": {"uri": uri},
                       "position": {"line": line, "character": character}}),
            ));
            id += 2;
        }
        messages.push(request(
            id,
            "textDocument/codeAction",
            json!({"textDocument": {"uri": uri},
                   "range": {"start": {"line": 0, "character": 0},
                             "end": {"line": 200, "character": 0}}}),
        ));
        id += 1;
        // an edit: full-document sync, then the same probe again
        let edited = format!("{source}\n// edited\n");
        messages.push(notification(
            "textDocument/didChange",
            json!({"textDocument": {"uri": uri},
                   "contentChanges": [{"text": edited}]}),
        ));
        messages.push(request(
            id,
            "textDocument/hover",
            json!({"textDocument": {"uri": uri},
                   "position": {"line": 0, "character": 3}}),
        ));
        id += 1;
        messages.push(notification(
            "textDocument/didClose",
            json!({"textDocument": {"uri": uri}}),
        ));
        // a request against a closed document, and one nobody implements
        messages.push(request(
            id,
            "textDocument/hover",
            json!({"textDocument": {"uri": uri},
                   "position": {"line": 0, "character": 0}}),
        ));
        id += 1;
    }
    messages.push(request(id, "textDocument/rename", json!({})));
    messages.push(request(id + 1, "shutdown", json!({})));
    messages.push(notification("exit", json!({})));
    messages
}

/// Up to sixteen probe positions per document: the first five identifiers of
/// each code line, which on a revl declaration line runs from the keywords
/// through the declared name into its first parameter and type — so the probes
/// land on real symbols (hover and definition resolve something) as well as on
/// keywords (they resolve nothing). Five per line rather than three because an
/// `extern emission async fn publish(...)` puts the NAME fifth, and a probe
/// budget that never reached it would leave the native signature reader
/// untested. Deterministic, so both servers see exactly the same requests.
fn sample_positions(source: &str) -> Vec<(usize, usize)> {
    let mut positions = Vec::new();
    for (index, line) in source.split('\n').enumerate() {
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with("//") {
            continue;
        }
        for start in identifier_spans(line).into_iter().take(5) {
            positions.push((index, start));
        }
        if positions.len() >= 16 {
            break;
        }
    }
    positions.truncate(16);
    positions
}

fn identifier_spans(line: &str) -> Vec<usize> {
    let bytes: Vec<char> = line.chars().collect();
    let word = |c: char| c.is_ascii_alphanumeric() || c == '_';
    let mut starts = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        if word(bytes[index]) && (index == 0 || !word(bytes[index - 1])) {
            starts.push(index);
        }
        index += 1;
    }
    starts
}

fn uri_for(name: &str) -> String {
    format!("file:///corpus/{name}.rvl")
}

fn did_open(uri: &str, text: &str) -> Value {
    notification(
        "textDocument/didOpen",
        json!({"textDocument": {"uri": uri, "languageId": "revl",
                                "version": 1, "text": text}}),
    )
}

fn request(id: i64, method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
}

fn notification(method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "method": method, "params": params})
}

fn frame_all(messages: &[Value]) -> Vec<u8> {
    let mut out = Vec::new();
    for message in messages {
        let body = serde_json::to_vec(message).unwrap();
        out.extend_from_slice(format!("Content-Length: {}\r\n\r\n", body.len()).as_bytes());
        out.extend_from_slice(&body);
    }
    out
}

fn parse_stream(bytes: &[u8]) -> Vec<Value> {
    let mut messages = Vec::new();
    let mut rest = bytes;
    while let Some(split) = find(rest, b"\r\n\r\n") {
        let header = String::from_utf8_lossy(&rest[..split]).to_string();
        let length: usize = header
            .rsplit("Content-Length:")
            .next()
            .unwrap()
            .trim()
            .parse()
            .unwrap_or_else(|_| panic!("bad frame header: {header}"));
        let body = &rest[split + 4..split + 4 + length];
        messages.push(serde_json::from_slice(body).unwrap());
        rest = &rest[split + 4 + length..];
    }
    messages
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn first_divergence(native: &[u8], reference: &[u8]) -> String {
    let native = parse_stream(native);
    let reference = parse_stream(reference);
    for (index, (a, b)) in native.iter().zip(reference.iter()).enumerate() {
        if a != b {
            return format!("message #{index}\n  binary:    {a}\n  reference: {b}");
        }
    }
    format!(
        "message counts differ: binary {} vs reference {}",
        native.len(),
        reference.len()
    )
}

// ------------------------------------------------------------------ context

struct Context {
    root: PathBuf,
    python: String,
}

impl Context {
    /// The reference is this crate's engine AND its oracle, so a machine that
    /// cannot import `revl` cannot verify anything here. That is reported, not
    /// papered over: the test fails with the reason unless the run explicitly
    /// opts out with `REVL_LSP_ALLOW_NO_REFERENCE=1` (the "skip with a stated
    /// reason, never a hollow green" discipline this arc inherits).
    fn new() -> Option<Self> {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..");
        let python = std::env::var("REVL_LSP_PYTHON").unwrap_or_else(|_| "python3".to_string());
        let context = Context { root, python };
        match context.probe_reference() {
            Ok(()) => Some(context),
            Err(reason) => {
                if std::env::var("REVL_LSP_ALLOW_NO_REFERENCE").is_ok() {
                    eprintln!("SKIPPED: no reference front end to compare against: {reason}");
                    return None;
                }
                panic!(
                    "no reference front end to compare against: {reason}\n\
                     set REVL_LSP_PYTHON to an interpreter that can `import revl`, \
                     or REVL_LSP_ALLOW_NO_REFERENCE=1 to skip this crate's oracle."
                );
            }
        }
    }

    fn probe_reference(&self) -> Result<(), String> {
        let output = Command::new(&self.python)
            .arg("-c")
            .arg("import revl.lsp")
            .env("PYTHONPATH", self.root.join("src"))
            .output()
            .map_err(|err| format!("cannot run `{}`: {err}", self.python))?;
        if output.status.success() {
            Ok(())
        } else {
            Err(String::from_utf8_lossy(&output.stderr).trim().to_string())
        }
    }

    fn source(&self, relative: &str) -> String {
        let path = self.root.join(relative);
        std::fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("cannot read {}: {err}", path.display()))
    }

    fn documents(&self) -> Vec<(String, String)> {
        let mut documents: Vec<(String, String)> = CORPUS
            .iter()
            .map(|doc| (doc.name.to_string(), self.source(doc.path)))
            .collect();
        documents.extend(
            INLINE
                .iter()
                .map(|(name, source)| (name.to_string(), source.to_string())),
        );
        documents
    }

    fn run_binary(&self, input: &[u8]) -> Vec<u8> {
        let mut command = Command::new(env!("CARGO_BIN_EXE_revl-lsp"));
        command.env("REVL_LSP_PYTHON", &self.python);
        self.drive(command, input)
    }

    /// The binary with the reference fallback for navigation switched off, so
    /// the NATIVE answers are observable on their own. Diagnostics are
    /// unaffected — they never had a native path to fall back from.
    fn run_binary_native_only(&self, input: &[u8]) -> Vec<u8> {
        let mut command = Command::new(env!("CARGO_BIN_EXE_revl-lsp"));
        command.env("REVL_LSP_PYTHON", &self.python);
        command.env("REVL_LSP_NATIVE_ONLY", "1");
        self.drive(command, input)
    }

    fn run_reference(&self, input: &[u8]) -> Vec<u8> {
        let mut command = Command::new(&self.python);
        command.arg("-m").arg("revl.lsp");
        self.drive(command, input)
    }

    fn drive(&self, mut command: Command, input: &[u8]) -> Vec<u8> {
        let mut child = command
            .env("PYTHONPATH", self.root.join("src"))
            .current_dir(&self.root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("cannot start the server under test");

        let mut stdin = child.stdin.take().unwrap();
        let owned = input.to_vec();
        // write on a thread: the reply stream is far larger than a pipe buffer,
        // so a write-then-read would deadlock
        let writer = std::thread::spawn(move || {
            let _ = stdin.write_all(&owned);
            let _ = stdin.flush();
        });

        let mut output = Vec::new();
        child
            .stdout
            .take()
            .unwrap()
            .read_to_end(&mut output)
            .expect("cannot read the server's replies");
        writer.join().expect("the writer thread panicked");
        let _ = child.wait();
        output
    }
}
