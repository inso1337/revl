"""MCP over HTTP, slice 2: SSE replies and the `subscriptions/listen` stream
(issue #1463).

The claim under test is that a stream carries only its own caller's events:
two callers listening at once never receive each other's notifications, a
progress event reaches only the request it belongs to, a caller who stops
authenticating gets nothing more, and none of it holds the dispatch lock or
fences an E-Stop. Every client here is a real HTTP client on its own
connection and thread.

The tests that boot the proxy need the cordis-py runtime and skip without it,
like every session test.
"""

import hashlib
import http.client
import importlib.util
import json
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:  # `runtime`, imported only by cordis tests
    sys.path.insert(0, str(_BACKEND))

from revl.mcp import server  # noqa: E402
from revl.mcp.http_guard import Exposure  # noqa: E402
from revl.mcp.http_transport import (PROTOCOL_VERSION, HttpTransport,  # noqa: E402
                                     ServerDispatcher)
from revl.mcp.operator import parse_profile  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "mcp_proxy_fake_upstream.py"
SID = "io.modelcontextprotocol/subscriptionId"

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SECRETS = {"agent": "agent-stream-0", "human": "human-stream-1",
           "alice": "alice-stream-2", "bob": "bob-stream-3"}
GRANTS = {"agent": "call, lease, swap, unload, commit, load",
          "human": "approve, estop, commit",
          "alice": "call", "bob": "call"}


def _profile(extra: str = "") -> str:
    lines = []
    for token, verbs in GRANTS.items():
        digest = hashlib.sha256(SECRETS[token].encode("utf-8")).hexdigest()
        lines.append(f"operator {token} key sha256:{digest}")
        lines.append(f"operator {token} may {verbs} on *")
    return "\n".join(lines) + "\n" + extra


META = {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {}}


# ---------------------------------------------------------------- clients

def _headers(method, *, who=None, name=None, sse=True):
    sent = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream" if sse
            else "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Method": method}
    if name is not None:
        sent["Mcp-Name"] = name
    if who is not None:
        sent["Authorization"] = f"Bearer {SECRETS[who]}"
    return sent


def _body(method, params, rid, meta=None):
    params = dict(params or {})
    params["_meta"] = {**META, **(meta or {})}
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                       "params": params}).encode("utf-8")


class Stream:
    """A real HTTP client on its own connection, reading SSE on a thread."""

    EOF = object()

    def __init__(self, port, method, params, *, who, rid=1, meta=None, sse=True,
                 name=None):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        self.conn.request("POST", "/mcp", body=_body(method, params, rid, meta),
                          headers=_headers(method, who=who, name=name, sse=sse))
        # the response takes the socket over (Connection: close), so keep it
        # here to hang up on the server the way a client that leaves does
        self.sock = self.conn.sock
        self.response = self.conn.getresponse()
        self.status = self.response.status
        self.type = self.response.getheader("Content-Type") or ""
        self.header = {k.lower(): v for k, v in self.response.getheaders()}
        self.events: queue.Queue = queue.Queue()
        self.json = None
        if self.type.startswith("text/event-stream"):
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
        else:
            raw = self.response.read()
            self.json = json.loads(raw) if raw else None
            self.conn.close()

    def _read(self):
        data = []
        try:
            while True:
                line = self.response.readline()
                if not line:
                    break
                line = line.decode("utf-8").rstrip("\r\n")
                if line == "":
                    if data:
                        self.events.put(json.loads("\n".join(data)))
                        data = []
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
        except (OSError, ValueError):
            pass
        self.events.put(self.EOF)

    def next(self, timeout=10.0):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, quiet=0.5):
        """Every event that arrives until the stream is quiet for `quiet` s."""
        got = []
        while True:
            event = self.next(timeout=quiet)
            if event is None:
                return got
            got.append(event)
            if event is self.EOF:
                return got

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()


def _listen(port, who, notifications, *, rid=1, sse=True):
    return Stream(port, "subscriptions/listen", {"notifications": notifications},
                  who=who, rid=rid, sse=sse)


def _call(port, who, name, arguments=None, *, rid=7, meta=None, sse=True):
    return Stream(port, "tools/call", {"name": name, "arguments": arguments or {}},
                  who=who, rid=rid, meta=meta, sse=sse, name=name)


def _acked(stream, rid):
    assert stream.status == 200, (stream.status, stream.json)
    assert stream.type.startswith("text/event-stream"), stream.type
    assert stream.header.get("x-accel-buffering") == "no"
    first = stream.next()
    assert first["method"] == "notifications/subscriptions/acknowledged", first
    assert first["params"]["_meta"][SID] == rid
    return first["params"]["notifications"]


def _methods(events):
    return [e.get("method") for e in events if e is not Stream.EOF]


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def fresh(monkeypatch, tmp_path):
    from revl.mcp.leases import LeaseBook
    from revl.mcp.session import Session

    session = Session()
    session._wal_path = str(tmp_path / "session.wal")
    session.leases = LeaseBook()
    monkeypatch.setattr(server, "SESSION", session)
    yield session
    try:
        if server.SESSION.loaded and not server.SESSION.halted:
            server.SESSION.unload()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


@pytest.fixture
def serving(fresh):
    started = []

    def start():
        transport = HttpTransport(ServerDispatcher(server),
                                  registry=parse_profile(_profile()),
                                  exposure=Exposure("127.0.0.1", 0),
                                  server_module=server)
        transport.start()
        started.append(transport)
        return transport, transport.exposure.port_in_use

    yield start
    for transport in started:
        transport.stop()


@pytest.fixture
def proxied(fresh, tmp_path):
    """The proxy over the fake upstream (`--streams`), served over HTTP, with
    the operator profile as a live file."""
    from revl.mcp import proxy as proxy_mod
    from revl.mcp.http_transport import ProxyDispatcher
    from revl.mcp.schema import parse_undo_specs

    made = []
    path = tmp_path / "ops.profile"

    def build(*, flags=(), trust_read_only=True, settle_ms=50, keepalive_s=15.0):
        path.write_text(_profile(), encoding="utf-8")
        upstream = proxy_mod.Upstream([sys.executable, str(FAKE), "--streams", *flags],
                                      timeout=90)
        proxy = proxy_mod.Proxy(upstream,
                                undo=parse_undo_specs(["delete_note=restore_note"]),
                                trust_read_only=trust_read_only,
                                stdout=proxy_mod._Discard())
        proxy.activate()
        upstream.start()
        made.append(proxy)
        proxy.connect()
        transport = HttpTransport(ProxyDispatcher(proxy), profile_path=str(path),
                                  profile_settle_ms=settle_ms,
                                  exposure=Exposure("127.0.0.1", 0),
                                  server_module=server)
        transport.keepalive_s = keepalive_s
        transport.start()
        made.append(transport)
        return proxy, transport, transport.exposure.port_in_use, path

    yield build
    for thing in reversed(made):
        if isinstance(thing, HttpTransport):
            thing.stop()
        else:
            thing.deactivate()
            thing.upstream.close()


def _upstream_state(proxy):
    return proxy.upstream.request("tools/call", {"name": "list_notes",
                                                "arguments": {}})["structuredContent"]


def _result(stream):
    """The final JSON-RPC response of a call, SSE or JSON."""
    if stream.json is not None:
        return stream.json
    events = stream.drain(quiet=10)
    finals = [e for e in events if e is not Stream.EOF and "id" in e]
    assert len(finals) == 1, events
    return finals[0]


# ---------------------------------------------------------------- serve

def test_listen_is_authenticated_first_and_acknowledged_first(serving):
    transport, port = serving()
    # no credential: a 401 JSON answer, and no stream is opened
    anonymous = _listen(port, None, {"toolsListChanged": True})
    assert anonymous.status == 401 and anonymous.json["error"]["code"] == -32600
    assert not anonymous.type.startswith("text/event-stream")
    stream = _listen(port, "alice", {"toolsListChanged": True}, rid="L1")
    try:
        # revl mcp serve's tool list never changes: nothing is honored
        assert _acked(stream, "L1") == {}
        # an open listen stream does not hold the one dispatch lock
        assert not transport.binding.lock.locked()
        status = Stream(port, "tools/list", {}, who="bob", rid=2).status
        assert status == 200
    finally:
        stream.close()


# ------------------------------------------- above FD_SETSIZE (issue #1716)
#
# `select()` refuses a descriptor at or above 1024, and `peer_closed` used to
# read that refusal as "the client left", so a process holding that many files
# closed every listen stream at its first quiet poll. The full CI suite holds
# more than 1024 by the time it reaches this file, which is how four of the
# proxy tests below went red there and stayed green alone. These two need no
# runtime, so they run in every job.

@pytest.fixture
def above_fd_setsize():
    """Hold enough descriptors open that the next socket's fd is above 1024."""
    import os
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 1200
    if soft < want:
        if hard != resource.RLIM_INFINITY and hard < want:
            pytest.skip(f"RLIMIT_NOFILE hard limit {hard} is below {want}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
    try:
        yield
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_an_open_peer_above_fd_setsize_is_not_read_as_closed(above_fd_setsize):
    from revl.mcp.http_stream import peer_closed

    near, far = socket.socketpair()
    try:
        assert near.fileno() >= 1024, near.fileno()
        assert peer_closed(near) is False, "an open, quiet peer read as closed"
        far.sendall(b"x")
        assert peer_closed(near) is False, "pending data is not a close"
        far.close()
        near.recv(1)
        assert peer_closed(near) is True, "a closed peer must still be seen"
    finally:
        near.close()


def test_a_listen_stream_above_fd_setsize_stays_open(serving, above_fd_setsize):
    transport, port = serving()
    stream = _listen(port, "alice", {"toolsListChanged": True}, rid="F1")
    try:
        assert _acked(stream, "F1") == {}
        # several quiet polls (`poll_s` is 0.25s): the stream must not end
        assert stream.drain(quiet=1.0) == []
    finally:
        stream.close()


def test_listen_filter_accept_and_limits_are_checked(serving):
    transport, port = serving()
    bad = _listen(port, "alice", {"toolsListChanged": "yes"})
    assert bad.status == 400 and bad.json["error"]["code"] == -32602
    plain = _listen(port, "alice", {"toolsListChanged": True}, sse=False)
    assert plain.status == 406 and "text/event-stream" in plain.json["error"]["message"]
    open_streams = []
    try:
        for rid in range(4):
            stream = _listen(port, "alice", {}, rid=rid)
            _acked(stream, rid)
            open_streams.append(stream)
        refused = _listen(port, "alice", {}, rid=99)
        assert refused.status == 429, "a fifth stream for one operator is refused"
        other = _listen(port, "bob", {}, rid=100)
        _acked(other, 100)
        open_streams.append(other)
    finally:
        for stream in open_streams:
            stream.close()


def test_stopping_ends_a_listen_stream_gracefully(fresh):
    transport = HttpTransport(ServerDispatcher(server),
                              registry=parse_profile(_profile()),
                              exposure=Exposure("127.0.0.1", 0), server_module=server)
    transport.start()
    stream = _listen(transport.exposure.port_in_use, "alice", {}, rid=5)
    _acked(stream, 5)
    transport.stop()
    events = stream.drain(quiet=5)
    assert events[-1] is Stream.EOF
    cancelled, completion = events[0], events[1]
    assert cancelled["method"] == "notifications/cancelled"
    assert cancelled["params"]["requestId"] == 5
    assert cancelled["params"]["_meta"][SID] == 5
    assert completion["id"] == 5 and completion["result"]["resultType"] == "complete"
    assert completion["result"]["_meta"][SID] == 5


# ---------------------------------------------------------------- proxy

@needs_cordis
def test_two_listeners_never_receive_each_others_resource_updates(proxied):
    proxy, transport, port, _path = proxied()
    discovered = Stream(port, "server/discover", {}, who="alice", rid=1).json
    caps = discovered["result"]["capabilities"]
    assert caps["resources"]["subscribe"] is True
    assert caps["tools"]["listChanged"] is True and "logging" not in caps

    alice = _listen(port, "alice", {"resourceSubscriptions": ["note://n1"]}, rid="a")
    bob = _listen(port, "bob", {"resourceSubscriptions": ["note://n2"]}, rid="b")
    try:
        assert _acked(alice, "a") == {"resourceSubscriptions": ["note://n1"]}
        assert _acked(bob, "b") == {"resourceSubscriptions": ["note://n2"]}
        assert sorted(_upstream_state(proxy)["subscribed"]) == ["note://n1", "note://n2"]

        assert _result(_call(port, "agent", "delete_note", {"id": "n1"}))["result"][
            "isError"] is False
        got = alice.next()
        assert got["method"] == "notifications/resources/updated"
        assert got["params"]["uri"] == "note://n1" and got["params"]["_meta"][SID] == "a"
        assert bob.drain() == [], "bob did not subscribe to note://n1"

        assert _result(_call(port, "agent", "delete_note", {"id": "n2"}))["result"][
            "isError"] is False
        got = bob.next()
        assert got["params"]["uri"] == "note://n2" and got["params"]["_meta"][SID] == "b"
        assert alice.drain() == [], "alice did not subscribe to note://n2"
    finally:
        alice.close()
        bob.close()
    # the last listener gone, the upstream is unsubscribed
    deadline = time.monotonic() + 10
    while _upstream_state(proxy)["subscribed"] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _upstream_state(proxy)["subscribed"] == []


@needs_cordis
def test_tools_list_changed_reaches_only_the_listener_that_asked(proxied):
    proxy, transport, port, _path = proxied()
    alice = _listen(port, "alice", {"toolsListChanged": True}, rid=11)
    bob = _listen(port, "bob", {"resourceSubscriptions": ["note://n2"]}, rid=12)
    try:
        assert _acked(alice, 11) == {"toolsListChanged": True}
        _acked(bob, 12)
        # the liar: it claims read-only and announces a change during its call,
        # so the proxy withdraws the claim and says the tool list changed
        touched = _result(_call(port, "agent", "touch_note", {"id": "n1"}))
        assert touched["result"]["_meta"]["revl/proxy"]["flagged"]
        got = alice.next()
        assert got["method"] == "notifications/tools/list_changed"
        assert got["params"]["_meta"][SID] == 11
        assert bob.drain() == [], "bob asked for note://n2 only"
    finally:
        alice.close()
        bob.close()


@needs_cordis
def test_progress_reaches_only_the_request_it_belongs_to(proxied):
    proxy, transport, port, _path = proxied()
    bob_listens = _listen(port, "bob", {"toolsListChanged": True,
                                        "resourcesListChanged": True,
                                        "resourceSubscriptions": ["note://n1"]}, rid=21)
    try:
        _acked(bob_listens, 21)
        # alice asks for progress: her reply becomes an SSE stream
        alice = _call(port, "alice", "progress_note", meta={"progressToken": "t"}, rid=1)
        assert alice.type.startswith("text/event-stream")
        events = alice.drain(quiet=10)
        progress = [e for e in events if e is not Stream.EOF
                    and e.get("method") == "notifications/progress"]
        assert [p["params"]["progress"] for p in progress] == [1, 2]
        assert all(p["params"]["progressToken"] == "t" for p in progress)
        assert events[-2]["id"] == 1 and events[-2]["result"]["isError"] is False
        assert "notifications/message" not in _methods(events), "logs are not relayed"

        # bob uses the SAME token. The upstream sends one late event under
        # alice's previous token first: it must reach nobody, bob included
        bob = _call(port, "bob", "progress_note", meta={"progressToken": "t"}, rid=2)
        events = bob.drain(quiet=10)
        progress = [e for e in events if e is not Stream.EOF
                    and e.get("method") == "notifications/progress"]
        assert [p["params"]["progress"] for p in progress] == [1, 2], progress
        # the upstream never saw a caller's own token
        seen = _upstream_state(proxy)["progressTokens"]
        assert len(seen) == 2 and "t" not in seen and seen[0] != seen[1]
        # and none of it reached bob's listen stream
        assert bob_listens.drain() == []

        # a client that does not accept SSE gets plain JSON and no progress
        plain = _call(port, "alice", "progress_note", meta={"progressToken": "p"},
                      rid=3, sse=False)
        assert plain.status == 200 and plain.json["result"]["isError"] is False
    finally:
        bob_listens.close()


@needs_cordis
def test_a_revoked_listener_gets_nothing_more_and_the_other_still_does(proxied):
    proxy, transport, port, path = proxied(keepalive_s=600)
    alice = _listen(port, "alice", {"toolsListChanged": True}, rid=31)
    bob = _listen(port, "bob", {"toolsListChanged": True}, rid=32)
    try:
        _acked(alice, 31)
        _acked(bob, 32)
        path.write_text(_profile("operator alice revoked\n"), encoding="utf-8")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:   # wait for the edit to settle
            if Stream(port, "tools/list", {}, who="agent", rid=3).status == 200:
                break
            time.sleep(0.05)
        assert Stream(port, "tools/list", {}, who="alice", rid=4).status == 401
        _result(_call(port, "agent", "touch_note", {"id": "n1"}))
        got = bob.next()
        assert got["method"] == "notifications/tools/list_changed"
        # alice's stream is closed before the event, with nothing more sent
        assert alice.drain(quiet=5) == [Stream.EOF]
    finally:
        alice.close()
        bob.close()


@needs_cordis
def test_open_streams_never_fence_an_estop(proxied, tmp_path):
    import runtime as rt

    proxy, transport, port, _path = proxied(flags=("--slow-tool",))
    release = tmp_path / "release"
    alice = _listen(port, "alice", {"toolsListChanged": True}, rid=41)
    bob = _listen(port, "bob", {"resourceSubscriptions": ["note://n1"]}, rid=42)
    outcome = {}

    def blocked():
        outcome["result"] = _result(_call(port, "agent", "wait_note",
                                          {"release": str(release)},
                                          meta={"progressToken": 5}))

    caller = threading.Thread(target=blocked)
    try:
        _acked(alice, 41)
        _acked(bob, 42)
        caller.start()
        deadline = time.monotonic() + 30
        while not transport.binding.lock.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.binding.lock.locked(), "the agent's call holds the session"
        started = time.monotonic()
        halted = _result(_call(port, "human", "revl_estop", {"reason": "stop"},
                               meta={"progressToken": 6}))
        assert time.monotonic() - started < 10
        payload = halted["result"]["structuredContent"]
        assert payload["ok"] is True and payload["latched"] is True
        assert payload["operator"] == "human"
    finally:
        release.write_text("go", encoding="utf-8")
        if caller.ident is not None:
            caller.join(timeout=60)
        alice.close()
        bob.close()
        rt.clear_estop()
        rt._LIVE_FRAMES.clear()
    assert outcome["result"]["result"]["isError"] is False


@needs_cordis
def test_no_server_to_client_request_is_relayed_and_no_input_is_taken(proxied):
    proxy, transport, port, _path = proxied()
    asked = _call(port, "alice", "ask_model", rid=51)
    result = _result(asked)["result"]
    # the proxy refuses the upstream's sampling request itself: the HTTP client
    # never sees it, and gets a complete result, never an InputRequiredResult
    assert result["resultType"] == "complete"
    reply = result["structuredContent"]["reply"]
    assert reply["error"]["code"] == -32601 and "does not relay" in reply["error"]["message"]
    before = _upstream_state(proxy)["calls"]
    for field, value in (("requestState", "forged"),
                         ("inputResponses", {"k": {"action": "accept"}})):
        refused = Stream(port, "tools/call", {"name": "delete_note",
                                              "arguments": {"id": "n1"}, field: value},
                         who="agent", rid=52, name="delete_note")
        assert refused.status == 400 and refused.json["error"]["code"] == -32602
    assert _upstream_state(proxy)["calls"] == before, "nothing was dispatched"


@needs_cordis
def test_an_upstream_list_change_is_handled_under_the_lock_then_announced(proxied):
    """`server/discover` and `subscriptions/listen` read the capabilities
    without the dispatch lock, so reading them must not act on a pending
    upstream change (a re-list and a session swap). The next dispatched
    request does, and the listener hears of it."""
    proxy, transport, port, _path = proxied()
    proxy.upstream.tools_changed = True   # as if the upstream announced a change
    assert Stream(port, "server/discover", {}, who="bob", rid=1).status == 200
    assert proxy.upstream.tools_changed is True, "discover acted outside the lock"
    alice = _listen(port, "alice", {"toolsListChanged": True}, rid=61)
    try:
        _acked(alice, 61)
        assert proxy.upstream.tools_changed is True, "listen acted outside the lock"
        assert alice.drain() == []
        assert Stream(port, "tools/list", {}, who="bob", rid=2).status == 200
        assert proxy.upstream.tools_changed is False
        got = alice.next()
        assert got["method"] == "notifications/tools/list_changed"
        assert got["params"]["_meta"][SID] == 61
    finally:
        alice.close()
