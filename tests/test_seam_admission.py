"""Peer admission on a seam — what a UDS seam seals, and what a network seam
can honestly claim instead (roadmap item 118 §1.4b / 421 F8).

`CorrelationGuard` (tests/test_deploy_118.py) is four gates: shape, a CLOSED
peer table, an HMAC under that peer's own per-boot secret, and a replay ledger.
On a local UDS seam the transport authenticates nothing, so the HMAC is the only
thing binding the claimed identity to a real caller — that is "sealed".

A TCP+mTLS seam cannot carry it. The secret is minted per boot by ONE conductor
and handed only to its own children; an item-151 cross-composition consumer runs
under another one and can never hold it, so demanding a sealed envelope over the
network refuses the legitimate caller rather than the stranger. What mTLS does
prove, per session and with a CA-signed key, is WHICH identity is calling; what
was missing is a closed set to check that against, because `CERT_REQUIRED`
against a shared CA answers every identity that CA ever signed.

So this suite pins three things:

  1. `PeerAllowlist` is that closed set, and it holds NAMES — no secret crosses a
     composition boundary, which is what lets it hold where sealing cannot;
  2. over a real TCP+mTLS `bridge.serve`, a stranger holding a perfectly valid
     CA-signed certificate is refused BEFORE the service is dispatched, and a
     declared peer still round-trips;
  3. the level a seam ACHIEVED is reported — `sealed`, `peer-pinned`, or
     UNVERIFIED — so a weaker property is never shipped under a stronger name.

Every socket here is on an EPHEMERAL port (bind :0, close, reuse the number);
this suite adds no fixed port to the tree.
"""

from __future__ import annotations

import asyncio
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
    """backends/python/bridge.py, imported directly (needs no cordis); same
    sys.modules-before-exec dance as tests/test_network_placement.py."""
    name = "revl_seam_admission_test_bridge"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge = _bridge()

from revl import deploy, placement as _placement  # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 1. the allowlist itself: names, not secrets
# ---------------------------------------------------------------------------


def test_a_declared_peer_is_admitted_and_an_undeclared_one_is_not():
    allow = deploy.PeerAllowlist(["consumer", "sibling"])
    assert allow.admit("consumer") == (True, "peer-pinned")
    assert allow.admit("sibling")[0] is True
    ok, reason = allow.admit("stranger")
    assert ok is False and reason == deploy.REJECT_UNKNOWN_PEER


def test_a_peer_the_transport_never_authenticated_is_refused_distinctly():
    """`None` is not "some identity I do not know", it is "no identity at all"
    — a plaintext or certless session. The reasons stay distinct because the
    two are distinct problems."""
    allow = deploy.PeerAllowlist(["consumer"])
    assert allow.admit(None) == (False, deploy.REJECT_UNAUTHENTICATED_PEER)
    assert allow.admit("") == (False, deploy.REJECT_UNAUTHENTICATED_PEER)


def test_the_allowlist_never_claims_to_dedup():
    """The property a network seam CANNOT carry stays uncarried: the same peer
    calling twice is admitted twice. A replay check would need an envelope, and
    an off-placement peer has no secret to seal one with."""
    allow = deploy.PeerAllowlist(["consumer"])
    assert allow.admit("consumer")[0] is True
    assert allow.admit("consumer")[0] is True
    assert not hasattr(allow, "ledger")


# ---------------------------------------------------------------------------
# 2. the achieved level is SAID, including when it is nothing
# ---------------------------------------------------------------------------


def test_an_unverified_seam_says_so_rather_than_staying_quiet():
    lines = deploy.render_seam_admissions([
        deploy.SeamAdmission("db", "uds", deploy.ADMISSION_SEALED, "sealed here",
                             peers=("edge",)),
        deploy.SeamAdmission("edge", "tcp+mtls", deploy.ADMISSION_UNVERIFIED,
                             "declares no peers"),
    ])
    rendered = "\n".join(lines)
    assert "UNVERIFIED" in rendered
    assert "1 of 2 seam(s) UNVERIFIED" in rendered
    # and the level that DID hold is not dressed up as the stronger one
    assert deploy.ADMISSION_SEALED in rendered
    assert deploy.ADMISSION_PEER_PINNED not in rendered


def test_no_seams_renders_no_admission_block():
    assert deploy.render_seam_admissions([]) == []


# ---------------------------------------------------------------------------
# 3. the load-bearing one: a real TCP+mTLS serve refuses a CA-signed stranger
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    out = tmp_path_factory.mktemp("admission_certs")
    return _placement.generate_seam_certs(
        out, ["provider", "consumer", "stranger"], ("127.0.0.1", "localhost"))


class _Ctx:
    def __init__(self, table):
        self._table = table

    def get(self, key):
        return self._table.get(key)


class _Counter:
    """Records every dispatch, so a test can prove a refused peer never got
    past the connection gate."""

    def __init__(self):
        self.calls: list = []

    def get(self, name):
        self.calls.append(name)
        return f"v:{name}"


class _NetSeam:
    """A real `bridge.serve` over TCP+mTLS on its own event-loop thread."""

    def __init__(self, certs, port, peers=None):
        tls = bridge.TlsConfig(certs["provider"]["cert"], certs["provider"]["key"],
                               certs["provider"]["ca"], identity="provider")
        endpoint = bridge.Endpoint(host="127.0.0.1", port=port, tls=tls)
        self.service = _Counter()
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self._loop)
            server = self._loop.run_until_complete(bridge.serve(
                _Ctx({"Cache": self.service}), {"Cache": ["get"]}, endpoint,
                peers=peers))
            ready.set()
            self._loop.run_forever()
            server.close()
            self._loop.run_until_complete(server.wait_closed())
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(10)

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def _dial(certs, identity, port):
    """One raw mTLS client speaking the bridge's JSON-line wire, so the test
    reads the provider's refusal reply verbatim instead of through a proxy."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certs[identity]["cert"], certs[identity]["key"])
    ctx.load_verify_locations(certs[identity]["ca"])
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(5.0)
    raw.connect(("127.0.0.1", port))
    return ctx.wrap_socket(raw, server_hostname="localhost")


def _call(conn, key="Cache", method="get", args=("a",)):
    io = conn.makefile("rwb")
    io.write((json.dumps({"key": key, "method": method,
                          "args": list(args)}) + "\n").encode())
    io.flush()
    line = io.readline()
    return json.loads(line) if line else None


def test_a_ca_signed_stranger_is_refused_before_the_service_is_dispatched(certs):
    """The exposure this closes: mTLS with `CERT_REQUIRED` against a shared CA
    admits EVERY identity that CA ever signed. `stranger` holds a certificate as
    valid as `consumer`'s — same CA, same EKUs — and is refused anyway, because
    the placement never declared it may call."""
    port = _free_port()
    seam = _NetSeam(certs, port, peers=deploy.PeerAllowlist(["consumer"]))
    try:
        conn = _dial(certs, "stranger", port)
        try:
            reply = _call(conn)
        finally:
            conn.close()
        assert reply is not None, "the stranger got no reply at all"
        assert reply["ok"] is False
        assert reply["peer_refused"] == deploy.REJECT_UNKNOWN_PEER
        assert seam.service.calls == []          # never dispatched
    finally:
        seam.stop()


def test_a_declared_peer_still_round_trips_over_the_same_seam(certs):
    """The other half: closing the set must not close the seam. Without this the
    suite would pass on a provider that refuses everyone."""
    port = _free_port()
    seam = _NetSeam(certs, port, peers=deploy.PeerAllowlist(["consumer"]))
    try:
        conn = _dial(certs, "consumer", port)
        try:
            assert _call(conn) == {"ok": True, "value": "v:a"}
        finally:
            conn.close()
        assert seam.service.calls == ["a"]
    finally:
        seam.stop()


def test_without_an_allowlist_the_seam_is_byte_identical_to_before(certs):
    """Back-compat, and the honest statement of the default: absent `peers`,
    the stranger IS answered. That is the behaviour the conductor reports as
    UNVERIFIED rather than leaving unsaid."""
    port = _free_port()
    seam = _NetSeam(certs, port, peers=None)
    try:
        conn = _dial(certs, "stranger", port)
        try:
            assert _call(conn) == {"ok": True, "value": "v:a"}
        finally:
            conn.close()
        assert seam.service.calls == ["a"]
    finally:
        seam.stop()


# ---------------------------------------------------------------------------
# 4. the conductor: what it wires, and what it says it achieved
# ---------------------------------------------------------------------------


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


class _StubProc:
    """Minimal Popen stand-in (mirrors tests/test_network_placement.py)."""

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


def _conduct(tmp_path, monkeypatch, placement_text):
    """Run the real `run_placement` once with the child runners stubbed, so the
    test reads the specs the conductor actually wrote and the report it printed.
    Nothing binds a port: the addresses below are ephemeral numbers the
    conductor's latency probe simply finds unreachable."""
    procs: dict = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        # only stub the child runner spawns; openssl (the test certs) is real
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
    plc = tmp_path / "seam.toml"
    plc.write_text(placement_text, encoding="utf-8")
    rc = _placement.run_placement([str(app)], str(plc), once=True)
    return rc, procs


def _net_toml(port, extra_provider="") -> str:
    return f"""
generate_test_certs = true

[processes.provider]
components = ["MemCache"]
{extra_provider}
[processes.provider.address]
host = "127.0.0.1"
port = {port}
rtt_ms = 0.3
[processes.provider.tls]
identity = "provider"

[processes.consumer]
components = ["Consumer"]
[processes.consumer.tls]
identity = "consumer"
"""


def test_a_network_provider_with_no_declared_peers_reports_unverified(
        tmp_path, monkeypatch, capsys):
    """The default is unchanged — and it is SAID. Before this, a network seam
    that answered every CA-signed identity said nothing at all about it."""
    rc, procs = _conduct(tmp_path, monkeypatch, _net_toml(_free_port()))
    assert rc == 0, rc
    serve = procs["provider"].spec["serve"]
    assert "peers" not in serve            # nothing invented
    out = capsys.readouterr().out
    assert "seam admission provider (tcp+mtls): UNVERIFIED" in out
    assert "declares no `peers`" in out
    assert "1 of 1 seam(s) UNVERIFIED" in out


def test_declared_peers_reach_the_provider_spec_and_report_peer_pinned(
        tmp_path, monkeypatch, capsys):
    """An operator naming an off-placement caller gets a closed set that still
    contains this placement's own consumers — declaring `partner` must not lock
    out `consumer`, which would be a self-inflicted outage on a security fix."""
    rc, procs = _conduct(tmp_path, monkeypatch,
                         _net_toml(_free_port(), 'peers = ["partner"]'))
    assert rc == 0, rc
    serve = procs["provider"].spec["serve"]
    assert serve["peers"] == ["consumer", "partner"]
    # names only: a secret never crosses a composition boundary, which is the
    # whole reason this holds where the correlation guard cannot
    assert "correlation" not in serve
    out = capsys.readouterr().out
    assert "seam admission provider (tcp+mtls): peer-pinned" in out
    assert "peers: consumer, partner" in out
    assert "UNVERIFIED" not in out


def test_an_empty_peer_list_closes_the_seam_to_this_composition_alone(
        tmp_path, monkeypatch):
    """`peers = []` is not "no allowlist", it is "only my own consumers" — the
    spelling for a network provider that no other composition may dial."""
    rc, procs = _conduct(tmp_path, monkeypatch, _net_toml(_free_port(), "peers = []"))
    assert rc == 0, rc
    assert procs["provider"].spec["serve"]["peers"] == ["consumer"]


def test_a_peer_list_that_could_admit_nobody_is_refused(tmp_path, monkeypatch, capsys):
    """A seam nobody could ever call is a configuration error, not a very secure
    seam. `MemCache` alone in a placement has no local network consumer, so an
    empty list there admits no caller at all."""
    solo = f"""
generate_test_certs = true

[processes.provider]
components = ["MemCache", "Consumer"]
peers = []
[processes.provider.address]
host = "127.0.0.1"
port = {_free_port()}
[processes.provider.tls]
identity = "provider"
"""
    rc, _ = _conduct(tmp_path, monkeypatch, solo)
    assert rc != 0
    err = capsys.readouterr().err
    assert "no caller could ever be admitted" in err


def test_peers_on_a_process_with_no_address_is_refused(tmp_path, monkeypatch, capsys):
    """A peer allowlist is the NETWORK seam's admission check. Accepting it
    silently on a UDS process would be a setting that reads as a control and
    enforces nothing."""
    local = """
[processes.provider]
components = ["MemCache"]
peers = ["consumer"]

[processes.consumer]
components = ["Consumer"]
"""
    rc, _ = _conduct(tmp_path, monkeypatch, local)
    assert rc != 0
    err = capsys.readouterr().err
    assert "declares `peers` but no `address`" in err


def test_a_declared_peer_must_be_a_declared_operator(tmp_path, monkeypatch, capsys):
    """Same rule the network identities themselves already obey (item 55): with
    an `operator_profile` configured, who may call is drawn from the operator
    model, not from an ad-hoc string."""
    profile = tmp_path / "ops.txt"
    profile.write_text("operator provider may swap on *\n"
                       "operator consumer may swap on *\n", encoding="utf-8")
    path = str(profile).replace("\\", "/")
    toml = (f'operator_profile = "{path}"\n'
            + _net_toml(_free_port(), 'peers = ["partner"]'))
    rc, _ = _conduct(tmp_path, monkeypatch, toml)
    assert rc != 0
    err = capsys.readouterr().err
    assert "allows peer identity 'partner'" in err
    assert "not a declared operator" in err


def test_a_local_uds_seam_still_reports_sealed(tmp_path, monkeypatch, capsys):
    """The unchanged half: a py->py UDS seam still gets the correlation guard,
    and the report names the level it achieved rather than only its absence."""
    local = """
[processes.provider]
components = ["MemCache"]

[processes.consumer]
components = ["Consumer"]
"""
    rc, procs = _conduct(tmp_path, monkeypatch, local)
    assert rc == 0, rc
    assert "correlation" in procs["provider"].spec["serve"]
    out = capsys.readouterr().out
    assert "seam admission provider (uds): sealed" in out
    assert "UNVERIFIED" not in out
