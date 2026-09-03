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
import builtins
import importlib.util
import json
import queue
import re
import shutil
import socket
import ssl
import sys
import tempfile
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

    def __init__(self, certs, port, peers=None, replay=None):
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
                peers=peers, replay=replay))
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


def _call(conn, key="Cache", method="get", args=("a",), correlation=None):
    io = conn.makefile("rwb")
    request = {"key": key, "method": method, "args": list(args)}
    if correlation is not None:
        request["correlation"] = correlation
    io.write((json.dumps(request) + "\n").encode())
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
# 3b. freshness on the same network seam — refused replays, and NO shared secret
# ---------------------------------------------------------------------------
#
# The recorded reason a network seam could not dedup was that dedup needs an
# authenticated identity to scope the key by, and the only identity binder on a
# UDS seam is the per-boot HMAC secret that a cross-composition caller can never
# hold. On TCP+mTLS that premise does not hold: the handshake already bound the
# identity with a CA-signed key. So the scope is available for free, and these
# tests drive it end to end against a REAL provider — the request is captured
# off the wire and re-delivered, never hand-fed to a guard object.


def _envelope(identity, *, composition="composition-of-this-placement",
              key="idem-1", generation=0):
    """The envelope a network consumer stamps: identity, composition, and an
    idempotency key declaring the crossing re-deliverable. **No `auth` member** —
    nothing here is sealed, and nothing needs to be."""
    return deploy.Correlation(composition_id=composition, generation=generation,
                              peer_identity=identity, effect_id="Cache.get",
                              idempotency_key=key).to_wire()


def _replay_seam(certs, port, peers=None):
    return _NetSeam(certs, port, peers=peers,
                    replay=deploy.TransportReplayGuard())


def test_a_replayed_request_on_a_network_seam_is_refused(certs):
    """The gap this closes. `peers` pins WHO may call and deliberately does not
    dedup, so before this an admitted peer's request could be captured and
    re-delivered verbatim and the provider dispatched it again.

    The replay here is the same request object, re-sent on a FRESH connection —
    a replayer does not have to reuse the socket, and a per-connection ledger
    would be no ledger at all."""
    port = _free_port()
    seam = _replay_seam(certs, port, peers=deploy.PeerAllowlist(["consumer"]))
    try:
        captured = {"key": "Cache", "method": "get", "args": ["a"],
                    "correlation": _envelope("consumer")}
        conn = _dial(certs, "consumer", port)
        try:
            first = _call(conn, correlation=captured["correlation"])
        finally:
            conn.close()
        assert first == {"ok": True, "value": "v:a"}

        replay = _dial(certs, "consumer", port)
        try:
            second = _call(replay, correlation=captured["correlation"])
        finally:
            replay.close()
        assert second["ok"] is False
        assert second["replay_refused"] == deploy.REJECT_DUPLICATE
        # the load-bearing assertion: the service ran ONCE
        assert seam.service.calls == ["a"]
    finally:
        seam.stop()


def test_a_fresh_crossing_from_the_same_peer_is_not_a_replay(certs):
    """The other half, without which the suite would pass on a provider that
    refuses every second call: a distinct idempotency key from the same peer is
    a distinct crossing and is dispatched."""
    port = _free_port()
    seam = _replay_seam(certs, port, peers=deploy.PeerAllowlist(["consumer"]))
    try:
        conn = _dial(certs, "consumer", port)
        try:
            assert _call(conn, correlation=_envelope("consumer", key="k1"))["ok"]
            assert _call(conn, correlation=_envelope("consumer", key="k2"))["ok"]
        finally:
            conn.close()
        assert seam.service.calls == ["a", "a"]
    finally:
        seam.stop()


def test_a_cross_composition_caller_under_another_conductor_is_admitted(certs):
    """The exact failure mode that made the first correlation-guard wiring break
    every cross-tier seam: a guard that demands something the legitimate caller
    cannot produce refuses the caller and nobody else.

    This caller is an item-151 consumer under a DIFFERENT conductor — a foreign
    `composition_id`, and no `auth` tag, because it holds none of this boot's
    secrets and never can. It is admitted and dispatched, and its OWN replay is
    still refused: the protection needed no secret to work on it."""
    port = _free_port()
    seam = _replay_seam(certs, port, peers=deploy.PeerAllowlist(["consumer"]))
    try:
        foreign = _envelope("consumer", composition="a-different-composition")
        assert deploy.AUTH_FIELD not in foreign      # nothing sealed it
        conn = _dial(certs, "consumer", port)
        try:
            assert _call(conn, correlation=foreign) == {"ok": True, "value": "v:a"}
            again = _call(conn, correlation=foreign)
        finally:
            conn.close()
        assert again["replay_refused"] == deploy.REJECT_DUPLICATE
        assert seam.service.calls == ["a"]
    finally:
        seam.stop()


def test_a_captured_envelope_replayed_by_another_peer_is_refused(certs):
    """Why the dedup key is read off the TRANSPORT and not off the payload. A
    replayer controls every byte it sends and controls nothing about the mTLS
    session it sends them in, so `stranger` re-delivering `consumer`'s captured
    envelope is caught by the disagreement between the two — and rewriting the
    envelope to its own name makes it its own first crossing, not a replay of
    someone else's.

    No allowlist here, so the refusal on show is the freshness gate's own and
    not the peer gate's."""
    port = _free_port()
    seam = _replay_seam(certs, port, peers=None)
    try:
        captured = _envelope("consumer")
        conn = _dial(certs, "consumer", port)
        try:
            assert _call(conn, correlation=captured)["ok"] is True
        finally:
            conn.close()

        thief = _dial(certs, "stranger", port)
        try:
            verbatim = _call(thief, correlation=captured)
        finally:
            thief.close()
        assert verbatim["ok"] is False
        assert verbatim["replay_refused"] == deploy.REJECT_PEER_MISMATCH
        assert seam.service.calls == ["a"]           # never dispatched
    finally:
        seam.stop()


def test_a_crossing_that_declares_no_key_is_dispatched_as_before(certs):
    """Back-compat, and the honest statement of the default. A request with no
    envelope at all — every caller built before this one existed — is still
    answered, and is NOT deduplicated: nothing declares it re-deliverable (item
    309), and guessing would be the claim this plane exists to avoid."""
    port = _free_port()
    seam = _replay_seam(certs, port, peers=None)
    try:
        conn = _dial(certs, "consumer", port)
        try:
            assert _call(conn) == {"ok": True, "value": "v:a"}
            assert _call(conn) == {"ok": True, "value": "v:a"}
        finally:
            conn.close()
        assert seam.service.calls == ["a", "a"]
    finally:
        seam.stop()


# ---------------------------------------------------------------------------
# 3c. the ledger is BOUNDED, and says what that costs
# ---------------------------------------------------------------------------


def test_the_replay_ledger_is_bounded_and_counts_what_it_forgot():
    """A provider answers a peer for weeks, so an unbounded `set` is a leak.
    The window is `per_peer` keyed crossings per identity; past that the oldest
    ages out, `evicted` counts it, and a replay of THAT crossing would be
    admitted again — stated here rather than left for an operator to discover."""
    ledger = deploy.BoundedReplayLedger(per_peer=4, max_peers=2)
    scopes = [("c", 0, f"k{i}") for i in range(4)]
    for scope in scopes:
        assert ledger.admit("consumer", scope) is True
    assert len(ledger) == 4
    assert ledger.admit("consumer", scopes[0]) is False    # still remembered
    ledger.admit("consumer", ("c", 0, "k4"))               # pushes the oldest out
    assert ledger.evicted == 1
    assert len(ledger) == 4                                # bounded, not growing
    assert ledger.admit("consumer", scopes[0]) is True     # the honest cost


def test_a_chatty_peer_cannot_age_out_a_quiet_peers_history():
    """Why the bound is per identity. With one shared window, a peer that can
    drive traffic could evict another peer's entries and make room for a
    stolen-key replay of THAT peer's crossing."""
    ledger = deploy.BoundedReplayLedger(per_peer=2, max_peers=8)
    quiet = ("c", 0, "quiet-key")
    assert ledger.admit("quiet", quiet) is True
    for i in range(50):
        ledger.admit("chatty", ("c", 0, f"k{i}"))
    assert ledger.admit("quiet", quiet) is False           # still refused


def test_the_peer_dimension_is_bounded_too():
    """Both dimensions, so the ceiling is a product and not a hope: identities
    are LRU'd as well, and their scopes go with them."""
    ledger = deploy.BoundedReplayLedger(per_peer=2, max_peers=2)
    for name in ("a", "b", "c"):
        ledger.admit(name, ("c", 0, "k"))
    assert len(ledger) == 2
    assert ledger.evicted == 1
    assert ledger.admit("a", ("c", 0, "k")) is True        # forgotten with "a"


def test_a_window_that_remembers_nothing_is_refused_not_accepted():
    with pytest.raises(ValueError):
        deploy.BoundedReplayLedger(per_peer=0)


def test_the_guard_refuses_to_key_on_an_unauthenticated_transport():
    """Fail closed where the premise fails: with no proven identity there is
    nothing to scope a key by, and scoping by the name in the payload would
    dedup in a namespace the caller picks. mTLS `CERT_REQUIRED` makes this
    unreachable on a real network seam, which is exactly why it must not
    silently degrade into trusting the body."""
    guard = deploy.TransportReplayGuard()
    wire = _envelope("consumer")
    assert guard.admit(wire, transport_identity=None) == (
        False, deploy.REJECT_UNAUTHENTICATED_PEER)
    assert guard.admit(wire, transport_identity="") == (
        False, deploy.REJECT_UNAUTHENTICATED_PEER)


def test_the_guard_says_which_verdict_it_reached():
    """A seam never reports a freshness check it did not run: an admitted
    crossing that WAS deduped and one that carried no key are two verdicts."""
    guard = deploy.TransportReplayGuard()
    assert guard.admit(_envelope("c"), transport_identity="c") == (
        True, deploy.ADMITTED_REPLAY_CHECKED)
    unkeyed = deploy.Correlation(composition_id="x", generation=0,
                                 peer_identity="c", effect_id="e").to_wire()
    assert guard.admit(unkeyed, transport_identity="c") == (
        True, deploy.ADMITTED_UNKEYED)
    assert guard.admit(None, transport_identity="c") == (
        True, deploy.ADMITTED_UNKEYED)
    assert guard.admit("not-a-mapping", transport_identity="c") == (
        False, deploy.REJECT_MALFORMED)
    assert guard.admit({"composition_id": "x"}, transport_identity="c") == (
        False, deploy.REJECT_MALFORMED)


def test_an_unhashable_idempotency_key_is_malformed_not_a_crash():
    """The dedup scope is a dict key, so a peer-supplied list or object would
    raise `unhashable type` inside the ledger — past every caller written to
    read an `(ok, reason)` verdict, which takes the seam down rather than
    refusing one envelope (roadmap 428 F10's class). It has no scope, so it is
    malformed. Both guards read the same parse, so both refuse it."""
    wire = _envelope("c")
    wire["idempotency_key"] = ["not", "a", "key"]
    assert deploy.TransportReplayGuard().admit(wire, transport_identity="c") == (
        False, deploy.REJECT_MALFORMED)
    secret = b"s" * 32
    sealed = deploy.seal(deploy.Correlation.from_wire(_envelope("c")), secret)
    sealed["idempotency_key"] = {"nested": "object"}
    ok, reason = deploy.CorrelationGuard({"c": secret}).admit(sealed)
    assert (ok, reason) == (False, deploy.REJECT_MALFORMED)


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


def test_declared_peers_reach_the_provider_spec_and_report_peer_bound(
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
    # the freshness gate rides with it, and holds no secret: `replay` is a
    # window bound, not a key table
    assert serve["replay"] == {"per_peer": deploy.REPLAY_WINDOW_PER_PEER,
                               "max_peers": deploy.REPLAY_WINDOW_PEERS}
    out = capsys.readouterr().out
    assert "seam admission provider (tcp+mtls): peer-bound" in out
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


# ---------------------------------------------------------------------------
# 5. the whole stack: the conductor's OWN wiring refuses a replay
# ---------------------------------------------------------------------------
#
# The tests in §3b drive `bridge.serve` with a guard this file constructs. That
# proves the mechanism and not the WIRING, and a guard that is written into a
# spec and never installed is exactly how the local seam's guard once shipped
# dead (roadmap 421 F8). So this one boots a real network placement through the
# same `run_placement` the CLI calls, constructs no guard, no envelope and no
# `bridge.serve` call of its own, and then dials the published port as the
# admitted consumer and re-delivers a captured request.


_NET_REPLAY_TOML = """
generate_test_certs = true

[processes.provider]
components = ["MemCache"]
peers = ["consumer"]
[processes.provider.address]
host = "127.0.0.1"
port = {port}
rtt_ms = 0.3
[processes.provider.tls]
identity = "provider"

[processes.consumer]
components = ["Consumer"]
probe = ["app.run()"]
[processes.consumer.tls]
identity = "consumer"
"""


def _dial_spec(endpoint) -> ssl.SSLSocket:
    """One raw mTLS client built from the CONDUCTOR's own consumer spec — the
    same cert, CA and servername the placement wired into the proxy."""
    tls = endpoint["tls"]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(tls["cert"], tls["key"])
    ctx.load_verify_locations(tls["ca"])
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(15.0)
    raw.connect((endpoint["host"], endpoint["port"]))
    return ctx.wrap_socket(raw, server_hostname=tls["server_hostname"])


def test_the_ordinary_run_placement_path_leaves_a_live_replay_guard(
        tmp_path, monkeypatch, capfd):
    """Boot a real network placement, then replay a captured request against
    the published port. Nothing here builds a `TransportReplayGuard`: the
    refusal has to come from the guard the conductor wired and the runner
    installed, or it does not come at all."""
    if not _placement._cordis_py_installed():
        pytest.skip("cordis-py runtime not installed (sh backends/python/setup.sh)")

    # the conductor's own run directory, so the test reads the specs it wrote
    # (cert paths and the consumer's correlation block) instead of guessing
    run_dirs: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        made = real_mkdtemp(*args, **kwargs)
        if "revl_placement" in str(kwargs.get("prefix", "")):
            run_dirs.append(made)
        return made

    monkeypatch.setattr(_placement.tempfile, "mkdtemp", spy_mkdtemp)

    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    plc = tmp_path / "net.toml"
    port = _free_port()
    plc.write_text(_NET_REPLAY_TOML.format(port=port), encoding="utf-8")

    # `once=True` tears the placement down the instant boot finishes, before an
    # outside dialer could connect; drive the swap REPL from a queue instead so
    # the provider stays up (same device as tests/test_deploy_118.py).
    commands: queue.Queue = queue.Queue()

    def fake_input(prompt=""):
        item = commands.get()
        if item is None:
            raise EOFError
        return item

    monkeypatch.setattr(builtins, "input", fake_input)
    result: dict = {}

    def run():
        result["rc"] = _placement.run_placement([str(app)], str(plc), once=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 90
        out = ""
        served = None
        while time.time() < deadline:
            out += capfd.readouterr().out
            served = re.search(
                r"\[provider] serve\s*\|.*-> tcp://\S+.*\(peer-pinned: \d+ "
                r"declared peer\(s\) \+ replay-checked\)", out)
            if served or not thread.is_alive():
                break
            time.sleep(0.1)
        assert served, f"provider never reported a replay-checked seam:\n{out}"
        assert run_dirs, "never saw the conductor's run directory"

        spec = json.loads(
            (Path(run_dirs[-1]) / "consumer.spec.json").read_text(encoding="utf-8"))
        proxy = spec["proxies"]["cache"]
        # what the conductor handed the consumer for a NETWORK seam: its own
        # identity and the composition, and NO secret — the whole reason this
        # works for a caller under another conductor
        assert proxy["correlation"]["peer_identity"] == "consumer"
        assert "secret" not in proxy["correlation"]

        captured = {"key": "cache", "method": "get", "args": ["alice"],
                    "correlation": {**proxy["correlation"], "generation": 0,
                                    "effect_id": "cache.get",
                                    "idempotency_key": "captured-crossing"}}
        conn = _dial_spec(proxy["endpoint"])
        try:
            io = conn.makefile("rwb")
            io.write((json.dumps(captured) + "\n").encode())
            io.flush()
            first = json.loads(io.readline())
        finally:
            conn.close()
        assert first["ok"] is True, first

        # the replay: same bytes, new connection, live provider
        replay = _dial_spec(proxy["endpoint"])
        try:
            io = replay.makefile("rwb")
            io.write((json.dumps(captured) + "\n").encode())
            io.flush()
            second = json.loads(io.readline())
        finally:
            replay.close()
        assert second["ok"] is False, second
        assert second.get("replay_refused") == deploy.REJECT_DUPLICATE, second
    finally:
        commands.put(":q")
        thread.join(timeout=60)
        assert not thread.is_alive(), "run_placement did not tear down"
    assert result.get("rc") == 0
