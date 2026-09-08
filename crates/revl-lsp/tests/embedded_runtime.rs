//! The genuinely-single-distributed-FILE exit check (item 336, issue #102): a
//! `revl-lsp` BUILT with a runtime archive baked into its own bytes
//! (`REVL_LSP_EMBED_RUNTIME`) self-extracts that runtime into a versioned
//! private cache and answers `publishDiagnostics` byte-identically to the
//! reference server — with no `REVL_LSP_PYTHON`, no beside-exe file and no
//! runtime env archive, so the ONLY way it reaches `revl` is the bytes inside
//! the executable.
//!
//! This is the piece the slice's other tests could not reach. `runtime.rs`'s
//! unit test drives the embedded-bytes extraction helper directly, and
//! `private_runtime.rs` drives the resolver through an archive named by the
//! ENVIRONMENT — neither exercises `build.rs` actually baking an archive into a
//! real binary, nor the `Source::Embedded` branch of `locate` on a shipped
//! artifact. This test builds that binary (through the documented
//! `tools/embed_runtime.py` pin-and-pack helper, so the build glue a
//! distribution step uses is exercised too) and proves the whole path end to
//! end: the baked pin is honored, `revl/gateVersion` reports the `embedded`
//! shape, the diagnostics equal the reference's, and a second launch reuses the
//! cache.
//!
//! Like `private_runtime.rs`, the baked archive wraps a real `revl`-capable
//! interpreter (the one `REVL_LSP_TEST_RUNTIME_PYTHON` / `REVL_LSP_PYTHON`
//! names), so extraction, caching and isolated-mode invocation are the
//! production paths; only the interpreter BYTES stand in for a shipped
//! `python-build-standalone` tree, whose fetch is the distribution step (338)
//! this crate defers. Skip-with-a-reason, never a hollow green: without a
//! runtime interpreter (or a `cargo` to build with) the path cannot be driven,
//! so the test reports why rather than passing vacuously.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::{json, Value};

/// Two refusals off the self-host frontier and one clean composition — enough
/// to prove the baked-in runtime carries real refusals AND a clean green
/// through byte-for-byte, so agreement cannot be bought by answering nothing.
const DOCS: &[&str] = &[
    "examples/rejections/g1_undeclared_access.rvl",
    "examples/rejections/g4_missing_undo.rvl",
    "examples/counter_pair.rvl",
];

#[test]
fn a_binary_built_with_an_embedded_runtime_answers_from_a_versioned_private_cache() {
    let Some(env) = TestEnv::new() else {
        return;
    };

    // The interpreter must land under the pin the BUILD baked in (via the
    // helper), not a runtime-env override — so no `REVL_LSP_RUNTIME_PIN` is set
    // when the binary runs, and this is where we expect the cache.
    let interpreter = env
        .cache
        .join("revl-lsp")
        .join("runtime")
        .join(&env.pin)
        .join("bin")
        .join("python3");

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

    // First launch: nothing under the cache, so this drives the atomic
    // extraction of the BAKED-IN bytes, then answers from the extracted runtime.
    let from_runtime = parse_stream(&env.run_embedded(&input));

    assert!(
        interpreter.is_file(),
        "the baked-in runtime was not extracted to the versioned cache at {} \
         (the embedded pin `{}` was not honored)",
        interpreter.display(),
        env.pin
    );

    // The binary reports it is running self-contained on the baked-in runtime,
    // and — the point of this test — that the runtime is the `embedded` shape:
    // the genuinely single distributed FILE, not an env archive or a beside-exe
    // pair. It keyed on the pin the build baked in.
    let version = from_runtime
        .iter()
        .find(|m| m.get("id") == Some(&json!(2)))
        .expect("gateVersion was answered");
    assert_eq!(
        version["result"]["embedding"], "private-runtime",
        "the embedded binary should report the private-runtime embedding: {version}"
    );
    assert_eq!(
        version["result"]["runtime"]["source"], "embedded",
        "a runtime baked into the binary should report the embedded source: {version}"
    );
    assert_eq!(
        version["result"]["runtime"]["pin"], env.pin,
        "the embedded runtime should key on the baked-in pin: {version}"
    );

    // Equivalence: its published diagnostics equal the reference server's, byte
    // for byte, over the same documents.
    let reference = parse_stream(&env.run_reference(&input));
    let mine = published(&from_runtime);
    let theirs = published(&reference);
    assert_eq!(
        mine.len(),
        DOCS.len(),
        "the embedded binary published {} documents, expected {}",
        mine.len(),
        DOCS.len()
    );
    assert_eq!(
        mine, theirs,
        "the embedded binary's diagnostics differ from the reference server's"
    );
    assert!(
        mine.iter().any(|(_, diags)| !diags.is_empty()),
        "no refusal came through the embedded runtime — the comparison is vacuous"
    );

    // Second launch REUSES the cache rather than re-extracting the baked bytes:
    // a sentinel we drop beside the runtime survives, which it would not if
    // `<pin>/` were rebuilt.
    let sentinel = interpreter
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("REUSED_MARKER");
    std::fs::write(&sentinel, b"kept").unwrap();
    let second = published(&parse_stream(&env.run_embedded(&input)));
    assert_eq!(second, theirs, "the reused runtime answered differently");
    assert!(
        sentinel.is_file(),
        "the second launch re-extracted the baked runtime instead of reusing the cache"
    );
}

// -------------------------------------------------------------------- harness

struct TestEnv {
    root: PathBuf,
    reference_python: String,
    binary: PathBuf,
    pin: String,
    cache: PathBuf,
    _scratch: Scratch,
}

impl TestEnv {
    fn new() -> Option<Self> {
        // A real self-contained interpreter with `revl` in its own site, the
        // same knob `private_runtime.rs` reads (and CI sets to the job python).
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

        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let root = manifest_dir.join("..").join("..");
        let scratch = Scratch::new();

        // A stand-in python-build-standalone tree: `bin/python3` hands off to the
        // real interpreter. Only the interpreter bytes are a stand-in; the pack,
        // pin, embed, extraction, caching and `-I` invocation are all real.
        let runtime = scratch.path.join("runtime");
        std::fs::create_dir_all(runtime.join("bin")).unwrap();
        let launcher = runtime.join("bin").join("python3");
        std::fs::write(
            &launcher,
            format!("#!/bin/sh\nexec {:?} \"$@\"\n", reference_python),
        )
        .unwrap();
        make_executable(&launcher);

        // Pin and pack the runtime with the documented build helper, exactly as
        // a distribution build would, and read back the two env values it emits.
        let dist = scratch.path.join("dist");
        let (archive, pin) = pin_and_pack(&reference_python, &manifest_dir, &runtime, &dist)?;

        // Build a revl-lsp binary that bakes THAT archive in, under THAT pin,
        // into an isolated target dir so it never disturbs the target the other
        // tests' `CARGO_BIN_EXE_revl-lsp` points at.
        let Some(binary) = build_embedded(&manifest_dir, &scratch.path, &archive, &pin) else {
            return None;
        };

        let cache = scratch.path.join("cache");
        std::fs::create_dir_all(&cache).unwrap();

        Some(TestEnv {
            root,
            reference_python,
            binary,
            pin,
            cache,
            _scratch: scratch,
        })
    }

    fn source(&self, relative: &str) -> String {
        let path = self.root.join(relative);
        std::fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("cannot read {}: {err}", path.display()))
    }

    /// The embedded binary reaching `revl` ONLY through its baked-in bytes: no
    /// `REVL_LSP_PYTHON`, no runtime env archive, no pin override (so the baked
    /// pin governs the cache), and a fresh cache root.
    fn run_embedded(&self, input: &[u8]) -> Vec<u8> {
        let mut command = Command::new(&self.binary);
        command
            .env_remove("REVL_LSP_PYTHON")
            .env_remove("REVL_LSP_RUNTIME")
            .env_remove("REVL_LSP_RUNTIME_ARCHIVE")
            .env_remove("REVL_LSP_RUNTIME_PIN")
            .env("REVL_LSP_CACHE", &self.cache);
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

/// Run `tools/embed_runtime.py` to pin and pack `runtime` into `dist`, returning
/// the archive path and the pin it derived. Also asserts the lock file it wrote
/// records the archive's real sha256, so the build glue's own contract holds.
fn pin_and_pack(
    python: &str,
    manifest_dir: &Path,
    runtime: &Path,
    dist: &Path,
) -> Option<(PathBuf, String)> {
    let helper = manifest_dir.join("tools").join("embed_runtime.py");
    assert!(helper.is_file(), "the embed helper is missing at {}", helper.display());
    let output = Command::new(python)
        .arg(&helper)
        .arg("--runtime")
        .arg(runtime)
        .arg("--out")
        .arg(dist)
        .output()
        .expect("cannot run the embed_runtime helper");
    assert!(
        output.status.success(),
        "embed_runtime.py failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    let archive = env_value(&stdout, "REVL_LSP_EMBED_RUNTIME")
        .expect("the helper printed no REVL_LSP_EMBED_RUNTIME");
    let pin = env_value(&stdout, "REVL_LSP_EMBED_RUNTIME_PIN")
        .expect("the helper printed no REVL_LSP_EMBED_RUNTIME_PIN");

    // The documented pin record: its sha256 must match the packed archive.
    let lock: Value = serde_json::from_str(
        &std::fs::read_to_string(dist.join("runtime.lock.json"))
            .expect("the helper wrote no runtime.lock.json"),
    )
    .expect("the runtime lock is not JSON");
    let bytes = std::fs::read(&archive).expect("the packed archive is unreadable");
    let sha = sha256_hex(&bytes);
    assert_eq!(
        lock["sha256"], sha,
        "the lock's sha256 does not match the packed archive"
    );
    assert_eq!(lock["pin"], pin, "the lock's pin does not match the emitted pin");

    Some((PathBuf::from(archive), pin))
}

/// Build a `revl-lsp` binary with `archive` baked in under `pin`, into a target
/// dir under `scratch` so the shared target the other tests use is untouched.
/// Returns the built binary, or `None` (skip-with-reason) if `cargo` cannot be
/// found to drive the build.
fn build_embedded(
    manifest_dir: &Path,
    scratch: &Path,
    archive: &Path,
    pin: &str,
) -> Option<PathBuf> {
    // Cargo sets `CARGO` for the process it runs a test under; fall back to the
    // name on PATH.
    let cargo = std::env::var("CARGO").unwrap_or_else(|_| "cargo".to_string());
    let target_dir = scratch.join("embed-target");
    let manifest = manifest_dir.join("Cargo.toml");
    let status = Command::new(&cargo)
        .arg("build")
        .arg("--bin")
        .arg("revl-lsp")
        .arg("--manifest-path")
        .arg(&manifest)
        .arg("--target-dir")
        .arg(&target_dir)
        .env("REVL_LSP_EMBED_RUNTIME", archive)
        .env("REVL_LSP_EMBED_RUNTIME_PIN", pin)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status();
    let status = match status {
        Ok(status) => status,
        Err(err) => {
            eprintln!("SKIPPED: cannot run `{cargo}` to build the embedded binary: {err}");
            return None;
        }
    };
    assert!(
        status.success(),
        "building the revl-lsp binary with an embedded runtime failed"
    );
    let binary = target_dir.join("debug").join("revl-lsp");
    assert!(
        binary.is_file(),
        "the embedded build produced no binary at {}",
        binary.display()
    );
    Some(binary)
}

/// The value of `export NAME=<value>` (single-quote-stripped) in the helper's
/// shell-export output.
fn env_value(output: &str, name: &str) -> Option<String> {
    let needle = format!("export {name}=");
    for line in output.lines() {
        if let Some(rest) = line.strip_prefix(&needle) {
            let trimmed = rest.trim();
            let unquoted = trimmed
                .strip_prefix('\'')
                .and_then(|value| value.strip_suffix('\''))
                .unwrap_or(trimmed);
            return Some(unquoted.to_string());
        }
    }
    None
}

// A minimal, dependency-free sha256 so the test verifies the helper's digest
// without pulling a crate in. Reference implementation of FIPS 180-4.
fn sha256_hex(data: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut message = data.to_vec();
    let bit_len = (data.len() as u64) * 8;
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in message.chunks_exact(64) {
        let mut w = [0u32; 64];
        for (i, word) in w.iter_mut().enumerate().take(16) {
            let j = i * 4;
            *word = u32::from_be_bytes([chunk[j], chunk[j + 1], chunk[j + 2], chunk[j + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        for (slot, value) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
            *slot = slot.wrapping_add(value);
        }
    }
    let mut hex = String::with_capacity(64);
    for word in h {
        hex.push_str(&format!("{word:08x}"));
    }
    hex
}

/// A temp directory removed when the test ends.
struct Scratch {
    path: PathBuf,
}

impl Scratch {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "revl-lsp-embed-test-{}-{}",
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
