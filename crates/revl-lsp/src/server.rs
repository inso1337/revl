//! JSON-RPC method dispatch over open text documents — the rust twin of
//! `src/revl/lsp/server.py`.
//!
//! Everything in this file is the part of the language server item 336 makes
//! native: the dispatch table, the per-URI full-text document store, the
//! capability negotiation, and the `publishDiagnostics` cadence. The analysis
//! itself is not here; it is the reference front end, reached through `engine`
//! (see that module for why a native checker may not stand in for it in slice
//! 1).
//!
//! Slice 2 splits the analysis in two. Navigation (`definition`, and the
//! symbol half of `hover`) is answered natively from `revl_gate::symbols` and
//! never reaches the reference; diagnostics, explain-hover and code actions
//! still do, because a native diagnostics engine off the self-host frontier
//! shows green where the reference refuses (design A1). The native table is
//! recomputed once per document version and held beside the text, so a
//! navigation request costs a lookup instead of an interpreter round trip.
//!
//! `handle` maps one incoming message to the messages to send back, exactly as
//! the reference's `handle` does, so a test can drive it with decoded messages
//! and no stream.

use std::collections::HashMap;

use serde_json::{json, Value};

use revl_gate::symbols::Symbols;

use crate::engine::Engine;
use crate::native;
use crate::protocol;

pub const SERVER_NAME: &str = "revl-lsp";
pub const SERVER_VERSION: &str = "2.0";

/// The `gate_version` API level this binary speaks (design: the version surface
/// that makes a stale redistributed binary detectable). Slice 2 answers
/// navigation from the native gate, so the reported surface changed and the
/// level is bumped with it.
pub const GATE_API: &str = "1";

/// Set to any value to stop navigation falling back to the reference, so the
/// NATIVE answer is observable on its own. This exists for the oracle
/// (`tests/reference_agreement.rs`), which has to see what the native path
/// alone would say in order to prove it never says something WRONG. It is a
/// strictly-fewer-answers mode: it can never produce an answer the ordinary
/// binary does not.
const NATIVE_ONLY_ENV: &str = "REVL_LSP_NATIVE_ONLY";

/// A document key: the URI as the client sent it, with `None` for a message
/// that carried none — the reference stores those under `None` too.
type DocKey = Option<String>;

pub struct LspServer {
    documents: HashMap<DocKey, String>,
    /// The native declaration table per document version, or the native front
    /// end's refusal to decide the document.
    symbols: HashMap<DocKey, Symbols>,
    /// The diagnostics last published for a document, computed from this same
    /// text. Navigation may only be answered natively for a document with none
    /// (`native::answerable`), so this is what that decision reads.
    published: HashMap<DocKey, Value>,
    pub shutting_down: bool,
    engine: Engine,
    native_only: bool,
}

impl LspServer {
    pub fn new() -> Self {
        LspServer {
            documents: HashMap::new(),
            symbols: HashMap::new(),
            published: HashMap::new(),
            shutting_down: false,
            engine: Engine::new(),
            native_only: std::env::var(NATIVE_ONLY_ENV).is_ok(),
        }
    }

    // ------------------------------------------------------------ dispatch

    pub fn handle(&mut self, message: &Value) -> Vec<Value> {
        let method = message.get("method").and_then(Value::as_str).unwrap_or("");
        let request_id = message.get("id").cloned().unwrap_or(Value::Null);
        let empty = json!({});
        let params = match message.get("params") {
            Some(Value::Object(_)) => message.get("params").unwrap(),
            _ => &empty,
        };

        match method {
            "initialize" => vec![protocol::response(&request_id, initialize_result())],
            "initialized" | "$/setTrace" => vec![],
            "shutdown" => {
                self.shutting_down = true;
                vec![protocol::response(&request_id, Value::Null)]
            }
            "exit" => {
                self.shutting_down = true;
                vec![]
            }
            "textDocument/didOpen" => self.did_open(params),
            "textDocument/didChange" => self.did_change(params),
            "textDocument/didClose" => self.did_close(params),
            "textDocument/hover" => vec![protocol::response(&request_id, self.hover(params))],
            "textDocument/definition" => {
                vec![protocol::response(&request_id, self.definition(params))]
            }
            "textDocument/codeAction" => {
                vec![protocol::response(&request_id, self.code_action(params))]
            }
            // A revl extension, not part of the reference's surface: the
            // version triple a client compares against its expected pin before
            // trusting this binary's greens. The reference answers "method not
            // found" here, which is the one deliberate divergence in the
            // dispatch table, and it is additive.
            "revl/gateVersion" => vec![protocol::response(&request_id, self.gate_version())],
            _ => {
                // an explicit `"id": null` is not a request id, matching the
                // reference's `request_id is None` test
                if request_id.is_null() {
                    vec![] // an unknown notification is ignored, per the spec
                } else {
                    vec![protocol::error(
                        &request_id,
                        -32601,
                        &format!("method not found: {method}"),
                    )]
                }
            }
        }
    }

    // ------------------------------------------------------------ documents

    fn did_open(&mut self, params: &Value) -> Vec<Value> {
        let doc = params.get("textDocument").cloned().unwrap_or(json!({}));
        let uri = uri_of(&doc);
        let text = doc
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        self.store(&uri, text);
        vec![self.publish(&uri)]
    }

    fn did_change(&mut self, params: &Value) -> Vec<Value> {
        let uri = params
            .get("textDocument")
            .map(uri_of)
            .unwrap_or(Value::Null);
        let changes = params
            .get("contentChanges")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if let (Some(last), false) = (changes.last(), uri.is_null()) {
            // full sync: the last change carries the complete new text
            let text = last
                .get("text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            self.store(&uri, text);
        }
        vec![self.publish(&uri)]
    }

    fn did_close(&mut self, params: &Value) -> Vec<Value> {
        let uri = params
            .get("textDocument")
            .map(uri_of)
            .unwrap_or(Value::Null);
        let key = key_of(&uri);
        self.documents.remove(&key);
        self.symbols.remove(&key);
        self.published.remove(&key);
        // clear the client's squiggles for a document we no longer track
        vec![protocol::notification(
            "textDocument/publishDiagnostics",
            json!({"uri": uri, "diagnostics": []}),
        )]
    }

    /// Record a new document version and rebuild its native declaration table
    /// in the same step, so the two can never describe different text.
    fn store(&mut self, uri: &Value, text: String) {
        let key = key_of(uri);
        self.symbols.insert(key.clone(), native::table_for(&text));
        self.documents.insert(key, text);
    }

    fn publish(&mut self, uri: &Value) -> Value {
        let text = self
            .documents
            .get(&key_of(uri))
            .cloned()
            .unwrap_or_default();
        let filename = filename_of(uri);
        let mut diagnostics = match self.engine.diagnostics(&text, &filename) {
            Ok(value) => value,
            // Fail closed: an engine that cannot answer is reported as an error
            // on the document, never as an empty diagnostics list. Silence here
            // is the editor's false-admit.
            Err(reason) => json!([engine_diagnostic(&reason)]),
        };
        // The agree-or-ADD rule: the reference's diagnostics are never dropped,
        // and a native refusal it did not report is appended. Never the other
        // way round — see `native::extra_diagnostics` for why the native gate
        // cannot become the engine here.
        if let Some(rows) = diagnostics.as_array_mut() {
            rows.extend(native::extra_diagnostics(&text, &json!(rows.clone())));
        }
        self.published.insert(key_of(uri), diagnostics.clone());
        protocol::notification(
            "textDocument/publishDiagnostics",
            json!({"uri": uri, "diagnostics": diagnostics}),
        )
    }

    // ------------------------------------------------------------ requests

    /// Hover: the native signature when the document is clean and the native
    /// front end can spell it, else the reference (which owns the guarantee
    /// text a diagnostic hover shows, and every answer on a document its own
    /// parser refused).
    fn hover(&mut self, params: &Value) -> Value {
        let (uri, line, character) = locate(params);
        let key = key_of(&uri);
        let Some(text) = self.documents.get(&key).cloned() else {
            return Value::Null;
        };
        if native::answerable(self.published.get(&key)) {
            if let Some(table) = self.symbols.get(&key) {
                if let Some(answer) = native::hover(table, &text, line, character) {
                    return answer;
                }
            }
        }
        if self.native_only {
            return Value::Null;
        }
        self.engine
            .hover(&text, &filename_of(&uri), line, character)
            .unwrap_or(Value::Null)
    }

    /// Go-to-definition: the native declaration table when the document is
    /// clean and the table resolves the word, else the reference. The table
    /// never resolves a name a local could shadow, so an answer here is the
    /// reference's answer or nothing.
    fn definition(&mut self, params: &Value) -> Value {
        let (uri, line, character) = locate(params);
        let key = key_of(&uri);
        let Some(text) = self.documents.get(&key).cloned() else {
            return Value::Null;
        };
        if native::answerable(self.published.get(&key)) {
            if let Some(table) = self.symbols.get(&key) {
                if let Some(answer) = native::definition(table, &text, &uri, line, character) {
                    return answer;
                }
            }
        }
        if self.native_only {
            return Value::Null;
        }
        self.engine
            .definition(&text, &filename_of(&uri), &uri, line, character)
            .unwrap_or(Value::Null)
    }

    fn code_action(&mut self, params: &Value) -> Value {
        let uri = params
            .get("textDocument")
            .map(uri_of)
            .unwrap_or(Value::Null);
        let Some(text) = self.documents.get(&key_of(&uri)).cloned() else {
            return json!([]);
        };
        let zero = json!({"start": {"line": 0, "character": 0},
                          "end": {"line": 0, "character": 0}});
        let range = match params.get("range") {
            Some(Value::Object(_)) => params.get("range").cloned().unwrap(),
            _ => zero,
        };
        self.engine
            .code_actions(&text, &filename_of(&uri), &uri, &range)
            .unwrap_or_else(|_| json!([]))
    }

    /// The skew surface: what a client compares against its expected pin before
    /// trusting this binary's greens.
    ///
    /// Slice 2 runs two engines, so it reports both. `frontier` stays
    /// `"reference"` because DIAGNOSTICS — the answers a green depends on — are
    /// still the reference's over the whole language; the native gate's own pin
    /// is reported beside it under `native`, which is the surface navigation is
    /// answered over and the one a stale-binary audit compares.
    fn gate_version(&mut self) -> Value {
        let reference = self.engine.version().unwrap_or(Value::Null);
        let gate = revl_gate::gate_version();
        json!({
            "api": GATE_API,
            "language": reference.get("language").cloned().unwrap_or(Value::Null),
            "frontier": "reference",
            "engine": "reference-diagnostics + native-navigation",
            // which interpreter answers diagnostics: the self-contained bundled
            // runtime, or a `revl` on the machine. A client can tell a one-file
            // artifact from one leaning on an installed `revl` alongside it.
            "embedding": self.engine.embedding(),
            "native": {
                "api": gate.api,
                "language": gate.language,
                "frontier": gate.frontier,
                "layer": gate.layer,
                "answers": ["textDocument/definition", "textDocument/hover (symbol)"],
            },
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    }
}

impl Default for LspServer {
    fn default() -> Self {
        Self::new()
    }
}

/// Byte-for-byte the capabilities `src/revl/lsp/server.py` advertises.
fn initialize_result() -> Value {
    json!({
        "capabilities": {
            // 1 == full-document sync: every change carries the whole text
            "textDocumentSync": 1,
            "hoverProvider": true,
            "definitionProvider": true,
            // quick fixes for the diagnostics fixgen can rewrite (item 287)
            "codeActionProvider": {"codeActionKinds": ["quickfix"]},
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })
}

fn engine_diagnostic(reason: &str) -> Value {
    json!({
        "range": {"start": {"line": 0, "character": 0},
                  "end": {"line": 0, "character": 0}},
        "severity": 1,
        "code": "REVL-LSP-ENGINE",
        "source": "revl-lsp",
        "message": format!(
            "the revl reference front end is unavailable, so this document was \
             NOT checked: {reason}"
        ),
    })
}

fn locate(params: &Value) -> (Value, i64, i64) {
    let uri = params
        .get("textDocument")
        .map(uri_of)
        .unwrap_or(Value::Null);
    let position = params.get("position").cloned().unwrap_or(json!({}));
    let line = position.get("line").and_then(Value::as_i64).unwrap_or(0);
    let character = position
        .get("character")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    (uri, line, character)
}

fn uri_of(document: &Value) -> Value {
    document.get("uri").cloned().unwrap_or(Value::Null)
}

fn key_of(uri: &Value) -> DocKey {
    uri.as_str().map(str::to_string)
}

/// A display filename for the checker from a `file://` URI. The name only
/// reaches diagnostic text, never the disk, so a best-effort strip is fine.
fn filename_of(uri: &Value) -> String {
    match uri.as_str() {
        None | Some("") => "<lsp>.rvl".to_string(),
        Some(text) => text
            .strip_prefix("file://")
            .unwrap_or(text)
            .to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initialize_advertises_the_reference_capabilities() {
        let mut server = LspServer::new();
        let replies = server.handle(&json!({"jsonrpc": "2.0", "id": 1,
                                            "method": "initialize", "params": {}}));
        let capabilities = &replies[0]["result"]["capabilities"];
        assert_eq!(capabilities["textDocumentSync"], 1);
        assert_eq!(capabilities["hoverProvider"], true);
        assert_eq!(capabilities["definitionProvider"], true);
        assert_eq!(capabilities["codeActionProvider"]["codeActionKinds"][0], "quickfix");
        assert_eq!(replies[0]["result"]["serverInfo"]["name"], SERVER_NAME);
    }

    #[test]
    fn an_unknown_request_is_method_not_found_and_a_notification_is_ignored() {
        let mut server = LspServer::new();
        let replies = server.handle(&json!({"jsonrpc": "2.0", "id": 7, "method": "nope"}));
        assert_eq!(replies[0]["error"]["code"], -32601);
        assert!(server
            .handle(&json!({"jsonrpc": "2.0", "method": "nope"}))
            .is_empty());
    }

    #[test]
    fn shutdown_then_exit_stops_the_loop() {
        let mut server = LspServer::new();
        let replies = server.handle(&json!({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}));
        assert_eq!(replies[0]["result"], Value::Null);
        assert!(server.shutting_down);
        assert!(server.handle(&json!({"jsonrpc": "2.0", "method": "exit"})).is_empty());
    }

    #[test]
    fn a_request_for_an_unopened_document_resolves_to_nothing() {
        let mut server = LspServer::new();
        let params = json!({"textDocument": {"uri": "file:///absent.rvl"},
                            "position": {"line": 0, "character": 0}});
        let replies = server.handle(&json!({"jsonrpc": "2.0", "id": 3,
                                            "method": "textDocument/hover",
                                            "params": params}));
        assert_eq!(replies[0]["result"], Value::Null);
    }

    #[test]
    fn closing_a_document_clears_its_squiggles_without_asking_the_engine() {
        let mut server = LspServer::new();
        let replies = server.handle(&json!({"jsonrpc": "2.0",
                                            "method": "textDocument/didClose",
                                            "params": {"textDocument": {"uri": "file:///a.rvl"}}}));
        assert_eq!(replies[0]["method"], "textDocument/publishDiagnostics");
        assert_eq!(replies[0]["params"]["diagnostics"], json!([]));
    }

    #[test]
    fn a_uri_becomes_the_checker_filename() {
        assert_eq!(filename_of(&json!("file:///tmp/a.rvl")), "/tmp/a.rvl");
        assert_eq!(filename_of(&json!("untitled:1")), "untitled:1");
        assert_eq!(filename_of(&Value::Null), "<lsp>.rvl");
    }

    #[test]
    fn an_engine_failure_is_a_visible_diagnostic_not_a_clean_document() {
        let diagnostic = engine_diagnostic("boom");
        assert_eq!(diagnostic["severity"], 1);
        assert_eq!(diagnostic["code"], "REVL-LSP-ENGINE");
        assert!(diagnostic["message"].as_str().unwrap().contains("NOT checked"));
    }
}
