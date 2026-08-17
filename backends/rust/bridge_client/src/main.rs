//! Rust side of the interop bridge (docs/interop-bridge.md §3): a Rust process
//! consuming a service provided by a revl component in ANOTHER language, over
//! the same newline-delimited JSON wire as py<->py and py<->node.
//!
//! This is the "quick" Rust milestone: it proves the wire is Rust-speakable by
//! calling a Python-provided `db` service (from demo/bridge_pypy.py --provider)
//! and getting typed values back. It deliberately does NOT touch cordis-rs.
//! The full version (a `Box<dyn Database>` proxy that lets a cordis-rs
//! component consume the seam, wired into `revl run --placement`) needs the
//! emitter to generate the typed proxy impl, since cordis-rs services are
//! static traits, not dynamic, so that is the follow-up.
//!
//! Usage: revl_bridge_client <unix-socket-path>

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;

/// One blocking request/response against the stub: send a call, read the reply.
fn call(path: &str, key: &str, method: &str, args: Vec<Value>) -> Value {
    let stream = UnixStream::connect(path).expect("connect to bridge stub");
    let mut writer = stream.try_clone().expect("clone stream");
    let request = json!({ "key": key, "method": method, "args": args });
    let mut line = serde_json::to_string(&request).unwrap();
    line.push('\n');
    writer.write_all(line.as_bytes()).expect("write request");

    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    reader.read_line(&mut response).expect("read reply");
    let reply: Value = serde_json::from_str(&response).expect("parse reply");
    if !reply["ok"].as_bool().unwrap_or(false) {
        panic!("remote error: {}", reply["error"]);
    }
    reply["value"].clone()
}

fn main() {
    let path = std::env::args().nth(1).expect("usage: revl_bridge_client <socket>");

    // Call the Python-provided `db` service across the language boundary.
    let executed = call(&path, "db", "execute", vec![json!("INSERT INTO cache_log VALUES (rust)")]);
    println!("[rust] db.execute -> {executed}");
    let rows = call(&path, "db", "query", vec![json!("SELECT 1")]);
    println!("[rust] db.query   -> {rows}");

    // The value the Python pool returned marshalled back into a typed Rust value.
    assert_eq!(executed.as_i64(), Some(1), "execute should return 1 (Int) from the Python pool");
    assert!(rows.is_array(), "query should return a List");
    println!("[rust] OK");
}
