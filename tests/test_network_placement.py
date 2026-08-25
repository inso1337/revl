"""Network placement — pointing a seam at a machine (roadmap item 56).

Item 54 gave a seam a *deadline*; item 55 gave a process an *identity*. This
item spends both: a seam in the placement map may name a network address
(`host`/`port`) instead of a local process, and the transport becomes TCP +
**mutual TLS** — both ends present a per-process certificate. The deadline,
reactive-withdrawal and canonical-encoding machinery all apply unchanged over
TCP; a network seam without a deadline or without an identity is *refused*.

Like tests/test_seam_deadlines.py this drives the bridge mechanism over real
sockets with a minimal protocol-speaking provider — now AF_INET + TLS — so no
cordis is needed for the transport itself. The placement-parse cases reuse that
suite's stub-conductor pattern. Loopback with generated self-signed **test**
certs (`placement.generate_seam_certs`, openssl) proves the whole path; the
end-to-end run with real cordis processes lives in the manual example.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if shutil.which("openssl") is None:  # the cert minting needs the openssl CLI
    pytest.skip("openssl CLI not available for test cert generation",
                allow_module_level=True)


def _bridge():
    """backends/python/bridge.py, imported directly (needs no cordis). Registered
    in sys.modules before exec so its dataclasses' string annotations resolve
    (py3.14; same rationale as tests/test_seam_deadlines.py::_bridge)."""
    name = "revl_netplace_test_bridge"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge = _bridge()

from revl import placement as _placement  # noqa: E402


# ---------------------------------------------------------------------------
# loopback test certs: one CA, a leaf per identity (server+client EKU, SAN)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    out = tmp_path_factory.mktemp("seam_certs")
    return _placement.generate_seam_certs(out, ["provider", "consumer"])


def _endpoint(certs, identity, port, host="127.0.0.1"):
    tls = bridge.TlsConfig(certs[identity]["cert"], certs[identity]["key"],
                           certs[identity]["ca"], identity=identity,
                           server_hostname=host)
    return bridge.Endpoint(host=host, port=port, tls=tls)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# a TCP + mTLS provider that speaks the bridge wire protocol, controllably
# wedged (like tests/test_seam_deadlines.py::_Provider, over TLS this time)
# ---------------------------------------------------------------------------


class _TlsProvider:
    def __init__(self, certs, port, value="v1", delay=0.0):
        self.value = value
        self.delay = delay
        self.calls = 0
        self._stopping = False
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certs["provider"]["cert"], certs["provider"]["key"])
        ctx.load_verify_locations(certs["provider"]["ca"])
        ctx.verify_mode = ssl.CERT_REQUIRED  # mutual: demand the client's cert
        self._ctx = ctx
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(16)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while not self._stopping:
            try:
                raw, _ = self._srv.accept()
            except OSError:
                break
            try:
                conn = self._ctx.wrap_socket(raw, server_side=True)
            except (ssl.SSLError, OSError):
                raw.close()
                continue  # an anonymous / bad-cert peer never gets a session
            with self._lock:
                if self._stopping:
                    conn.close()
                    break
                self._conns.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        stream = conn.makefile("rwb")
        try:
            for line in stream:
                req = json.loads(line)
                if self.delay:
                    time.sleep(self.delay)
                if self._stopping:
                    break
                self.calls += 1
                value = self.value if req.get("method") == "get" else None
                stream.write((json.dumps({"ok": True, "value": value}) + "\n").encode())
                stream.flush()
        except (OSError, ValueError):
            pass

    def stop(self):
        with self._lock:
            self._stopping = True
            conns = list(self._conns)
        try:
            self._srv.close()
        except OSError:
            pass
        for conn in conns:
            for op in (lambda c: c.shutdown(socket.SHUT_RDWR), lambda c: c.close()):
                try:
                    op(conn)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 1. a TCP + mTLS round-trip: a value crosses cleanly, canonical-encoded
# ---------------------------------------------------------------------------


def test_tcp_mtls_round_trip(certs):
    port = _free_port()
    provider = _TlsProvider(certs, port, value={"$hit": True, "n": [1, 2, 3]})
    client = bridge._Client(_endpoint(certs, "consumer", port), deadline=2.0)
    try:
        # a record with a list crosses by copy over TLS, canonical-encoded
        assert client.call("cache", "get", []) == {"$hit": True, "n": [1, 2, 3]}
        assert provider.calls == 1
    finally:
        client.close()
        provider.stop()


def test_anonymous_client_cannot_use_a_network_seam(certs):
    """mTLS is mutual: a caller with no client certificate is refused. TLS 1.3
    surfaces the missing client cert on first I/O (not at handshake), so the
    proof is that the anonymous caller cannot complete a round-trip."""
    port = _free_port()
    provider = _TlsProvider(certs, port)
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(3.0)
    raw.connect(("127.0.0.1", port))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # trusts the CA but presents no cert
    ctx.load_verify_locations(certs["consumer"]["ca"])
    tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
    try:
        io = tls.makefile("rwb")
        io.write(b'{"key":"cache","method":"get","args":[]}\n')
        with pytest.raises(OSError):   # the server drops the certless peer
            io.flush()
            if not io.readline():
                raise OSError("peer closed the anonymous session")
    finally:
        tls.close()
        provider.stop()


# ---------------------------------------------------------------------------
# 2. the deadline still fires over TCP (a wedged remote provider)
# ---------------------------------------------------------------------------


def test_deadline_fires_over_tcp(certs):
    port = _free_port()
    provider = _TlsProvider(certs, port, delay=2.0)   # alive, but far too slow
    client = bridge._Client(_endpoint(certs, "consumer", port), deadline=0.2)
    lost: list[int] = []
    client.watch(lambda: lost.append(1))
    try:
        t0 = time.monotonic()
        with pytest.raises(bridge.SeamDeadline) as caught:
            client.call("cache", "get", [])
        assert time.monotonic() - t0 < 1.5
        assert caught.value.key == "cache" and caught.value.deadline == 0.2
        assert not isinstance(caught.value, ConnectionError)
        time.sleep(0.2)
        assert lost == [], "a hang over TCP must not read as a peer death"
    finally:
        client.close()
        provider.stop()


# ---------------------------------------------------------------------------
# 3. peer-death withdrawal still fires over TCP
# ---------------------------------------------------------------------------


def test_peer_death_withdraws_over_tcp(certs):
    port = _free_port()
    provider = _TlsProvider(certs, port)
    client = bridge._Client(_endpoint(certs, "consumer", port), deadline=2.0)
    lost: list[int] = []
    client.watch(lambda: lost.append(1))
    try:
        assert client.call("cache", "get", []) == "v1"   # healthy first
        provider.stop()                                    # the remote dies
        deadline = time.time() + 5.0
        while not lost and time.time() < deadline:
            time.sleep(0.02)
        assert lost == [1], "peer death over TCP must fire reactive withdrawal"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 4. the bridge refuses a malpractice network seam (no identity / no deadline)
# ---------------------------------------------------------------------------


def test_network_seam_without_identity_is_refused(certs):
    ep = bridge.Endpoint(host="127.0.0.1", port=1, tls=None)
    with pytest.raises(ValueError, match="identity"):
        bridge.proxy_component("cache", ["get"], ep, deadline=1.0)


def test_network_seam_without_deadline_is_refused(certs):
    ep = _endpoint(certs, "consumer", 1)
    with pytest.raises(ValueError, match="deadline"):
        bridge.proxy_component("cache", ["get"], ep, deadline=None)


def test_local_uds_seam_is_not_network_and_needs_no_cert(tmp_path):
    """Back-compat: the local form is unchanged — a bare path string, or a
    `{"socket": ...}` serve-spec, normalizes to a UDS endpoint with no TLS, and
    is never subject to the network identity/deadline guard. (A live UDS
    round-trip is already covered by tests/test_seam_deadlines.py.)"""
    ep = bridge.Endpoint.from_spec(str(tmp_path / "x.sock"))
    assert ep.is_network is False and ep.path.endswith("x.sock") and ep.tls is None
    ep2 = bridge.Endpoint.from_spec({"socket": str(tmp_path / "y.sock")})
    assert ep2.is_network is False and ep2.tls is None


# ---------------------------------------------------------------------------
# 5. latency: a real per-seam RTT number
# ---------------------------------------------------------------------------


def test_seam_latency_measures_a_real_number():
    port = _free_port()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    try:
        rtt = _placement.seam_latency_ms("127.0.0.1", port, samples=3)
        assert rtt is not None and rtt >= 0.0
    finally:
        srv.close()
    # unreachable endpoint -> None (the conductor then falls back to configured)
    assert _placement.seam_latency_ms("127.0.0.1", port, samples=1, timeout=0.2) is None


# ---------------------------------------------------------------------------
# 6. placement parse: a network address produces a TCP+mTLS seam spec
# ---------------------------------------------------------------------------


class _StubProc:
    """Minimal Popen stand-in (mirrors tests/test_seam_deadlines.py)."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self._lines = [f"[{name}] UP"]
        self._down = False
        self.stdin = self
        self.returncode = 0

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self._lines:
                return self._lines.pop(0)
            if self._down:
                raise StopIteration
            time.sleep(0.005)

    def write(self, _t):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return 0 if self._down else None

    def wait(self, timeout=None):
        self._teardown()
        return 0

    def terminate(self):
        self._teardown()

    def kill(self):
        self._teardown()

    def _teardown(self):
        if not self._down:
            self._lines.append(f"[{self.name}] residue no residue")
            self._lines.append(f"[{self.name}] DOWN")
            self._down = True


_APP = """
service Cache {
  async fn put(k: Str, v: Str) -> Opt[Str]
  async fn get(k: Str) -> Opt[Str]
}
service App { async fn run() -> Opt[Str] }

component MemCache provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  provide cache {
    async fn put(k, v) = m.insert(k, v)
    async fn get(k) = m.get(k)
  }
}
component Consumer requires cache: Cache provides app: App {
  provide app { async fn run() = cache.get("k") }
}
"""


def _run_conductor_once(tmp_path, monkeypatch, placement_text):
    procs: dict = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        # Only stub the *child runner* spawns (a `.spec.json` arg); real
        # subprocesses the conductor shells out to (openssl for the test certs)
        # must still run for real.
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        proc = _StubProc(spec["name"], spec)
        procs[spec["name"]] = proc
        return proc

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    plc = tmp_path / "net.toml"
    plc.write_text(placement_text, encoding="utf-8")
    rc = _placement.run_placement([str(app)], str(plc), once=True)
    return rc, procs


_NET = """
generate_test_certs = true

[processes.provider]
components = ["MemCache"]
[processes.provider.address]
host = "127.0.0.1"
port = 39555
rtt_ms = 0.3
[processes.provider.tls]
identity = "provider"

[processes.consumer]
components = ["Consumer"]
[processes.consumer.tls]
identity = "consumer"
"""


def test_placement_parses_a_network_address_into_a_tcp_mtls_seam(tmp_path, monkeypatch):
    rc, procs = _run_conductor_once(tmp_path, monkeypatch, _NET)
    assert rc == 0, rc
    # the consumer proxies over a network endpoint, not a UDS socket
    proxy = procs["consumer"].spec["proxies"]["cache"]
    assert "socket" not in proxy
    ep = proxy["endpoint"]
    assert ep["host"] == "127.0.0.1" and ep["port"] == 39555
    # it presents the *consumer's* identity (mTLS: the client's own cert) ...
    assert ep["tls"]["identity"] == "consumer"
    # SNI is the cert's DNS SAN, not the raw IP host — an IP literal is not a
    # legal TLS servername (node/RFC 6066 refuse it), so a loopback address
    # dials under "localhost" (item 152).
    assert ep["tls"]["server_hostname"] == "localhost"
    # the minted cert/key/ca paths are wired in (they live under the run's
    # 0700 tmpdir and are torn down with it when the run ends)
    for k in ("cert", "key", "ca"):
        assert ep["tls"][k] and Path(ep["tls"][k]).name.startswith("seam_")
    # ... carries a deadline (item 54) and the configured latency class ...
    assert proxy["deadline"] == _placement.DEFAULT_SEAM_DEADLINE
    assert proxy["latency_ms"] == 0.3
    # ... and the provider serves the same seam over the network with ITS identity
    serve = procs["provider"].spec["serve"]
    assert "socket" not in serve
    assert serve["endpoint"]["port"] == 39555
    assert serve["endpoint"]["tls"]["identity"] == "provider"


_LOCAL = """
[processes.provider]
components = ["MemCache"]
[processes.consumer]
components = ["Consumer"]
"""


def test_local_placement_is_unchanged_and_needs_no_certs(tmp_path, monkeypatch):
    """Full back-compat: with no address the seam is a local UDS — a `socket`,
    no `endpoint`, no cert required."""
    rc, procs = _run_conductor_once(tmp_path, monkeypatch, _LOCAL)
    assert rc == 0, rc
    proxy = procs["consumer"].spec["proxies"]["cache"]
    assert "endpoint" not in proxy and proxy["socket"].endswith(".sock")
    assert "endpoint" not in procs["provider"].spec["serve"]


_NET_NO_IDENTITY = """
generate_test_certs = true
[processes.provider]
components = ["MemCache"]
[processes.provider.address]
host = "127.0.0.1"
port = 39556
[processes.consumer]
components = ["Consumer"]
[processes.consumer.tls]
identity = "consumer"
"""


def test_placement_refuses_a_network_seam_without_a_provider_identity(tmp_path, monkeypatch, capsys):
    rc, _ = _run_conductor_once(tmp_path, monkeypatch, _NET_NO_IDENTITY)
    assert rc != 0
    err = capsys.readouterr().err
    assert "provider" in err and "identity" in err


_NET_OPERATOR = """
generate_test_certs = true
operator_profile = "OPFILE"

[processes.provider]
components = ["MemCache"]
[processes.provider.address]
host = "127.0.0.1"
port = 39557
[processes.provider.tls]
identity = "provider"

[processes.consumer]
components = ["Consumer"]
[processes.consumer.tls]
identity = "intruder"
"""


_NET_NONPY = """
generate_test_certs = true
[processes.provider]
backend = "rust"
components = ["MemCache"]
[processes.provider.address]
host = "127.0.0.1"
port = 39558
[processes.provider.tls]
identity = "provider"
[processes.consumer]
components = ["Consumer"]
[processes.consumer.tls]
identity = "consumer"
"""


def test_network_seam_on_a_nonpy_backend_is_refused(tmp_path, monkeypatch, capsys):
    """The TCP+mTLS transport ships on the py runner in this cut; a network seam
    placed on rust/go/ts/java is refused (those runners read only the local
    `socket` form)."""
    # bypass the runtime preflight (no cargo here) so we reach the config guard
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    rc, _ = _run_conductor_once(tmp_path, monkeypatch, _NET_NONPY)
    assert rc != 0
    err = capsys.readouterr().err
    assert "py-only" in err and "rust" in err


# --------------------------------------------------------------------------
# 5. a ts consumer over the network path (roadmap item 149). The TCP+mTLS
#    *listener* is still py-only (the provider must be py), but the *client*
#    now ships on the node/ts runner too, so a node consumer of a py network
#    provider is allowed — and gets a network `endpoint`, not a local `socket`.
# --------------------------------------------------------------------------

_NET_TS_CONSUMER = """
generate_test_certs = true
[processes.provider]
components = ["MemCache"]
[processes.provider.address]
host = "127.0.0.1"
port = 39560
rtt_ms = 0.3
[processes.provider.tls]
identity = "provider"
[processes.consumer]
backend = "node"
components = ["Consumer"]
[processes.consumer.tls]
identity = "consumer"
"""


def test_ts_consumer_of_a_py_network_provider_is_allowed(tmp_path, monkeypatch):
    """Item 149: a node/ts consumer may cross onto a py provider's TCP+mTLS
    seam. The conductor hands the node consumer a network `endpoint` (its own
    mTLS identity, the provider's host/port, a deadline), exactly as it does a
    py consumer — the node client dials it (backends/typescript/bridge.ts)."""
    # bypass the node runtime preflight (cordis-ts need not be installed to
    # generate + assert the spec); the ts emit itself still runs for real.
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    rc, procs = _run_conductor_once(tmp_path, monkeypatch, _NET_TS_CONSUMER)
    assert rc == 0, rc
    assert procs["consumer"].spec["backend"] == "node"
    proxy = procs["consumer"].spec["proxies"]["cache"]
    assert "socket" not in proxy                     # a network seam, not a UDS
    ep = proxy["endpoint"]
    assert ep["host"] == "127.0.0.1" and ep["port"] == 39560
    assert ep["tls"]["identity"] == "consumer"       # the node client's own cert
    assert proxy["deadline"] == _placement.DEFAULT_SEAM_DEADLINE
    # the py provider still serves the seam with its own identity over the network
    serve = procs["provider"].spec["serve"]
    assert serve["endpoint"]["port"] == 39560
    assert serve["endpoint"]["tls"]["identity"] == "provider"


_NET_RUST_CONSUMER = """
generate_test_certs = true
[processes.provider]
components = ["MemCache"]
[processes.provider.address]
host = "127.0.0.1"
port = 39561
[processes.provider.tls]
identity = "provider"
[processes.consumer]
backend = "rust"
components = ["Consumer"]
[processes.consumer.tls]
identity = "consumer"
"""


def test_network_consumer_on_rust_is_still_refused(tmp_path, monkeypatch, capsys):
    """The TCP+mTLS *client* ships only on the py and node/ts runners (item
    149); a rust/go/java consumer of a network provider is still refused — those
    runners read only the local `socket` form."""
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    rc, _ = _run_conductor_once(tmp_path, monkeypatch, _NET_RUST_CONSUMER)
    assert rc != 0
    err = capsys.readouterr().err
    assert "item 149" in err and "rust" in err and "consumes a network seam" in err


def test_network_identity_must_be_a_declared_operator(tmp_path, monkeypatch, capsys):
    """Identity per process is issued by the operator model (item 55): when a
    placement names an operator_profile, a network process whose identity is not
    a declared operator token is refused."""
    opfile = tmp_path / "ops.txt"
    opfile.write_text("operator provider may swap on *\n"
                      "operator consumer may swap on *\n", encoding="utf-8")
    text = _NET_OPERATOR.replace("OPFILE", str(opfile).replace("\\", "/"))
    rc, _ = _run_conductor_once(tmp_path, monkeypatch, text)
    assert rc != 0
    err = capsys.readouterr().err
    assert "intruder" in err and "operator" in err
