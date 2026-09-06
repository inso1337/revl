//! `revl-lsp` — the revl language server as a native binary (roadmap item 336,
//! slice 1).
//!
//! An editor launches this and speaks LSP on its stdin/stdout, exactly as it
//! would launch `python -m revl.lsp`. The protocol, the document lifecycle and
//! the capability negotiation are rust. Slice 2 answers NAVIGATION in rust too,
//! from the self-host front end (`revl_gate::symbols`, see `native`);
//! diagnostics, explain-hover and code actions are still the reference front
//! end's, forwarded verbatim (see `engine` for why, and for the distribution
//! caveat).

mod engine;
mod native;
mod protocol;
mod pyjson;
mod runtime;
mod server;

use std::io::{self, BufReader};

use serde_json::json;

const USAGE: &str = "\
revl-lsp — the revl language server over stdio

usage:
  revl-lsp                serve LSP on stdin/stdout
  revl-lsp --version      print the server and gate version
  revl-lsp --help         print this message

environment:
  REVL_LSP_PYTHON         interpreter used to run the reference front end; wins
                          over a bundled runtime (default: python3; it must be
                          able to `import revl`)
  REVL_LSP_RUNTIME        an already-extracted private runtime directory (one
                          containing bin/python3) to analyse with, in isolated
                          mode, instead of a python on PATH
  REVL_LSP_RUNTIME_ARCHIVE  a pinned runtime archive, extracted atomically into
                          a versioned private cache on first use, then run in
                          isolated mode (the one-file bundling path)
  REVL_LSP_CACHE          cache root the versioned runtime is extracted under
                          (default: the platform cache dir)
  REVL_LSP_RUNTIME_PIN    override the pin the runtime cache is keyed by
  REVL_LSP_NATIVE_ONLY    do not fall back to the reference for hover and
                          definition, so the native answers are observable on
                          their own (the agreement oracle uses this)
";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        None => std::process::exit(serve()),
        Some("--help" | "-h") => print!("{USAGE}"),
        Some("--version" | "-V") => {
            let mut lsp = server::LspServer::new();
            let version = lsp.handle(&json!({"jsonrpc": "2.0", "id": 0,
                                             "method": "revl/gateVersion"}));
            println!(
                "{} {} (gate api {}) {}",
                server::SERVER_NAME,
                server::SERVER_VERSION,
                server::GATE_API,
                version[0]["result"]
            );
        }
        Some(other) => {
            eprintln!("revl-lsp: unknown argument `{other}`\n\n{USAGE}");
            std::process::exit(2);
        }
    }
}

/// Read framed JSON-RPC from stdin, dispatch, and frame replies onto stdout
/// until `exit` or EOF.
fn serve() -> i32 {
    let stdin = io::stdin();
    let mut reader = BufReader::new(stdin.lock());
    let stdout = io::stdout();
    let mut writer = stdout.lock();
    let mut lsp = server::LspServer::new();

    while let Some(message) = protocol::read_message(&mut reader) {
        for outgoing in lsp.handle(&message) {
            if protocol::write_message(&mut writer, &outgoing).is_err() {
                return 1; // the client is gone
            }
        }
        if lsp.shutting_down && message.get("method").and_then(|m| m.as_str()) == Some("exit") {
            break;
        }
    }
    0
}
