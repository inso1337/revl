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
//! `handle` maps one incoming message to the messages to send back, exactly as
//! the reference's `handle` does, so a test can drive it with decoded messages
//! and no stream.

use std::collections::HashMap;

use serde_json::{json, Value};

use crate::engine::Engine;
use crate::protocol;

pub const SERVER_NAME: &str = "revl-lsp";
pub const SERVER_VERSION: &str = "2.0";

/// The `gate_version` API level this binary speaks (design: the version surface
/// that makes a stale redistributed binary detectable).
pub const GATE_API: &str = "0";

/// A document key: the URI as the client sent it, with `None` for a message
/// that carried none — the reference stores those under `None` too.
type DocKey = Option<String>;

pub struct LspServer {
    documents: HashMap<DocKey, String>,
    pub shutting_down: bool,
    engine: Engine,
}

impl LspServer {
    pub fn new() -> Self {
        LspServer {
            documents: HashMap::new(),
            shutting_down: false,
            engine: Engine::new(),
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
        self.documents.insert(key_of(&uri), text);
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
            self.documents.insert(key_of(&uri), text);
        }
        vec![self.publish(&uri)]
    }

    fn did_close(&mut self, params: &Value) -> Vec<Value> {
        let uri = params
            .get("textDocument")
            .map(uri_of)
            .unwrap_or(Value::Null);
        self.documents.remove(&key_of(&uri));
        // clear the client's squiggles for a document we no longer track
        vec![protocol::notification(
            "textDocument/publishDiagnostics",
            json!({"uri": uri, "diagnostics": []}),
        )]
    }

    fn publish(&mut self, uri: &Value) -> Value {
        let text = self
            .documents
            .get(&key_of(uri))
            .cloned()
            .unwrap_or_default();
        let filename = filename_of(uri);
        let diagnostics = match self.engine.diagnostics(&text, &filename) {
            Ok(value) => value,
            // Fail closed: an engine that cannot answer is reported as an error
            // on the document, never as an empty diagnostics list. Silence here
            // is the editor's false-admit.
            Err(reason) => json!([engine_diagnostic(&reason)]),
        };
        protocol::notification(
            "textDocument/publishDiagnostics",
            json!({"uri": uri, "diagnostics": diagnostics}),
        )
    }

    // ------------------------------------------------------------ requests

    fn hover(&mut self, params: &Value) -> Value {
        let (uri, line, character) = locate(params);
        let Some(text) = self.documents.get(&key_of(&uri)).cloned() else {
            return Value::Null;
        };
        self.engine
            .hover(&text, &filename_of(&uri), line, character)
            .unwrap_or(Value::Null)
    }

    fn definition(&mut self, params: &Value) -> Value {
        let (uri, line, character) = locate(params);
        let Some(text) = self.documents.get(&key_of(&uri)).cloned() else {
            return Value::Null;
        };
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

    fn gate_version(&mut self) -> Value {
        let reference = self.engine.version().unwrap_or(Value::Null);
        json!({
            "api": GATE_API,
            "language": reference.get("language").cloned().unwrap_or(Value::Null),
            // Slice 1's engine is the reference front end, so the covered
            // surface is the whole language, not the self-host frontier. A
            // slice-2/3 binary reports the frontier pin its native engine is
            // trusted over.
            "frontier": "reference",
            "engine": "reference-subprocess",
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
