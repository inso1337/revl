"""Two HTTP listener fixes (issue #1488, and #1463's revocation follow-up).

1. `revl serve --http` dispatched concurrent requests straight into one live
   session. A `Session` drives one asyncio loop with `run_until_complete`, so on
   the tree before this change four concurrent calls on a real session gave
   three 500s ("This event loop is already running") and one 200. The face now
   takes the same `http_guard.DispatchLock` the MCP HTTP transport takes.

2. The MCP HTTP transport read the operator profile once, at start, so a
   `revoked` line took effect only on restart. It now re-reads the file when it
   changes, checked on every request, and a profile that no longer parses
   refuses every request (503, naming the error) instead of serving on under
   the old one.
"""

import hashlib
import http.client
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, build_http_server  # noqa: E402
from revl.mcp.http_guard import DispatchLock, Exposure  # noqa: E402
from revl.mcp.http_transport import (PROTOCOL_VERSION, HttpTransport,  # noqa: E402
                                     ServerDispatcher, TransportError)

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a real session needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


# ---------------------------------------------------------------- 1: one lock

_SLOW = '''
extern pure fn slow(n: Int) -> Int = @py {
    import time
    time.sleep(0.2)
    return n * 10
}
service Work { fn run(n: Int) -> Int }
component W provides work: Work {
  provide work { fn run(n) = slow(n) }
}
'''

N = 8


@needs_cordis
def test_concurrent_requests_to_the_face_all_complete_with_their_own_answer():
    from revl.mcp.session import Session

    session = Session()
    session.load(compile_source(_SLOW, "slow.rvl"))
    # the face serves only what is declared public (#1505, item 569 B1)
    face = HttpComposedServer(session, composition="app", public={("work", "run")})
    httpd = build_http_server(face, "127.0.0.1", 0)
    port = httpd.server_address[1]
    serving = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving.start()
    results = {}
    barrier = threading.Barrier(N)

    def call(n):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        barrier.wait()
        conn.request("POST", "/app/work/run", body=json.dumps([n]),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        results[n] = (response.status, json.loads(response.read()))
        conn.close()

    try:
        callers = [threading.Thread(target=call, args=(n,)) for n in range(N)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=120)
    finally:
        httpd.shutdown()
        httpd.server_close()
        serving.join(timeout=5)
        session.unload()
    assert results == {n: (200, {"ok": True, "value": n * 10}) for n in range(N)}


def test_both_listeners_share_one_lock_implementation():
    face = HttpComposedServer(_StubSession(), composition="app")
    assert isinstance(face.dispatch_lock, DispatchLock)
    transport = HttpTransport(ServerDispatcher(server),
                              registry=_parse(_profile({"alice": ""})),
                              exposure=Exposure("127.0.0.1", 0), server_module=server)
    assert isinstance(transport.binding.lock, DispatchLock)


class _StubSession:
    ir = compile_source(_SLOW, "slow.rvl")


def test_hold_does_not_wait_when_asked_not_to():
    lock = DispatchLock()
    with lock:
        with lock.hold(blocking=False) as held:
            assert held is False
    with lock.hold(blocking=False) as held:
        assert held is True
    assert not lock.locked()


# ---------------------------------------------------------------- 2: revocation

SECRETS = {"alice": "alice-secret-2", "bob": "bob-secret-3"}


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _profile(extra: dict) -> str:
    lines = []
    for token, secret in SECRETS.items():
        lines.append(f"operator {token} key sha256:{_digest(secret)}")
        lines.append(f"operator {token} may lease on *")
        if extra.get(token):
            lines.append(extra[token])
    return "\n".join(lines) + "\n"


def _parse(text):
    from revl.mcp.operator import parse_profile

    return parse_profile(text)


def _list(port, who):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {}}}}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/mcp", body=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": "tools/list", "Authorization": f"Bearer {SECRETS[who]}"})
    response = conn.getresponse()
    payload = json.loads(response.read() or b"null")
    conn.close()
    return response.status, payload


@pytest.fixture
def from_file(tmp_path, monkeypatch):
    from revl.mcp.session import Session

    monkeypatch.setattr(server, "SESSION", Session())
    path = tmp_path / "ops.profile"
    path.write_text(_profile({}), encoding="utf-8")
    transport = HttpTransport(ServerDispatcher(server), exposure=Exposure("127.0.0.1", 0),
                              server_module=server, profile_path=str(path))
    transport.start()
    yield path, transport.exposure.port_in_use
    transport.stop()


SETTLE_S = 1.1   # a little more than the default --profile-settle-ms (1000)


def _settle(port, who="alice"):
    """The first request after an edit sees the new content once and is refused
    ("changing"); one SETTLE later the second identical read adopts it."""
    status, body = _list(port, who)
    assert status == 503 and "changing" in body["error"]["message"], (status, body)
    time.sleep(SETTLE_S)


def test_a_revocation_takes_effect_on_the_next_request(from_file):
    path, port = from_file
    assert _list(port, "bob")[0] == 200
    path.write_text(_profile({"bob": "operator bob revoked"}), encoding="utf-8")
    # bob is NOT served under the old profile while the edit settles
    _settle(port, "bob")
    status, body = _list(port, "bob")
    assert status == 401
    assert "REVOKED" in body["error"]["message"]
    assert _list(port, "alice")[0] == 200, "only bob was revoked"


def test_a_profile_truncated_before_a_deny_is_never_served(from_file):
    """The realistic race: the profile is written in two steps, and the first
    step stops just before a `may not` line. Served as it stands, it would widen
    alice's grant. No request is ever served under it, nor under the old
    profile once the edit has begun, and the completed profile is adopted."""
    path, port = from_file
    # the edit adds a grant and, after it, the deny that bounds it
    deny = "operator alice may not lease on *\n"
    complete = _profile({}) + "operator alice may snapshot on *\n" + deny
    truncated = complete[:complete.index(deny)]
    assert truncated != _profile({}), "the truncated write is a real change"
    assert _lease(port, "alice", "SecretBefore")["ok"] is True, "the old grant"

    path.write_text(truncated, encoding="utf-8")
    outcomes = []
    stop = threading.Event()

    def hammer():
        i = 0
        while not stop.is_set():
            i += 1
            outcomes.append(_lease_status(port, "alice", f"Secret{i}"))

    attacker = threading.Thread(target=hammer)
    attacker.start()
    first_write = time.monotonic()
    time.sleep(0.1)
    path.write_text(complete, encoding="utf-8")
    gap = time.monotonic() - first_write
    assert gap < 1.0, f"the second write came {gap:.2f}s later: not the race under test"
    time.sleep(2.0)
    stop.set()
    attacker.join(timeout=30)

    served = [o for o in outcomes if o[0] == 200 and o[1].get("ok") is True]
    assert served == [], f"a Secret lease was granted mid-edit: {served[:3]}"
    assert any(o[0] == 503 for o in outcomes), "the edit window refused requests"
    refused = _lease(port, "alice", "SecretAfter")
    assert refused["ok"] is False and refused["authority"]["allowed"] is False
    assert _lease(port, "bob", "Other")["ok"] is True, "the completed profile is in force"


def _lease_status(port, who, component):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "revl_lease",
                       "arguments": {"action": "claim", "component": component},
                       "_meta": {
                           "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                           "io.modelcontextprotocol/clientCapabilities": {}}}}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/mcp", body=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": "tools/call", "Mcp-Name": "revl_lease",
        "Authorization": f"Bearer {SECRETS[who]}"})
    response = conn.getresponse()
    payload = json.loads(response.read() or b"null")
    conn.close()
    if response.status == 200:
        return 200, payload["result"]["structuredContent"]
    return response.status, payload


def _lease(port, who, component):
    status, payload = _lease_status(port, who, component)
    assert status == 200, (status, payload)
    return payload


def test_an_edit_the_stat_signature_cannot_see_is_still_read(from_file, monkeypatch):
    """The racy-clean rule. On a filesystem with coarse timestamps, two writes in
    one tick can leave mtime, ctime and size all unchanged. The stat below is
    frozen to exactly that, and the edit must still be seen, because the file's
    mtime is within `RACY_NS` of the last read."""
    from revl.mcp import live_profile

    path, port = from_file
    assert _list(port, "bob")[0] == 200
    frozen = os.stat(path)
    real_stat = os.stat

    def stat(target, *args, **kwargs):
        if os.fspath(target) == str(path):
            return frozen
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(live_profile.os, "stat", stat)
    original = "operator bob may lease on *"
    revoked = "operator bob revoked".ljust(len(original))
    path.write_text(path.read_text(encoding="utf-8").replace(original, revoked),
                    encoding="utf-8")
    _settle(port, "bob")
    assert _list(port, "bob")[0] == 401


@pytest.mark.parametrize("breakage", ["garbage", "delete"])
def test_a_broken_profile_refuses_every_request_and_names_why(from_file, breakage):
    path, port = from_file
    assert _list(port, "alice")[0] == 200
    if breakage == "delete":
        path.unlink()
    else:
        path.write_text(_profile({}) + "operator alice key sha256:not-a-digest\n",
                        encoding="utf-8")
        _settle(port)
    for who in ("alice", "bob"):
        status, body = _list(port, who)
        assert status == 503, (who, status)
        assert "operator profile" in body["error"]["message"]
        assert "changing" not in body["error"]["message"]
    # fixing the file restores service without a restart
    path.write_text(_profile({}), encoding="utf-8")
    _settle(port)
    assert _list(port, "alice")[0] == 200


def test_a_profile_that_cannot_load_at_start_refuses_to_start(tmp_path):
    path = tmp_path / "bad.profile"
    path.write_text("operator x key sha256:not-a-digest\n", encoding="utf-8")
    with pytest.raises(TransportError):
        HttpTransport(ServerDispatcher(server), exposure=Exposure("127.0.0.1", 0),
                      server_module=server, profile_path=str(path))


# ---------------------------------------------------------------- 3: stdio too

_SHOUT = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)

CASTERS = {"alice": "alice-secret-2", "bob": "bob-secret-3", "carol": "carol-secret-4"}


def _cast_profile(revoked=()):
    lines = []
    for token, secret in CASTERS.items():
        lines.append(f"operator {token} key sha256:{_digest(secret)}")
        lines.append(f"operator {token} may call, approve on *")
        if token in revoked:
            lines.append(f"operator {token} revoked")
    return "\n".join(lines) + "\n"


def _rpc_line(rid, name, arguments):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}) + "\n"


@needs_cordis
def test_on_stdio_a_revoked_caster_is_refused_after_a_file_edit(tmp_path, monkeypatch):
    """One mechanism: the stdio server re-binds its session to the live profile
    before each message, so a `revoked` line reaches the quorum-cast registry
    with no restart."""
    import io

    from revl.mcp.live_profile import ProfileSource, StdioBinding
    from revl.mcp.operator import parse_profile
    from revl.mcp.session import Session
    from revl.policy import ApprovalRule, Policy

    session = Session()
    session._wal_path = str(tmp_path / "stdio.wal")
    monkeypatch.setattr(server, "SESSION", session)
    path = tmp_path / "ops.profile"
    path.write_text(_cast_profile(), encoding="utf-8")
    registry = parse_profile(path.read_text(encoding="utf-8"))
    session.operator, session.operator_registry = registry.get("alice"), registry
    session.approval_policy = "auto"
    session.load(compile_source(_SHOUT, "shout.rvl"), record=True,
                 origin={"source": _SHOUT})
    session.sandbox = Policy(approval_rules=(
        ApprovalRule("announce", None, 2, tuple(CASTERS)),))
    binding = StdioBinding(ProfileSource(str(path)), server, "alice")
    sink = str(tmp_path / "sink.log")
    call = {"key": "ops", "method": "shout", "args": [sink, "q"]}

    # alice proposes: the class-(c) call raises the quorum ticket
    asked = server.handle(json.loads(_rpc_line(1, "revl_call", call)))
    ticket = asked["result"]["structuredContent"]["ticket"]

    def cast(rid, who):
        return _rpc_line(rid, "revl_approve", {
            "hash": ticket["hash"], "asToken": who, "asSecret": CASTERS[who]})

    def stdin():
        yield cast(2, "carol")                               # carol counts
        path.write_text(_cast_profile(revoked=("bob",)), encoding="utf-8")
        yield _rpc_line(3, "revl_state", {})                 # the edit is settling
        time.sleep(SETTLE_S)
        yield cast(4, "bob")                                 # bob, now revoked

    out = io.StringIO()
    server.serve(stdin=stdin(), stdout=out, before=binding)
    replies = {json.loads(line)["id"]: json.loads(line) for line in out.getvalue().splitlines()}

    assert replies[2]["result"]["structuredContent"]["ok"] is True, "carol counted"
    assert "changing" in replies[3]["error"]["message"], "refused while settling"
    refused = replies[4]["result"]["structuredContent"]
    assert refused["ok"] is False
    assert "REVOKED" in refused["diagnostics"][0]["message"]
    records = [json.loads(line) for line in
               Path(session._wal_path).read_text(encoding="utf-8").splitlines()]
    assert any(r.get("record") == "quorum-refused" and r.get("reason") == "revoked-credential"
               for r in records)
    assert not os.path.exists(sink), "the crossing never fired"
    session.unload()


def test_a_stdio_refusal_is_a_json_rpc_error_and_nothing_is_handled(monkeypatch):
    import io

    handled = []
    monkeypatch.setattr(server, "handle", lambda message: handled.append(message))
    out = io.StringIO()
    server.serve(stdin=[json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}),
                        json.dumps({"jsonrpc": "2.0", "method": "notifications/x"})],
                 stdout=out, before=lambda _message: "profile changing")
    assert handled == []
    assert [json.loads(line) for line in out.getvalue().splitlines()] == [
        {"jsonrpc": "2.0", "id": 9,
         "error": {"code": -32603, "message": "profile changing"}}]


# ---------------------------------------------------------------- 4: E-Stop is never fenced

_PURE = """
service Cache { fn size() -> Int }
component UserCache provides uc: Cache {
  let s = effect Map.new() undo s.drop()
  provide uc { fn size() = 0 }
}
"""

HALTERS = {"alice": "alice-secret-2", "human": "human-secret-9"}


def _halt_profile(extra=""):
    return (f"operator alice key sha256:{_digest(HALTERS['alice'])}\n"
            f"operator alice may lease, call on *\n"
            f"operator human key sha256:{_digest(HALTERS['human'])}\n"
            f"operator human may estop on *\n" + extra)


def _tool_status(port, who, name, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}, "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {}}}}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/mcp", body=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": "tools/call", "Mcp-Name": name,
        "Authorization": f"Bearer {HALTERS[who]}"})
    response = conn.getresponse()
    payload = json.loads(response.read() or b"null")
    conn.close()
    return response.status, payload


def _clear_halt():
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import runtime as rt

    rt.clear_estop()
    rt._LIVE_FRAMES.clear()


@pytest.fixture
def halting(tmp_path, monkeypatch):
    from revl.mcp.session import Session

    session = Session()
    monkeypatch.setattr(server, "SESSION", session)
    session.load(compile_source(_PURE, "pure.rvl"), origin={"source": _PURE})
    path = tmp_path / "ops.profile"
    path.write_text(_halt_profile(), encoding="utf-8")
    transport = HttpTransport(ServerDispatcher(server), exposure=Exposure("127.0.0.1", 0),
                              server_module=server, profile_path=str(path))
    transport.start()
    try:
        yield path, transport.exposure.port_in_use, session
    finally:
        transport.stop()
        _clear_halt()


@needs_cordis
@pytest.mark.parametrize("edit", ["settling", "broken"])
def test_estop_is_accepted_while_the_profile_settles_or_is_broken(halting, edit):
    path, port, session = halting
    if edit == "settling":
        path.write_text(_halt_profile("operator alice revoked\n"), encoding="utf-8")
    else:
        path.write_text(_halt_profile("operator bob key sha256:nope\n"), encoding="utf-8")
    # every other verb in the window is refused, for every caller
    for who, name, arguments in [("alice", "revl_state", {}),
                                 ("alice", "revl_lease", {"action": "claim",
                                                          "component": "X"}),
                                 ("human", "revl_estop_report", {})]:
        status, body = _tool_status(port, who, name, arguments)
        assert status == 503, (who, name, status, body)
    # an operator NOT authorized for estop under the last adopted profile is
    # refused by the operator gate; the one that is authorized halts
    status, body = _tool_status(port, "alice", "revl_estop", {"reason": "not mine"})
    assert status == 200 and body["result"]["structuredContent"]["ok"] is False
    assert session.halted is False
    status, body = _tool_status(port, "human", "revl_estop", {"reason": "mid-edit"})
    assert status == 200, body
    assert body["result"]["structuredContent"]["ok"] is True
    assert session.halted is True


def test_estop_under_the_last_adopted_profile_is_judged_against_it(tmp_path):
    """No live session is needed to show which registry an E-Stop is checked
    against: the last adopted one, not the one being written."""
    from revl.mcp.live_profile import ProfileSource, is_estop

    path = tmp_path / "ops.profile"
    path.write_text(_halt_profile(), encoding="utf-8")
    source = ProfileSource(str(path), settle_ms=50)
    adopted = source.registry
    path.write_text("operator mallory may estop on *\n", encoding="utf-8")
    registry, error = source.current()
    assert registry is None and "changing" in error
    assert source.last_adopted is adopted
    assert is_estop({"method": "tools/call", "params": {"name": "revl_estop"}})
    assert not is_estop({"method": "tools/call", "params": {"name": "revl_state"}})


def test_a_negative_settle_window_is_refused(tmp_path):
    from revl.mcp.live_profile import ProfileSource

    path = tmp_path / "ops.profile"
    path.write_text(_halt_profile(), encoding="utf-8")
    with pytest.raises(ValueError):
        ProfileSource(str(path), settle_ms=-1)


# ---------------------------------------------------------------- 5: stdio operator revoked

@needs_cordis
def test_a_revoked_stdio_operator_is_refused_except_for_estop(tmp_path, monkeypatch):
    import io

    from revl.mcp.live_profile import ProfileSource, StdioBinding
    from revl.mcp.operator import parse_profile
    from revl.mcp.session import Session

    session = Session()
    monkeypatch.setattr(server, "SESSION", session)
    session.load(compile_source(_PURE, "pure.rvl"), origin={"source": _PURE})
    path = tmp_path / "ops.profile"
    text = (f"operator ops key sha256:{_digest('ops-secret')}\n"
            f"operator ops may lease, estop on *\n")
    path.write_text(text, encoding="utf-8")
    registry = parse_profile(text)
    session.operator, session.operator_registry = registry.get("ops"), registry
    binding = StdioBinding(ProfileSource(str(path)), server, "ops")

    def stdin():
        yield _rpc_line(1, "revl_lease", {"action": "claim", "component": "A"})
        path.write_text(text + "operator ops revoked\n", encoding="utf-8")
        yield _rpc_line(2, "revl_state", {})                 # settling: refused
        time.sleep(SETTLE_S)
        yield _rpc_line(3, "revl_lease", {"action": "claim", "component": "B"})
        yield _rpc_line(4, "revl_estop", {"reason": "revoked but halting"})

    out = io.StringIO()
    try:
        server.serve(stdin=stdin(), stdout=out, before=binding)
        replies = {json.loads(line)["id"]: json.loads(line)
                   for line in out.getvalue().splitlines()}
        assert replies[1]["result"]["structuredContent"]["ok"] is True
        assert "changing" in replies[2]["error"]["message"]
        assert "REVOKED" in replies[3]["error"]["message"], replies[3]
        assert replies[4]["result"]["structuredContent"]["ok"] is True
        assert session.halted is True
    finally:
        _clear_halt()


@needs_cordis
def test_on_stdio_estop_is_accepted_while_the_profile_settles(tmp_path, monkeypatch):
    import io

    from revl.mcp.live_profile import ProfileSource, StdioBinding
    from revl.mcp.operator import parse_profile
    from revl.mcp.session import Session

    session = Session()
    monkeypatch.setattr(server, "SESSION", session)
    session.load(compile_source(_PURE, "pure.rvl"), origin={"source": _PURE})
    path = tmp_path / "ops.profile"
    text = (f"operator ops key sha256:{_digest('ops-secret')}\n"
            f"operator ops may lease, estop on *\n")
    path.write_text(text, encoding="utf-8")
    registry = parse_profile(text)
    session.operator, session.operator_registry = registry.get("ops"), registry
    binding = StdioBinding(ProfileSource(str(path)), server, "ops")

    def stdin():
        path.write_text(text + "operator other may lease on *\n", encoding="utf-8")
        yield _rpc_line(1, "revl_lease", {"action": "claim", "component": "A"})
        yield _rpc_line(2, "revl_estop", {"reason": "mid-edit"})

    out = io.StringIO()
    try:
        server.serve(stdin=stdin(), stdout=out, before=binding)
        replies = {json.loads(line)["id"]: json.loads(line)
                   for line in out.getvalue().splitlines()}
        assert "changing" in replies[1]["error"]["message"]
        assert replies[2]["result"]["structuredContent"]["ok"] is True
        assert session.halted is True
    finally:
        _clear_halt()
