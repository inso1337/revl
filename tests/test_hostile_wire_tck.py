"""Hostile-wire conformance suite: the seam-envelope TCK section (issue #475).

A conformance section that pins how the SEALED SEAM ENVELOPE behaves when the
wire underneath it is adversarial — truncated, reordered, duplicated, bounced,
or partitioned mid-envelope. The shapes are protocol-agnostic on purpose: the
envelope is a value with a rule (`revl.deploy.Correlation` + `CorrelationGuard`),
and the rule must hold no matter which transport carried the bytes.

Seam entry point under test
---------------------------
The EXISTING sealed seam, at two altitudes, so a shape is pinned once as a pure
property of the envelope and again end to end over a real socket:

  * **protocol-agnostic** — `revl.deploy.CorrelationGuard.admit(wire)` over an
    envelope produced by `deploy.seal(deploy.Correlation(...), secret)`. This is
    the sealed UDS seam's authenticator with no socket in the picture, so a
    property proven here is a property of the envelope, not of a transport.
  * **system level** — a real `bridge.serve` UDS provider (the sealed local
    seam) running that same `CorrelationGuard`, dialled by a RAW Unix-socket
    client this file drives byte by byte, so it can truncate a line, resend a
    frame, or drop a connection mid-envelope — things `bridge._Client` will not
    do for you.

Why the UDS seam and not a network one: item 421 F8's network seam is still in
design (#107 T3), so the sealed envelope the conductor actually wires today is
the local UDS one (`_process_runner.py` installs `CorrelationGuard` there). This
section lands the adversarial corpus against THAT seam so the network seam is
build-to-test against it later, exactly as the issue asks. It is one slice of
the larger suite: the network-transport reconnect-storm and the coverage pin
named in the issue's "Done" ride with the F8 seam when it exists.

One capability the sealed envelope does NOT have is called out honestly as an
`xfail` rather than faked: it carries no per-crossing sequence number, so the
seam cannot ENFORCE an ordering — it can only CORRELATE order-free, which is the
property the reorder tests actually pin.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import deploy  # noqa: E402


def _bridge():
    """backends/python/bridge.py, imported by path (it needs no cordis) — the
    same sys.modules-before-exec dance as tests/test_deploy_118.py."""
    name = "revl_hostile_wire_test_bridge"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- the sealed envelope and its guard -------------------------------------

DB_SECRET = b"secret-for-db-" + b"0" * 18
EDGE_SECRET = b"secret-for-edge-" + b"0" * 16
SECRETS = {"db": DB_SECRET, "edge": EDGE_SECRET}


def _envelope(identity, *, effect="Cache.get", key="idem-1", generation=7,
              composition="app"):
    return deploy.Correlation(composition_id=composition, generation=generation,
                              peer_identity=identity, effect_id=effect,
                              realm="prod", idempotency_key=key)


def _guard():
    return deploy.CorrelationGuard(dict(SECRETS))


def _sealed(identity, **kw):
    """A well-formed sealed wire envelope for `identity`, HMAC'd under its own
    per-process secret — a genuine crossing this seam should admit."""
    return deploy.seal(_envelope(identity, **kw), SECRETS[identity])


REQUIRED_MEMBERS = ("composition_id", "generation", "peer_identity", "effect_id")


# ---------------------------------------------------------------------------
# Section A — protocol-agnostic property tests over the sealed envelope
#
# No socket: the input is a sealed wire dict and the oracle is
# `CorrelationGuard.admit`. A property here is a property of the ENVELOPE.
# ---------------------------------------------------------------------------


def _rng(tag: str) -> random.Random:
    """A per-test deterministic PRNG, so a red is reproducible from the name."""
    return random.Random(f"hostile-wire::{tag}")


def _random_envelope(rng: random.Random) -> tuple[str, dict]:
    identity = rng.choice(("db", "edge"))
    wire = _sealed(identity,
                   effect=f"Cache.{rng.choice(('get', 'put', 'drop'))}",
                   key=f"k{rng.randrange(1_000_000)}",
                   generation=rng.randrange(0, 32),
                   composition=rng.choice(("app", "app-2", "other-comp")))
    return identity, wire


def test_truncated_sealed_envelope_refuses_and_leaves_no_residue():
    """A sealed envelope with bytes/members removed must REFUSE, and the refusal
    must record NOTHING — a truncated attempt that quietly seated its dedup
    scope would let an attacker pre-poison a key an honest peer will later use.

    Three truncations, over many random envelopes:
      * a required member dropped -> shape gate, MALFORMED;
      * the auth tag cut to a random-length prefix -> FORGED;
      * a non-auth member dropped -> the HMAC no longer covers these bytes,
        FORGED.
    After each, the INTACT envelope for the same scope is presented to the SAME
    guard and must still be admitted first-time — proving the refusal left no
    ledger residue behind it."""
    rng = _rng("truncate")
    accept = {deploy.REJECT_MALFORMED, deploy.REJECT_FORGED}
    trials = 0
    for _ in range(200):
        identity, intact = _random_envelope(rng)

        # (1) drop a required member
        broke = dict(intact)
        del broke[rng.choice(REQUIRED_MEMBERS)]
        # (2) truncate the auth tag to a shorter prefix (or empty)
        tag = intact[deploy.AUTH_FIELD]
        cut = dict(intact)
        cut[deploy.AUTH_FIELD] = tag[:rng.randrange(0, len(tag))]
        # (3) drop a non-auth, non-required member so the HMAC stops matching
        droppable = [k for k in intact
                     if k not in REQUIRED_MEMBERS and k != deploy.AUTH_FIELD]
        thin = dict(intact)
        del thin[rng.choice(droppable)]

        for hostile in (broke, cut, thin):
            guard = _guard()
            ok, reason = guard.admit(hostile)
            assert ok is False, (hostile, reason)
            assert reason in accept, reason
            # no residue: the honest crossing for this scope is still fresh
            ok2, reason2 = guard.admit(intact)
            assert ok2 is True, (reason2, "truncated attempt left residue")
            assert len(guard.ledger) == 1  # only the honest crossing is seated
            trials += 1
    assert trials >= 500  # the property was actually exercised, not skipped


def test_reordered_correlated_calls_correlate_by_envelope_not_arrival_order():
    """A reordered PAIR of correlated calls must never be silently mis-accepted.
    The sealed seam correlates off the envelope, not off arrival position, so:

      * two DISTINCT crossings admit in EITHER order (no order-accept coupling);
      * a crossing and its exact DUPLICATE: whichever copy arrives second is
        refused DUPLICATE, no matter which order the pair is delivered in — there
        is no arrival-order path that admits a replay."""
    rng = _rng("reorder")
    for _ in range(200):
        identity = rng.choice(("db", "edge"))
        a = _sealed(identity, effect="Cache.get", key=f"a{rng.randrange(1<<30)}")
        b = _sealed(identity, effect="Cache.put", key=f"b{rng.randrange(1<<30)}")

        # distinct crossings: order-free, both admit
        g1 = _guard()
        first, second = (a, b) if rng.random() < 0.5 else (b, a)
        assert g1.admit(first)[0] is True
        assert g1.admit(second)[0] is True

        # a crossing and its exact duplicate: the SECOND is refused, in any order
        dup = dict(a)  # byte-identical replay
        g2 = _guard()
        pair = [a, dup]
        rng.shuffle(pair)  # "reorder" the delivery of the two identical frames
        assert g2.admit(pair[0])[0] is True
        ok, reason = g2.admit(pair[1])
        assert (ok, reason) == (False, deploy.REJECT_DUPLICATE)


@pytest.mark.xfail(reason="the sealed Correlation envelope carries no "
                          "per-crossing sequence number, so the seam can "
                          "CORRELATE order-free but cannot ENFORCE ordering; "
                          "true in-order-delivery enforcement is out of scope "
                          "until a seq field lands (issue #421 F8 / #107 T3)",
                   strict=True)
def test_sealed_envelope_exposes_a_monotonic_sequence_for_order_enforcement():
    """Documents the ONE reorder capability the seam does not have, greppably and
    without faking a pass. If a per-crossing sequence number is ever added to
    `Correlation`, this flips to XPASS and is promoted to a real ordering test."""
    fields = set(deploy.Correlation.__dataclass_fields__)
    assert "sequence" in fields or "seq" in fields


def test_duplicated_envelope_is_never_silently_deduped_by_the_envelope_layer():
    """The load-bearing assertion of the whole section: the ENVELOPE layer does
    no dedup of its own, ever. Dedup is a decision the guard makes ONLY when the
    crossing declares itself re-deliverable with an idempotency key (item 309),
    and it is a VISIBLE verdict, not a swallowed frame.

      * an UNKEYED envelope duplicated -> BOTH admit (nothing to dedup on);
      * a KEYED envelope duplicated -> the second is refused with an explicit
        DUPLICATE reason, and the ledger seated exactly one scope."""
    rng = _rng("duplicated")
    for _ in range(200):
        identity = rng.choice(("db", "edge"))

        # unkeyed: no silent dedup, both admitted
        unkeyed = _sealed(identity, key=None,
                          effect=f"Cache.{rng.choice(('get', 'put'))}")
        g = _guard()
        assert g.admit(unkeyed)[0] is True
        assert g.admit(dict(unkeyed))[0] is True, "unkeyed duplicate was deduped"
        assert len(g.ledger) == 0  # nothing keyed, nothing seated

        # keyed: the duplicate is an explicit refusal, not a silent drop
        keyed = _sealed(identity, key=f"k{rng.randrange(1<<30)}")
        g2 = _guard()
        assert g2.admit(keyed)[0] is True
        ok, reason = g2.admit(dict(keyed))
        assert (ok, reason) == (False, deploy.REJECT_DUPLICATE)
        assert len(g2.ledger) == 1


# ---------------------------------------------------------------------------
# Section B — the same shapes end to end over a real sealed UDS seam
#
# A raw Unix-socket client drives the wire byte by byte against a real
# `bridge.serve` provider running the correlation guard, so a truncation, a
# resent frame, a dropped connection are exercised on the actual transport.
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, table):
        self._table = table

    def get(self, key):
        return self._table.get(key)


class _Counter:
    """Records every dispatch, so a test can prove a refused/partial envelope
    never reached the service."""

    def __init__(self):
        self.calls: list = []

    def get(self, name):
        self.calls.append(name)
        return f"v:{name}"


class _Seam:
    """A `bridge.serve` UDS provider on its own event-loop thread, guarded by a
    `CorrelationGuard`, torn down cleanly (mirrors tests/test_deploy_118.py)."""

    def __init__(self, bridge, sock, service, guard):
        self.bridge, self.sock, self.service = bridge, sock, service
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self._loop)
            server = self._loop.run_until_complete(bridge.serve(
                _Ctx({"Cache": service}), {"Cache": ["get"]}, sock,
                correlation=guard))
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


@pytest.fixture
def sockdir():
    """A short-pathed directory for Unix sockets (the macOS ``sun_path`` limit;
    same rationale as tests/test_deploy_118.py)."""
    directory = tempfile.mkdtemp(prefix="rvhw", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def seam(sockdir):
    bridge = _bridge()
    service = _Counter()
    running = _Seam(bridge, str(sockdir / "s.sock"), service, _guard())
    try:
        yield running.sock, service
    finally:
        running.stop()


def _dial(sock_path: str) -> socket.socket:
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(5.0)
    raw.connect(sock_path)
    return raw


def _request_bytes(sealed, *, key="Cache", method="get", args=("a",)) -> bytes:
    req = {"key": key, "method": method, "args": list(args)}
    if sealed is not None:
        req["correlation"] = sealed
    return (json.dumps(req) + "\n").encode()


def _call_raw(sock_path, sealed, **kw):
    """One full request on a fresh connection; returns the parsed reply."""
    conn = _dial(sock_path)
    try:
        io = conn.makefile("rwb")
        io.write(_request_bytes(sealed, **kw))
        io.flush()
        line = io.readline()
        return json.loads(line) if line else None
    finally:
        conn.close()


def test_a_duplicated_frame_is_not_deduped_by_the_transport(seam):
    """The frame layer does NO dedup of its own — proven on the real wire.

      * the same UNKEYED frame sent twice is dispatched twice (the transport did
        not swallow the repeat);
      * the same KEYED frame sent twice yields ONE dispatch and an explicit
        `correlation_refused: duplicate-envelope` reply on the second — a
        verdict the consumer can see, never a silently dropped frame."""
    sock, service = seam

    unkeyed = _sealed("db", key=None)
    assert _call_raw(sock, unkeyed)["ok"] is True
    assert _call_raw(sock, unkeyed)["ok"] is True
    assert service.calls == ["a", "a"], "the transport deduped an unkeyed frame"

    keyed = _sealed("db", key="frame-dup-1")
    assert _call_raw(sock, keyed)["ok"] is True
    refused = _call_raw(sock, keyed)
    assert refused["ok"] is False
    assert refused["correlation_refused"] == deploy.REJECT_DUPLICATE
    assert service.calls == ["a", "a", "a"], "the keyed replay reached the service"


def test_a_truncated_line_on_the_wire_is_refused_and_dispatches_nothing(seam):
    """A malformed/truncated request line must not be dispatched, and must not
    take the seam down: a following honest call on a fresh connection still
    round-trips. Over `bridge.serve` a line that does not parse as JSON is
    skipped without a reply (fail-closed), so the observable proof is that the
    service was never entered and the seam still serves."""
    sock, service = seam
    rng = _rng("wire-truncate")
    for _ in range(20):
        payload = _request_bytes(_sealed("db", key=f"t{rng.randrange(1<<30)}"))
        cut = rng.randrange(1, len(payload) - 1)  # a partial, truncated JSON body
        conn = _dial(sock)
        try:
            # newline-terminated so the server reads a COMPLETE but invalid line;
            # it is skipped fail-closed (no reply), so we do not read one back.
            conn.sendall(payload[:cut] + b"\n")
        finally:
            conn.close()
    assert service.calls == [], "a truncated line reached the service"
    # the seam survived every truncation: an honest crossing still round-trips
    assert _call_raw(sock, _sealed("db", key="after-truncate"))["ok"] is True
    assert service.calls == ["a"]


def test_a_partition_mid_envelope_refuses_and_leaves_no_residue(seam):
    """The connection is severed in the MIDDLE of an envelope — no trailing
    newline, then the socket drops. The provider must treat the partial as a
    non-crossing: never dispatched, and no ledger residue, so the honest peer
    can still make the very crossing whose fragment was seen. Fail-closed, since
    there is no complete request to answer with a refusal."""
    sock, service = seam
    rng = _rng("partition")
    scope_key = "partitioned-crossing"
    full = _request_bytes(_sealed("db", key=scope_key))
    for _ in range(20):
        cut = rng.randrange(1, len(full) - 1)
        conn = _dial(sock)
        try:
            conn.sendall(full[:cut])  # half an envelope, NO newline
        finally:
            conn.close()             # partition mid-envelope
    assert service.calls == [], "a partitioned fragment was dispatched"
    # no residue: the SAME scope, now delivered whole, is admitted first-time
    reply = _call_raw(sock, _sealed("db", key=scope_key))
    assert reply["ok"] is True, reply
    assert service.calls == ["a"]


def test_a_reconnect_storm_mid_flight_refuses_cleanly_and_leaves_no_residue(seam):
    """A consumer that BOUNCES the connection mid-flight, over and over: some
    connections send nothing, some send a partial envelope and drop, some send a
    whole keyed crossing and hang up abruptly. After the storm the seam must be
    intact, have dispatched EXACTLY the whole crossings (no partial slipped
    through, no crossing double-counted), and still admit a fresh crossing."""
    sock, service = seam
    rng = _rng("reconnect-storm")
    expected = 0
    for i in range(90):
        kind = rng.choice(("silent", "partial", "whole"))
        conn = _dial(sock)
        try:
            if kind == "silent":
                pass  # open and drop
            elif kind == "partial":
                payload = _request_bytes(_sealed("db", key=f"s{i}"))
                conn.sendall(payload[:max(1, len(payload) // 2)])  # no newline
            else:  # whole crossing, then hang up without waiting for the reply
                conn.sendall(_request_bytes(_sealed("db", key=f"w{i}")))
                expected += 1
                if rng.random() < 0.5:
                    # sometimes read the reply, sometimes bounce before reading
                    conn.settimeout(5.0)
                    conn.makefile("rb").readline()
        finally:
            conn.close()
        time.sleep(0.001)

    # the seam is still up and correlating: a brand-new crossing round-trips
    assert _call_raw(sock, _sealed("db", key="post-storm"))["ok"] is True
    dispatched = [c for c in service.calls]
    # every whole crossing ran once; the final post-storm call adds one; no
    # partial or silent bounce ever reached the service.
    assert len(dispatched) == expected + 1, (
        f"dispatched {len(dispatched)} != {expected} whole crossings + 1 probe")
    assert all(c == "a" for c in dispatched)


# ---------------------------------------------------------------------------
# Section C — the section is self-describing: every hostile shape is covered
# ---------------------------------------------------------------------------

HOSTILE_WIRE_SHAPES = (
    "truncated", "reordered", "duplicated", "partition", "reconnect_storm",
)


def test_every_hostile_wire_shape_has_a_test_in_this_section():
    """Coverage pin for the section: each shape the issue enumerates is backed
    by at least one test here, so a shape cannot silently fall out of the suite.
    The reconnect-storm at the network-transport level and the coverage-config
    pin named in issue #475 ride with the F8 network seam (still in design) and
    are out of scope for this UDS slice."""
    names = [n for n in globals() if n.startswith("test_")]
    for shape in HOSTILE_WIRE_SHAPES:
        assert any(shape in n for n in names), f"no test covers shape {shape!r}"
