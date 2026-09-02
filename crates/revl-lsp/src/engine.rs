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
//! SLICE-1 FALLBACK, STATED IN THE OPEN. The design's slice 0 embeds a private
//! interpreter (pyo3 + python-build-standalone + the frozen wheel) so the
//! artifact is ONE file. That spike is not done, so this crate takes the
//! design's recorded honest fallback (A2): the reference runs as a child
//! process against a `revl` the machine already has. The distribution story is
//! therefore "a native binary PLUS a reference `revl` alongside", not
//! "self-contained single file". What is not taken is the other option A2
//! forbids: substituting a native checker to avoid the dependency.
//!
//! Fail-closed. A worker that will not start, dies, or errors yields `Err`, and
//! the server renders that as a visible diagnostic. An engine failure must
//! never render as a clean document, which is the editor's false-admit.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

use crate::pyjson;

/// The reference analysis worker, embedded in the binary rather than installed
/// beside it, so the only external requirement is an interpreter that can
/// `import revl`.
const WORKER_SOURCE: &str = include_str!("../reference/worker.py");

/// Interpreter override, for a venv or a pinned reference build.
const PYTHON_ENV: &str = "REVL_LSP_PYTHON";

pub struct Engine {
    python: String,
    worker: Option<Worker>,
}

struct Worker {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Engine {
    pub fn new() -> Self {
        let python = std::env::var(PYTHON_ENV).unwrap_or_else(|_| "python3".to_string());
        Engine {
            python,
            worker: None,
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
        let mut child = Command::new(&self.python)
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
