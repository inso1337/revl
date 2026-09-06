//! The diagnostics/hover/definition engine: the REFERENCE front end, driven as
//! a worker process.
//!
//! Item 336's CRITICAL (design A1) fixes what may compute an answer here. The
//! self-host front end runs natively on rust today, but only over the
//! conformance `revl` frontier; off it the native pipeline has no check to run
//! and returns "admitted", which in an editor is a MISSING squiggle — green on
//! code the reference refuses. So slice 1 computes nothing about a document in
//! rust. It asks the reference, and forwards the answer verbatim: the binary
//! matches `python -m revl.lsp` by construction, over the whole language, not
//! over a frontier.
//!
//! ONE-FILE BUNDLING (item 336, issue #102). The distribution story has two
//! interpreters, in a fixed order of preference:
//!
//!   1. A PRIVATE RUNTIME the distributable carries — a pinned
//!      `python-build-standalone` tree with the `revl` wheel frozen into its own
//!      site, extracted atomically into a versioned cache and run in ISOLATED
//!      mode (`-I`). This is the self-contained single-file path; `runtime`
//!      resolves it and this engine launches under it when present.
//!   2. A `revl`-capable interpreter the machine already has, on PATH or named
//!      by `REVL_LSP_PYTHON`. This is the honest fallback design A2 records —
//!      "a native binary PLUS a reference `revl` alongside" — taken only when no
//!      private runtime is bundled, so a bare `cargo` build and every editor
//!      that already has `revl` installed keep working unchanged.
//!
//! `REVL_LSP_PYTHON`, when set, wins over the private runtime: it is the
//! explicit override CI and the oracle harness use to pin the reference. What is
//! NOT taken, in either mode, is the option A2 forbids: substituting a native
//! checker off the self-host frontier to avoid the dependency, which is the
//! silent editor false-admit (A1).
//!
//! Fail-closed. A worker that will not start, dies, or errors yields `Err`, and
//! the server renders that as a visible diagnostic. An engine failure must
//! never render as a clean document, which is the editor's false-admit.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

use crate::pyjson;
use crate::runtime;

/// The reference analysis worker, embedded in the binary rather than installed
/// beside it, so the only external requirement is an interpreter that can
/// `import revl`.
const WORKER_SOURCE: &str = include_str!("../reference/worker.py");

/// Interpreter override, for a venv or a pinned reference build.
const PYTHON_ENV: &str = "REVL_LSP_PYTHON";

pub struct Engine {
    python: String,
    /// Launch the interpreter in isolated mode (`-I`): true for a bundled
    /// private runtime, so it reads `revl` only from its own site and ignores
    /// the machine's `PYTHONPATH`, user site, and `PATH`. False for the PATH
    /// fallback, whose whole job is to find the machine's installed `revl`.
    isolated: bool,
    /// A runtime that was CONFIGURED but could not be prepared. Held so the
    /// first request fails closed with the reason (rendered as a visible engine
    /// diagnostic), rather than silently degrading to a `python3` on PATH.
    init_error: Option<String>,
    worker: Option<Worker>,
}

struct Worker {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Engine {
    pub fn new() -> Self {
        let (python, isolated, init_error) = Self::resolve_interpreter();
        Engine {
            python,
            isolated,
            init_error,
            worker: None,
        }
    }

    /// Pick the interpreter this engine drives, and how to launch it.
    ///
    ///   1. `REVL_LSP_PYTHON`, if set, is the explicit override — it wins over
    ///      any bundled runtime so CI and the oracle can pin the reference.
    ///   2. otherwise a bundled private runtime, if one is resolvable
    ///      (`runtime::locate`), launched in isolated mode.
    ///   3. otherwise `python3` on PATH, the unchanged slice-1 fallback.
    ///
    /// A runtime that is configured but broken becomes `init_error`, not a
    /// silent fall-through to PATH.
    fn resolve_interpreter() -> (String, bool, Option<String>) {
        if let Ok(explicit) = std::env::var(PYTHON_ENV) {
            if !explicit.is_empty() {
                return (explicit, false, None);
            }
        }
        match runtime::locate() {
            Some(Ok(rt)) => (rt.python, rt.isolated, None),
            Some(Err(reason)) => ("python3".to_string(), false, Some(reason)),
            None => ("python3".to_string(), false, None),
        }
    }

    /// Which interpreter this engine analyses with — `"private-runtime"` for the
    /// self-contained bundled path, `"system-python"` for the PATH fallback.
    /// Surfaced in `revl/gateVersion` so a client can tell a self-contained
    /// artifact from one leaning on a `revl` alongside it (design: skew and
    /// distribution made legible, not hidden).
    pub fn embedding(&self) -> &'static str {
        if self.isolated {
            "private-runtime"
        } else {
            "system-python"
        }
    }

    pub fn diagnostics(&mut self, text: &str, filename: &str) -> Result<Value, String> {
        self.request(json!({"op": "diagnostics", "text": text, "filename": filename}))
    }

    pub fn hover(
        &mut self,
        text: &str,
        filename: &str,
        line: i64,
        character: i64,
    ) -> Result<Value, String> {
        self.request(json!({
            "op": "hover", "text": text, "filename": filename,
            "line": line, "character": character,
        }))
    }

    pub fn definition(
        &mut self,
        text: &str,
        filename: &str,
        uri: &Value,
        line: i64,
        character: i64,
    ) -> Result<Value, String> {
        self.request(json!({
            "op": "definition", "text": text, "filename": filename, "uri": uri,
            "line": line, "character": character,
        }))
    }

    pub fn code_actions(
        &mut self,
        text: &str,
        filename: &str,
        uri: &Value,
        range: &Value,
    ) -> Result<Value, String> {
        self.request(json!({
            "op": "codeAction", "text": text, "filename": filename,
            "uri": uri, "range": range,
        }))
    }

    /// `{language, python}` of the reference this binary is analysing with —
    /// the skew surface, so a client can tell a stale pairing from a current
    /// one before trusting the binary's greens.
    pub fn version(&mut self) -> Result<Value, String> {
        self.request(json!({"op": "version"}))
    }

    /// One worker round trip, restarting a dead worker once. A second failure
    /// is reported, never smoothed over.
    fn request(&mut self, message: Value) -> Result<Value, String> {
        match self.exchange(message.clone()) {
            Ok(value) => Ok(value),
            Err(first) => {
                self.worker = None;
                self.exchange(message)
                    .map_err(|second| format!("{first}; after restart: {second}"))
            }
        }
    }

    fn exchange(&mut self, message: Value) -> Result<Value, String> {
        self.ensure_worker()?;
        let worker = self.worker.as_mut().expect("worker was just ensured");
        let mut line = pyjson::dumps_compact(&message);
        line.push(b'\n');
        worker
            .stdin
            .write_all(&line)
            .and_then(|()| worker.stdin.flush())
            .map_err(|err| format!("cannot write to the reference worker: {err}"))?;

        let mut reply = String::new();
        let read = worker
            .stdout
            .read_line(&mut reply)
            .map_err(|err| format!("cannot read from the reference worker: {err}"))?;
        if read == 0 {
            return Err("the reference worker exited".to_string());
        }
        let payload: Value = serde_json::from_str(reply.trim())
            .map_err(|err| format!("the reference worker sent no JSON: {err}"))?;
        if payload.get("ok") == Some(&Value::Bool(true)) {
            Ok(payload.get("result").cloned().unwrap_or(Value::Null))
        } else {
            Err(payload
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("the reference worker refused the request")
                .to_string())
        }
    }

    fn ensure_worker(&mut self) -> Result<(), String> {
        if self.worker.is_some() {
            return Ok(());
        }
        // A runtime that was bundled but could not be prepared fails closed: the
        // server renders this as a visible engine diagnostic, never as a clean
        // document.
        if let Some(reason) = &self.init_error {
            return Err(format!("the bundled revl runtime is unavailable: {reason}"));
        }
        let mut command = Command::new(&self.python);
        // Isolated mode for a private runtime (`-I`): import `revl` only from the
        // runtime's own site, never the machine's `PYTHONPATH`, user site, or
        // `sys.path[0]`. The PATH fallback runs WITHOUT `-I`, because its job is
        // to find the machine's installed `revl`.
        if self.isolated {
            command.arg("-I");
        }
        let mut child = command
            .arg("-c")
            .arg(WORKER_SOURCE)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // the editor shows a server's stderr in its log; a traceback there
            // is how a broken reference install gets diagnosed
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|err| {
                format!(
                    "cannot start the reference front end with `{}`: {err} \
                     (set {PYTHON_ENV} to an interpreter that can `import revl`)",
                    self.python
                )
            })?;
        let stdin = child.stdin.take().ok_or("the worker has no stdin")?;
        let stdout = child.stdout.take().ok_or("the worker has no stdout")?;
        self.worker = Some(Worker {
            child,
            stdin,
            stdout: BufReader::new(stdout),
        });
        Ok(())
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        if let Some(mut worker) = self.worker.take() {
            let _ = worker.child.kill();
            let _ = worker.child.wait();
        }
    }
}
