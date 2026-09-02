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
//! Two tests, and they fail in different directions on purpose:
//!
//!   * `binary_matches_the_reference_byte_for_byte` compares whole reply
//!     streams. A native engine that answered differently fails here.
//!   * `off_frontier_documents_still_get_their_squiggles` asserts the binary
//!     shows the exact refusal code the reference shows on each off-frontier
//!     document. A native engine that answered NOTHING (the silent
//!     false-admit, the dangerous direction) fails here, loudly.

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

/// Documents that exercise the protocol's edges rather than the language's.
const INLINE: &[(&str, &str)] = &[
    ("empty", ""),
    ("syntax_error", "fn broken( {\n"),
    (
        "in_frontier_g1",
        "fn add(a: Int, b: Int) -> Int {\n  return a + c\n}\n",
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
    assert_eq!(version["api"], "0");
    assert_eq!(version["frontier"], "reference");
    assert_eq!(version["engine"], "reference-subprocess");
    assert!(version["language"].is_string(), "{version}");
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

/// Up to eight probe positions per document: the first three identifiers of
/// each code line, which on a revl line is the keyword, the declared name, and
/// the first parameter or type — so the probes land on real symbols (hover and
/// definition resolve something) as well as on keywords (they resolve nothing).
/// Deterministic, so both servers see exactly the same requests.
fn sample_positions(source: &str) -> Vec<(usize, usize)> {
    let mut positions = Vec::new();
    for (index, line) in source.split('\n').enumerate() {
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with("//") {
            continue;
        }
        for start in identifier_spans(line).into_iter().take(3) {
            positions.push((index, start));
        }
        if positions.len() >= 8 {
            break;
        }
    }
    positions.truncate(8);
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
