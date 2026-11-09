//! The opened repository must not run as the worker starts.
//!
//! Issue #3158, the regression of #317 for the rust language server. An editor
//! launches the server with the project it opened as its working directory, and
//! the engine starts the reference worker as `python3 -c <worker source>`. On
//! CPython, `-c` puts that inherited working directory at `sys.path[0]`, ahead
//! of site-packages: the worker's very first `import json` resolves to a
//! `json.py` sitting in the repository, and the later `from revl.lsp.analysis
//! import ...` resolves to a `revl/` package there. Either one is host Python
//! running as the developer, before any `.rvl` file is looked at.
//!
//! The escape is the same one `revl._safepath.drop_cwd_entry` and `python -P`
//! (PYTHONSAFEPATH) close for `python -m revl`: the workspace comes OFF
//! `sys.path`. `engine.rs` passes `-P` on every launch it is not already
//! launching isolated, and the worker source drops the `-c` entry itself so the
//! direct `python3 -c "$(cat reference/worker.py)"` reproduction in the issue is
//! safe too.
//!
//! The shadow modules record that they ran and then exit with a code no
//! ordinary worker failure produces, so a hijack cannot be mistaken for the
//! engine's fail-closed diagnostic. Both halves are asserted: the marker is
//! ABSENT, and the answer is the real reference answer (a clean document stays
//! clean, a version request answers `ok`), so the test cannot pass by the
//! worker never running.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::{json, Value};

/// The worker source the binary embeds, driven directly by one of the tests.
const WORKER_SOURCE: &str = include_str!("../reference/worker.py");

/// A shadow module that records its import and leaves with a marker exit code.
const SHADOW: &str = "open('HIJACKED', 'w').write('ran')\nraise SystemExit(99)\n";

/// A document the reference diagnoses as clean: a worker that never ran shows
/// up as an engine diagnostic, never as this.
const CLEAN_DOC: &str = "fn add(a: Int, b: Int) -> Int {\n  return a + b\n}\n";

// ------------------------------------------------------------------- tests

/// The server, started the way an editor starts it: working directory is the
/// repository being opened, which holds a `json.py`.
#[test]
fn the_server_does_not_run_a_json_module_from_the_repository_it_opens() {
    let Some(reference) = Reference::new() else {
        return;
    };
    let workspace = Scratch::new();
    workspace.write("json.py", SHADOW);

    let published = reference.drive_clean_document(&workspace.path);

    assert!(
        !workspace.ran("HIJACKED"),
        "the repository's json.py ran: the engine put the workspace on the \
         worker's sys.path[0] (issue #3158, regression of #317)"
    );
    assert_eq!(
        published.len(),
        1,
        "the opened document was published once, not once per reply: {published:?}"
    );
    assert!(
        published[0].is_empty(),
        "a clean document came back with diagnostics, so the worker did not \
         reach the reference: {:?}",
        published[0]
    );
}

/// The same shape through the OTHER name the issue names: the worker's later
/// `from revl.lsp.analysis import ...` must not resolve to a `revl/` package in
/// the opened repository.
#[test]
fn the_server_does_not_import_a_revl_package_from_the_repository_it_opens() {
    let Some(reference) = Reference::new() else {
        return;
    };
    let workspace = Scratch::new();
    workspace.write("revl/__init__.py", SHADOW);

    let published = reference.drive_clean_document(&workspace.path);

    assert!(
        !workspace.ran("HIJACKED"),
        "the repository's revl package ran: the engine left the workspace on \
         the worker's sys.path (issue #3158, regression of #317)"
    );
    assert_eq!(published.len(), 1, "{published:?}");
    assert!(
        published[0].is_empty(),
        "a clean document came back with diagnostics: {:?}",
        published[0]
    );
}

/// The reproduction the issue text gives: the worker source itself, run with
/// `python3 -c` from a directory holding a `json.py`, with no `-P` to lean on.
#[test]
fn the_worker_source_does_not_import_a_json_module_from_its_working_directory() {
    let Some(reference) = Reference::new() else {
        return;
    };
    let workspace = Scratch::new();
    workspace.write("json.py", SHADOW);

    let mut child = Command::new(&reference.python)
        .arg("-c")
        .arg(WORKER_SOURCE)
        .current_dir(workspace.path())
        .env("PYTHONPATH", reference.root.join("src"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("cannot start the reference worker");
    child
        .stdin
        .take()
        .expect("the worker has a stdin")
        .write_all(b"{\"op\":\"version\"}\n")
        .expect("cannot ask the worker for its version");
    let mut output = Vec::new();
    child
        .stdout
        .take()
        .expect("the worker has a stdout")
        .read_to_end(&mut output)
        .expect("cannot read the worker's reply");
    let _ = child.wait();

    assert!(
        !workspace.ran("HIJACKED"),
        "the working directory's json.py ran before the worker's first import \
         (issue #3158, regression of #317)"
    );
    let reply = String::from_utf8_lossy(&output);
    assert!(
        reply.contains("\"ok\": true"),
        "the worker did not answer a version request from its working \
         directory: {reply}"
    );
}

// ----------------------------------------------------------------- harness

struct Reference {
    root: PathBuf,
    python: String,
}

impl Reference {
    /// The reference interpreter must be able to `import revl`; without one the
    /// worker cannot run at all, so the test reports the reason and skips
    /// rather than passing vacuously. CI sets `REVL_LSP_PYTHON` to its own
    /// python and `PYTHONPATH=src`, so it always runs there.
    fn new() -> Option<Self> {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..");
        let python = std::env::var("REVL_LSP_PYTHON").unwrap_or_else(|_| "python3".to_string());
        let probe = Command::new(&python)
            .arg("-c")
            .arg("import revl.lsp")
            .env("PYTHONPATH", root.join("src"))
            .output();
        match probe {
            Ok(output) if output.status.success() => Some(Reference { root, python }),
            Ok(output) => {
                eprintln!(
                    "SKIPPED: no reference front end to run the worker with: {}",
                    String::from_utf8_lossy(&output.stderr).trim()
                );
                None
            }
            Err(err) => {
                eprintln!("SKIPPED: cannot run `{python}`: {err}");
                None
            }
        }
    }

    /// Start the binary with `workspace` as the working directory (what an
    /// editor does when it opens a folder) and drive one clean document.
    /// Returns the diagnostics of each `publishDiagnostics`.
    fn drive_clean_document(&self, workspace: &Path) -> Vec<Vec<Value>> {
        let messages = vec![
            json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json!({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                   "params": {"textDocument": {"uri": "file:///opened/workspace.rvl",
                                               "languageId": "revl", "version": 1,
                                               "text": CLEAN_DOC}}}),
            json!({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}),
            json!({"jsonrpc": "2.0", "method": "exit", "params": {}}),
        ];
        let mut input = Vec::new();
        for message in messages {
            let body = serde_json::to_vec(&message).unwrap();
            input.extend_from_slice(format!("Content-Length: {}\r\n\r\n", body.len()).as_bytes());
            input.extend_from_slice(&body);
        }

        let mut child = Command::new(env!("CARGO_BIN_EXE_revl-lsp"))
            .env("REVL_LSP_PYTHON", &self.python)
            .env("PYTHONPATH", self.root.join("src"))
            .current_dir(workspace)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("cannot start the server under test");
        let mut stdin = child.stdin.take().expect("the server has a stdin");
        let writer = std::thread::spawn(move || {
            let _ = stdin.write_all(&input);
            let _ = stdin.flush();
        });
        let mut output = Vec::new();
        child
            .stdout
            .take()
            .expect("the server has a stdout")
            .read_to_end(&mut output)
            .expect("cannot read the server's replies");
        writer.join().expect("the writer thread panicked");
        let _ = child.wait();

        parse_stream(&output)
            .into_iter()
            .filter(|message| {
                message.get("method") == Some(&json!("textDocument/publishDiagnostics"))
            })
            .map(|message| {
                message["params"]["diagnostics"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
            })
            .collect()
    }
}

/// A temp directory removed when the test ends.
struct Scratch {
    path: PathBuf,
}

impl Scratch {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "revl-lsp-hostile-workspace-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|elapsed| elapsed.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&path).unwrap();
        Scratch { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }

    fn write(&self, relative: &str, text: &str) {
        let target = self.path.join(relative);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(&target, text).unwrap();
    }

    fn ran(&self, marker: &str) -> bool {
        self.path.join(marker).exists()
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// The `jsonrpc` messages of an LSP stream; a frame with no `Content-Length`
/// stops the parse rather than reading past it.
fn parse_stream(bytes: &[u8]) -> Vec<Value> {
    let mut messages = Vec::new();
    let mut rest = bytes;
    while let Some(split) = rest.windows(4).position(|window| window == b"\r\n\r\n") {
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
