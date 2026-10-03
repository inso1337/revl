"""The rust tier's generated seam dispatch fails closed (issue #1634).

`backends/rust/emit.py` (and `selfhost/emit_rust.rvl`, byte for byte) generates
`_revl_invoke` and one `_revl_dispatch_<svc>` per service. Both returned a bare
`serde_json::Value`, so every call they could not make came back as `null`:
an undeclared method, a key no component provides, a served key whose provider
could not be resolved, a parameter type the bridge cannot unmarshal. The
placement runner sent that as `{"ok": true, "value": null}` and a probe printed
`-> null`. Now both return `Result<serde_json::Value, String>` and the runner
turns an `Err` into an error reply or a probe `ERROR`.

The runner's own exported-surface check (issue #1599) stops an undeclared
method before dispatch whenever the spec lists `serve.methods`. These tests
reach the dispatch without that list, and through a probe, which the surface
check does not cover.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import placement  # noqa: E402

needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None,
                                 reason="cargo is not installed")

SOURCE = """\
service S {
  fn bump(x: Int) -> Int
}
component P provides s: S {
  provide s { fn bump(x) { return x + 1 } }
}
"""


def _emit():
    spec = importlib.util.spec_from_file_location("revl_rust_emit_1634", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# the generated code
# --------------------------------------------------------------------------

def test_the_generated_dispatch_returns_a_result_and_never_null_for_a_miss():
    code = _emit().emit(compile_source(SOURCE, "p.rvl"))
    invoke = code[code.index("pub fn _revl_invoke("):]
    invoke = invoke[:invoke.index("\n}\n")]
    dispatch = code[code.index("fn _revl_dispatch_s("):]
    dispatch = dispatch[:dispatch.index("\n}\n")]
    assert "-> Result<serde_json::Value, String> {" in invoke
    assert "-> Result<serde_json::Value, String> {" in dispatch
    assert "Value::Null" not in invoke
    assert "_ => Err(format!(\"method '{method}' is not exported for service S\"))," in dispatch
    assert "Err(_) => Err(\"no provider for key 's' right now\".to_string())," in invoke
    assert "_ => Err(format!(\"key '{key}' is not provided by this process\"))," in invoke


# --------------------------------------------------------------------------
# the running runner
# --------------------------------------------------------------------------

class _Runner:
    def __init__(self, spec_extra: dict):
        # a unix socket path is capped near 104 bytes; pytest's tmp_path on
        # macOS is longer, so the socket and spec live in a short dir
        self.dir = Path(tempfile.mkdtemp(prefix="r1634", dir="/tmp"))
        binary = placement._build_rust(compile_source(SOURCE, "p.rvl"), self.dir)
        self.sock = str(self.dir / "s.sock")
        spec = {"name": "provider", "components": ["p"], "config": {},
                "provides": ["s"], "proxies": {}, "probe": [],
                "serve": {"socket": self.sock, "keys": ["s"]}}
        spec.update(spec_extra)
        (self.dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        # stdin stays open: the runner treats EOF on it as the conductor
        # going away and stops
        self.proc = subprocess.Popen([binary, str(self.dir / "spec.json")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self.lines: list[str] = []
        threading.Thread(target=lambda: self.lines.extend(self.proc.stdout),
                         daemon=True).start()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any("] UP" in line for line in self.lines) and os.path.exists(self.sock):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError("the rust runner never came up:\n" + "".join(self.lines))

    def call(self, key: str, method: str, args: list) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(30)
            conn.connect(self.sock)
            conn.sendall((json.dumps({"key": key, "method": method, "args": args}) + "\n").encode())
            return json.loads(conn.makefile().readline())

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


@needs_cargo
def test_a_served_key_with_no_methods_list_refuses_an_undeclared_method():
    """No `serve.methods`, so the runner's surface check leaves the method to
    the generated dispatch. On the base: `{"ok": true, "value": null}`."""
    runner = _Runner({})
    try:
        assert runner.call("s", "bump", [41]) == {"ok": True, "value": 42}
        assert runner.call("s", "peek", [2]) == {
            "ok": False, "error": "method 'peek' is not exported for service S"}
    finally:
        runner.close()


@needs_cargo
def test_a_probe_the_dispatch_cannot_make_says_error_not_null():
    """A probe does not pass the runner's surface check. On the base it
    printed `-> null`."""
    runner = _Runner({"probe": [{"key": "s", "method": "peek", "args": [2]},
                                {"key": "s", "method": "bump", "args": [41]}]})
    try:
        probes = [line.rstrip() for line in runner.lines if "] probe" in line]
        assert probes == [
            "[provider] probe | s.peek(...) ERROR method 'peek' is not exported for service S",
            "[provider] probe | s.bump(...) -> 42",
        ], runner.lines
    finally:
        runner.close()
