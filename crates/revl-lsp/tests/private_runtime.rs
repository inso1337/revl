//! The one-file bundling exit checks (item 336, issue #102): a `revl-lsp` that
//! carries its OWN interpreter answers `publishDiagnostics` byte-identically to
//! the reference server, having extracted that interpreter atomically into a
//! versioned private cache and launched it in isolated mode — with no
//! `REVL_LSP_PYTHON` set and nothing but the bundled runtime to reach `revl`
//! through.
//!
//! What this proves, and what it defers. It exercises the runtime-management
//! machinery this slice owns — archive resolution, ATOMIC versioned-cache
//! extraction, cache REUSE on a second launch, and ISOLATED-mode invocation —
//! and that routing diagnostics through the private runtime changes not one byte
//! of the answer. The runtime here is a real self-contained interpreter with
//! `revl` in its own site (the one `REVL_LSP_TEST_RUNTIME_PYTHON` names, e.g. a
//! project venv), reached through a wrapper `bin/python3`, so the extraction,
//! caching and isolation paths are the production ones. What it does NOT ship is
//! the pinned `python-build-standalone` archive itself, nor the in-process pyo3
//! link — those are the distribution/build step (338) layered on this contract.
//!
//! Skip-with-a-reason, never a hollow green: without a runtime interpreter to
//! wrap, the machinery cannot be driven end to end, so the test reports why it
//! could not run rather than passing vacuously.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::{json, Value};

/// Corpus documents to compare: two refusals off the self-host frontier and one
/// clean composition (the empty-diagnostics case), enough to prove the private
/// runtime carries real refusals AND a clean green through unchanged.
const DOCS: &[&str] = &[
    "examples/rejections/g1_undeclared_access.rvl",
    "examples/rejections/g4_missing_undo.rvl",
    "examples/counter_pair.rvl",
];

#[test]
fn a_bundled_runtime_answers_from_a_versioned_private_cache_in_isolated_mode() {
    let Some(env) = TestEnv::new() else {
        return;
    };

    let mut messages = vec![request(1, "initialize", json!({}))];
    for (index, relative) in DOCS.iter().enumerate() {
        messages.push(did_open(
            &format!("file:///corpus/{index}.rvl"),
            &env.source(relative),
        ));
    }
    messages.push(request(2, "revl/gateVersion", json!({})));
    messages.push(notification("exit", json!({})));
    let input = frame_all(&messages);

    // First launch: nothing under the cache yet, so this drives the atomic
    // extraction, then answers from the freshly extracted runtime.
    let from_runtime = parse_stream(&env.run_bundled(&input));

    // The interpreter landed in the VERSIONED private cache, at the pin's dir.
    let interpreter = env
        .cache
        .join("revl-lsp")
        .join("runtime")
        .join("test-pin-102")
        .join("bin")
        .join("python3");
    assert!(
        interpreter.is_file(),
        "the runtime was not extracted to the versioned cache at {}",
        interpreter.display()
    );

    // The binary reports it is running self-contained, not on a machine python.
    let version = from_runtime
        .iter()
        .find(|m| m.get("id") == Some(&json!(2)))
        .expect("gateVersion was answered");
    assert_eq!(
        version["result"]["embedding"], "private-runtime",
        "the bundled binary should report the private-runtime embedding: {version}"
    );

    // Equivalence: its published diagnostics equal the reference server's, byte
    // for byte, over the same documents.
    let reference = parse_stream(&env.run_reference(&input));
    let mine = published(&from_runtime);
    let theirs = published(&reference);
    assert_eq!(
        mine.len(),
        DOCS.len(),
        "the bundled binary published {} documents, expected {}",
        mine.len(),
        DOCS.len()
    );
    assert_eq!(
        mine, theirs,
        "the bundled binary's diagnostics differ from the reference server's"
    );
    // Not a stream of empty greens: at least one real refusal came through the
    // private runtime.
    assert!(
        mine.iter().any(|(_, diags)| !diags.is_empty()),
        "no refusal came through the private runtime — the comparison is vacuous"
    );

    // Second launch REUSES the cache rather than re-extracting: a sentinel we
    // drop beside the runtime survives, which it would not if `<pin>/` were
    // rebuilt.
    let sentinel = interpreter
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("REUSED_MARKER");
    std::fs::write(&sentinel, b"kept").unwrap();
    let second = published(&parse_stream(&env.run_bundled(&input)));
    assert_eq!(second, theirs, "the reused runtime answered differently");
    assert!(
        sentinel.is_file(),
        "the second launch re-extracted the runtime instead of reusing the cache"
    );
}

// -------------------------------------------------------------------- harness

struct TestEnv {
    root: PathBuf,
    reference_python: String,
    archive: PathBuf,
    cache: PathBuf,
    // kept so the temp tree lives for the whole test
    _scratch: Scratch,
}

impl TestEnv {
    fn new() -> Option<Self> {
        // A real self-contained interpreter with `revl` in its own site. The
        // agreement crate already needs one to compare against; reuse the same
        // knob names so a CI job that sets REVL_LSP_PYTHON gets this for free.
        let reference_python = std::env::var("REVL_LSP_TEST_RUNTIME_PYTHON")
            .or_else(|_| std::env::var("REVL_LSP_PYTHON"))
            .ok()
            .filter(|value| !value.is_empty())?;
        if !Path::new(&reference_python).is_file() {
            eprintln!(
                "SKIPPED: the runtime interpreter {reference_python} does not exist \
                 (set REVL_LSP_TEST_RUNTIME_PYTHON to a python that can `import revl`)"
            );
            return None;
        }

        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..");
        let scratch = Scratch::new();

        // Build a runtime archive: a self-contained tree whose `bin/python3`
        // hands off to the real interpreter. Extraction, caching and `-I`
        // invocation are exercised for real; only the bytes of the interpreter
        // are a stand-in for a shipped python-build-standalone tree.
        let runtime = scratch.path.join("runtime");
        std::fs::create_dir_all(runtime.join("bin")).unwrap();
        let launcher = runtime.join("bin").join("python3");
        std::fs::write(
            &launcher,
            format!("#!/bin/sh\nexec {:?} \"$@\"\n", reference_python),
        )
        .unwrap();
        make_executable(&launcher);

        let archive = scratch.path.join("runtime.tar");
        let status = Command::new("tar")
            .arg("-cf")
            .arg(&archive)
            .arg("-C")
            .arg(&runtime)
            .arg(".")
            .status()
            .expect("cannot run tar to build the runtime archive");
        assert!(status.success(), "tar failed to build the runtime archive");

        let cache = scratch.path.join("cache");
        std::fs::create_dir_all(&cache).unwrap();

        Some(TestEnv {
            root,
            reference_python,
            archive,
            cache,
            _scratch: scratch,
        })
    }

    fn source(&self, relative: &str) -> String {
        let path = self.root.join(relative);
        std::fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("cannot read {}: {err}", path.display()))
    }

    /// The binary reaching `revl` ONLY through the bundled runtime: no
    /// `REVL_LSP_PYTHON`, the archive and a fresh versioned cache set.
    fn run_bundled(&self, input: &[u8]) -> Vec<u8> {
        let mut command = Command::new(env!("CARGO_BIN_EXE_revl-lsp"));
        command
            .env_remove("REVL_LSP_PYTHON")
            .env("REVL_LSP_RUNTIME_ARCHIVE", &self.archive)
            .env("REVL_LSP_CACHE", &self.cache)
            .env("REVL_LSP_RUNTIME_PIN", "test-pin-102");
        self.drive(command, input)
    }

    fn run_reference(&self, input: &[u8]) -> Vec<u8> {
        let mut command = Command::new(&self.reference_python);
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

/// A temp directory removed when the test ends.
struct Scratch {
    path: PathBuf,
}

impl Scratch {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "revl-lsp-runtime-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&path).unwrap();
        Scratch { path }
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

#[cfg(unix)]
fn make_executable(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let mut perms = std::fs::metadata(path).unwrap().permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(path, perms).unwrap();
}

#[cfg(not(unix))]
fn make_executable(_path: &Path) {}

// -------------------------------------------------------------------- framing

fn published(messages: &[Value]) -> Vec<(String, Vec<Value>)> {
    messages
        .iter()
        .filter(|m| m.get("method") == Some(&json!("textDocument/publishDiagnostics")))
        .map(|m| {
            (
                m["params"]["uri"].as_str().unwrap_or_default().to_string(),
                m["params"]["diagnostics"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default(),
            )
        })
        .collect()
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
