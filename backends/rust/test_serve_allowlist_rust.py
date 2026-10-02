"""The rust placement runner serves only what its process exports (issue #1599).

A placement process's spec carries `serve.keys` (the keys other processes
consume from it) and `serve.methods` (per key, the operations the service
declaration admits). The rust runner handed every request to the generated
`_revl_invoke`, which resolves EVERY key the document's components provide and
answers a key or method it does not know with `null`; `handle_conn` wrapped
that in `"ok": true`. So a raw call to a key the process provides but does not
export was answered, and an unknown key or method read as an answer of `null`.

These tests run the real runner, built the way the conductor builds it, with a
raw client on its socket. The program provides `s: S` (`bump`, `twice`) and
`h: Hidden` (`peek`) in one component; the spec serves `s` only.
"""

from __future__ import annotations

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

pytestmark = pytest.mark.skipif(shutil.which("cargo") is None,
                                reason="cargo is not installed")

SOURCE = """\
service S {
  fn bump(x: Int) -> Int
  fn twice(x: Int) -> Int
}
service Hidden { fn peek(x: Int) -> Int }
component P provides s: S, h: Hidden {
  provide s {
    fn bump(x) { return x + 1 }
    fn twice(x) { return x * 2 }
  }
  provide h { fn peek(x) { return x * 100 } }
}
"""


class _Runner:
    """One runner process from a hand-written spec, with a raw client on its
    serve socket."""

    def __init__(self, binary: str, spec: dict, workdir: Path, label: str):
        self.sock = spec["serve"]["socket"]
        spec_path = workdir / f"{label}.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        # stdin stays open: the runner treats EOF on it as the conductor
        # going away and stops
        self.proc = subprocess.Popen([binary, str(spec_path)],
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


def _short_dir() -> Path:
    # a unix socket path is capped near 104 bytes; pytest's tmp_path on macOS
    # is longer, so sockets and specs live in a short dir
    return Path(tempfile.mkdtemp(prefix="r1599", dir="/tmp"))


class _Provider(_Runner):
    def __init__(self, methods: dict):
        self.dir = _short_dir()
        binary = placement._build_rust(compile_source(SOURCE, "p.rvl"), self.dir)
        # the rust runner names a component by its snake_case plugin name
        spec = {"name": "provider", "components": ["p"], "config": {},
                "provides": ["s", "h"], "proxies": {}, "probe": [],
                "serve": {"socket": str(self.dir / "s.sock"), "keys": ["s"],
                          "methods": methods}}
        super().__init__(binary, spec, self.dir, "provider")

    def close(self):
        super().close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture(scope="module")
def provider():
    p = _Provider({"s": ["bump", "twice"]})
    yield p
    p.close()


def test_an_exported_key_and_declared_method_is_answered(provider):
    assert provider.call("s", "bump", [41]) == {"ok": True, "value": 42}


def test_a_key_the_process_provides_but_does_not_export_is_refused(provider):
    """`h` is provided in this process and nobody consumes it across a seam.
    On the base this answered 200."""
    assert provider.call("h", "peek", [2]) == {
        "ok": False, "error": "key 'h' is not exported by this process"}


def test_a_key_nobody_provides_is_refused_not_answered_with_null(provider):
    """On the base: `{"ok": true, "value": null}`."""
    assert provider.call("nope", "bump", [1]) == {
        "ok": False, "error": "key 'nope' is not exported by this process"}


def test_a_method_the_service_does_not_declare_is_refused_not_null(provider):
    """On the base: `{"ok": true, "value": null}`."""
    assert provider.call("s", "peek", [2]) == {
        "ok": False,
        "error": "method 'peek' is not exported for key 's' (exported: bump, twice)"}


def test_the_methods_allowlist_is_read_not_just_the_service():
    """`twice` is an operation of `S`, so the generated dispatch would run it.
    The spec's `serve.methods` lists `bump` only, and the runner holds to it."""
    p = _Provider({"s": ["bump"]})
    try:
        assert p.call("s", "twice", [4]) == {
            "ok": False, "error": "method 'twice' is not exported for key 's' (exported: bump)"}
        assert p.call("s", "bump", [4]) == {"ok": True, "value": 5}
    finally:
        p.close()


# --------------------------------------------------------------------------
# the relay case: a key this process only holds a PROXY for
# --------------------------------------------------------------------------

RELAY_SOURCE = """\
service K { fn get(x: Int) -> Int }
service J { fn ask(x: Int) -> Int }
component Store provides k: K { provide k { fn get(x) { return x + 1000 } } }
component User requires k: K provides j: J {
  provide j { fn ask(x) { return k.get(x) } }
}
"""


def test_a_key_held_only_as_a_proxy_is_not_relayed():
    """`store` serves `k`. `user` consumes `k` through a proxy and serves only
    `j`. A raw call for `k` on `user`'s socket used to resolve the proxy and
    cross on to `store`, answering 1001 under `user`'s identity: a relay
    through a process the placement never made a provider of `k`."""
    workdir = _short_dir()
    binary = placement._build_rust(compile_source(RELAY_SOURCE, "r.rvl"), workdir)
    store_sock = str(workdir / "store.sock")
    store = _Runner(binary, {
        "name": "store", "components": ["store"], "config": {}, "provides": ["k"],
        "proxies": {}, "probe": [],
        "serve": {"socket": store_sock, "keys": ["k"], "methods": {"k": ["get"]}},
    }, workdir, "store")
    user = None
    try:
        user = _Runner(binary, {
            "name": "user", "components": ["user"], "config": {}, "provides": ["j"],
            "proxies": {"k": {"socket": store_sock, "methods": ["get"], "service": "K"}},
            "probe": [],
            "serve": {"socket": str(workdir / "user.sock"), "keys": ["j"],
                      "methods": {"j": ["ask"]}},
        }, workdir, "user")
        assert user.call("j", "ask", [1]) == {"ok": True, "value": 1001}   # the seam works
        assert user.call("k", "get", [1]) == {
            "ok": False, "error": "key 'k' is not exported by this process"}
    finally:
        if user is not None:
            user.close()
        store.close()
        shutil.rmtree(workdir, ignore_errors=True)

