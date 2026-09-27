"""MCP over Streamable HTTP with one operator per request (issue #1463, slice 1).

The claim under test is that two callers of one HTTP server cannot act as each
other: a request with no identity is never dispatched and never runs as a
default operator, each request's identity reaches leases, tickets, approvals and
quorum votes, identity does not leak between concurrent requests, and an E-Stop
from one caller lands while another caller's request holds the session.

The wire-level tests need no runtime. The ones that boot a composition, a proxy
or a quorum need the cordis-py runtime and skip without it, like every session
test.
"""

import copy
import datetime
import hashlib
import http.client
import importlib.util
import io
import json
import os
import ssl
import subprocess
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

from revl import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.http_guard import (Exposure, ExposureError, check_exposure,  # noqa: E402
                                 request_refusal)
from revl.mcp.http_transport import (NO_CALLER, PROTOCOL_VERSION, HttpTransport,  # noqa: E402
                                     ServerDispatcher, TransportError)
from revl.mcp.operator import parse_profile  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "mcp_proxy_fake_upstream.py"

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SECRETS = {"agent": "agent-secret-0", "human": "human-secret-1",
           "alice": "alice-secret-2", "bob": "bob-secret-3",
           "carol": "carol-secret-4", "viewer": "viewer-secret-5",
           "stale": "stale-secret-6"}


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


GRANTS = {
    "agent": "call, lease, swap, unload, commit, load",
    "human": "approve, estop, commit",
    "alice": "call, lease, swap, unload, commit, approve",
    "bob": "call, lease, swap, unload, commit, approve, estop",
    "carol": "approve",
    "viewer": "lease",
    "stale": "lease",
}


def _profile() -> str:
    lines = []
    for token, verbs in GRANTS.items():
        until = " until 2020-01-01T00:00:00Z" if token == "stale" else ""
        lines.append(f"operator {token} key sha256:{_digest(SECRETS[token])}{until}")
        lines.append(f"operator {token} may {verbs} on *")
    return "\n".join(lines) + "\n"


PROFILE = _profile()

META = {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {}}


# ---------------------------------------------------------------- client

def _send(port, body, *, token=None, headers=None, method="POST", path="/mcp",
          context=None, raw=None):
    """One HTTP request: (status, parsed JSON body or None, response headers)."""
    if context is not None:
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=90,
                                           context=context)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    sent = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    sent.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode("utf-8")
                                        if body is not None else None)
    conn.request(method, path, body=data, headers=sent)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    parsed = json.loads(payload) if payload else None
    return response.status, parsed, {k.lower(): v for k, v in response.getheaders()}


def _rpc(port, rpc_method, params=None, *, token=None, rid=1, headers=None,
         meta=META, context=None):
    params = dict(params or {})
    if meta is not None:
        params["_meta"] = dict(meta)
    sent = {"MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Method": rpc_method}
    if rpc_method == "tools/call":
        sent["Mcp-Name"] = params.get("name", "")
    sent.update(headers or {})
    return _send(port, {"jsonrpc": "2.0", "id": rid, "method": rpc_method,
                        "params": params}, token=token, headers=sent,
                 context=context)


def _tool(port, who, name, arguments=None, **kw):
    """Call a tool as `who`; the revl verdict (`structuredContent`)."""
    status, body, _ = _rpc(port, "tools/call",
                           {"name": name, "arguments": arguments or {}},
                           token=SECRETS[who] if who else None, **kw)
    assert status == 200, (status, body)
    return body["result"]


def _payload(port, who, name, arguments=None, **kw):
    return _tool(port, who, name, arguments, **kw)["structuredContent"]


# ---------------------------------------------------------------- fixtures

class _Counting:
    """A dispatcher that counts what reaches it."""

    def __init__(self, inner):
        self.inner = inner
        self.handled = []

    def handle(self, message):
        self.handled.append(message.get("method"))
        return self.inner.handle(message)

    def describe(self):
        return self.inner.describe()

    def advertised_tools(self):
        return self.inner.advertised_tools()


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A clean compiler-server session for one test."""
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

    def start(dispatcher=None, *, profile=PROFILE, exposure=None, auth="bearer"):
        transport = HttpTransport(dispatcher or ServerDispatcher(server),
                                  registry=parse_profile(profile),
                                  exposure=exposure or Exposure("127.0.0.1", 0),
                                  auth=auth, server_module=server)
        transport.start()
        started.append(transport)
        return transport

    yield start
    for transport in started:
        transport.stop()


def _port(transport) -> int:
    return transport.exposure.port_in_use


# ---------------------------------------------------------------- 1-3: no identity

@pytest.mark.parametrize("rpc_method,params", [
    ("server/discover", None), ("tools/list", None),
    ("tools/call", {"name": "revl_state", "arguments": {}}),
])
def test_no_credential_is_401_and_nothing_is_dispatched(serving, rpc_method, params):
    counting = _Counting(ServerDispatcher(server))
    transport = serving(counting)
    status, body, headers = _rpc(_port(transport), rpc_method, params)
    assert status == 401
    assert "bearer" in headers["www-authenticate"].lower()
    assert counting.handled == []
    assert server.SESSION.operator.token == NO_CALLER


@pytest.mark.parametrize("authorization", [
    "Bearer not-a-secret-anyone-holds",
    "Basic " + SECRETS["alice"],
    "Bearer ",
    f"Bearer {SECRETS['stale']}",          # its `until` has passed
])
def test_a_bad_or_lapsed_credential_is_401(serving, authorization):
    counting = _Counting(ServerDispatcher(server))
    transport = serving(counting)
    status, _, _ = _rpc(_port(transport), "tools/list",
                        headers={"Authorization": authorization})
    assert status == 401
    assert counting.handled == []


def test_two_authorization_headers_are_refused(serving):
    transport = serving()
    conn = http.client.HTTPConnection("127.0.0.1", _port(transport), timeout=30)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                       "params": {"_meta": META}}).encode()
    conn.putrequest("POST", "/mcp")
    for name, value in [("Content-Type", "application/json"),
                        ("MCP-Protocol-Version", PROTOCOL_VERSION),
                        ("Mcp-Method", "tools/list"),
                        ("Authorization", f"Bearer {SECRETS['alice']}"),
                        ("Authorization", f"Bearer {SECRETS['bob']}"),
                        ("Content-Length", str(len(body)))]:
        conn.putheader(name, value)
    conn.endheaders(body)
    assert conn.getresponse().status == 401
    conn.close()


def test_the_transport_never_runs_as_a_default_operator(serving):
    transport = serving()
    # between requests the session is bound to a deny-all placeholder: never
    # None (ungated) and never an empty token (the lease book's default holder)
    assert server.SESSION.operator is not None
    assert server.SESSION.operator.token == NO_CALLER
    assert server.SESSION.operator.grants == ()
    status, _, _ = _rpc(_port(transport), "tools/list", token=SECRETS["viewer"])
    assert status == 200
    assert server.SESSION.operator.token == NO_CALLER


def test_a_request_that_raises_still_unbinds_the_caller(serving):
    class _Raises(_Counting):
        def handle(self, message):
            raise RuntimeError("boom")

    transport = serving(_Raises(ServerDispatcher(server)))
    status, body, _ = _rpc(_port(transport), "tools/list", token=SECRETS["bob"])
    assert status == 500 and "boom" in body["error"]["message"]
    assert server.SESSION.operator.token == NO_CALLER


def test_stopping_restores_the_session_binding(fresh):
    fresh.operator = None
    transport = HttpTransport(ServerDispatcher(server),
                              registry=parse_profile(PROFILE),
                              exposure=Exposure("127.0.0.1", 0),
                              server_module=server)
    transport.start()
    transport.stop()
    assert fresh.operator is None
    assert fresh.operator_registry is None
    assert not hasattr(fresh, "operator_bound_by")


def test_http_mode_needs_a_profile_and_refuses_a_process_operator(tmp_path):
    profile = tmp_path / "ops.profile"
    profile.write_text(PROFILE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    for argv, expected in [
        (["--http", "127.0.0.1:0"], "needs --operator-profile"),
        (["--http", "127.0.0.1:0", "--operator-profile", str(profile),
          "--operator", "alice"], "--operator does not apply with --http"),
        (["--http", "0.0.0.0:0", "--operator-profile", str(profile)],
         "without TLS"),
    ]:
        done = subprocess.run([sys.executable, "-m", "revl", "mcp", "serve", *argv],
                              capture_output=True, text=True, timeout=120,
                              cwd=str(tmp_path), env=env)
        assert done.returncode == 1, (argv, done.stderr)
        assert expected in done.stderr, (argv, done.stderr)


def test_a_bearer_profile_with_no_credential_cannot_start():
    with pytest.raises(TransportError):
        HttpTransport(ServerDispatcher(server),
                      registry=parse_profile("operator a may lease on *\n"),
                      exposure=Exposure("127.0.0.1", 0))
    with pytest.raises(TransportError):
        HttpTransport(ServerDispatcher(server), registry=None,
                      exposure=Exposure("127.0.0.1", 0))


# ---------------------------------------------------------------- 4: no leak

def test_identity_does_not_leak_between_concurrent_requests(serving):
    transport = serving()
    port = _port(transport)
    seen = []
    errors = []

    def caller(who):
        try:
            for i in range(20):
                name = f"{who}-component-{i}"
                claimed = _payload(port, who, "revl_lease",
                                   {"action": "claim", "component": name})
                seen.append((who, claimed["holder"], claimed["authority"]["operator"]))
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(repr(exc))

    threads = [threading.Thread(target=caller, args=(who,)) for who in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == []
    assert len(seen) == 40
    assert all(who == holder == stamped for who, holder, stamped in seen)
    holders = {lease["component"]: lease["holder"]
               for lease in server.SESSION.leases.document()}
    assert all(name.startswith(holder) for name, holder in holders.items())
    assert len(holders) == 40


def test_a_caller_cannot_release_another_callers_lease(serving):
    transport = serving()
    port = _port(transport)
    assert _payload(port, "bob", "revl_lease",
                    {"action": "claim", "component": "UserCache"})["ok"] is True
    refused = _payload(port, "alice", "revl_lease",
                       {"action": "release", "component": "UserCache"})
    assert refused["ok"] is False
    assert "leased by `bob`" in refused["diagnostics"][0]["message"]


# ---------------------------------------------------------------- 5-7: authority

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


def _load_shout(session, quorum=False):
    from revl.policy import ApprovalRule, Policy

    session.approval_policy = "auto"
    session.load(copy.deepcopy(compile_source(_SHOUT, "shout.rvl")), record=True,
                 origin={"source": _SHOUT})
    if quorum:
        # bound after the boot, as the quorum suites do: the rule is read at
        # ticket time, and admitting `announce` under it would need a `with`
        # edge this small composition does not carry
        session.sandbox = Policy(approval_rules=(
            ApprovalRule("announce", None, 2, ("alice", "bob", "carol")),))


def _wal(session):
    return [json.loads(line) for line in
            Path(session._wal_path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


@needs_cordis
def test_the_agent_cannot_approve_its_own_ticket_and_the_human_can(serving, fresh,
                                                                   tmp_path):
    _load_shout(fresh)
    transport = serving()
    port = _port(transport)
    sink = str(tmp_path / "sink.log")
    asked = _payload(port, "agent", "revl_call",
                     {"key": "ops", "method": "shout", "args": [sink, "a"]})
    assert asked["approvalRequired"] is True
    ticket = asked["ticket"]
    assert not os.path.exists(sink), "nothing fired"

    self_approved = _payload(port, "agent", "revl_approve", {"hash": ticket["hash"]})
    assert self_approved["ok"] is False
    assert self_approved["authority"]["operator"] == "agent"
    assert self_approved["authority"]["allowed"] is False

    approved = _payload(port, "human", "revl_approve", {"hash": ticket["hash"]})
    assert approved["ok"] is True
    assert approved["authority"]["operator"] == "human"
    fired = _payload(port, "agent", "revl_call",
                     {"key": "ops", "method": "shout", "args": [sink, "a"]})
    assert fired["ok"] is True
    assert Path(sink).read_text(encoding="utf-8") == "announce:a\n"


def _quorum_run(port_or_none, session, tmp_path, *, over_http):
    """alice proposes, bob and carol answer. Over HTTP each on its own
    authenticated request; on stdio each by the session's own binding."""
    from revl.mcp.operator import parse_profile as _parse

    registry = _parse(PROFILE)
    sink = str(tmp_path / "quorum.log")

    def act(who, name, arguments):
        if over_http:
            return _payload(port_or_none, who, name, arguments)
        session.operator = registry.get(who)
        session.operator_registry = registry
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
        return response["result"]["structuredContent"]

    call = {"key": "ops", "method": "shout", "args": [sink, "q"]}
    asked = act("alice", "revl_call", call)
    ticket_hash = asked["ticket"]["hash"]
    by_proposer = act("alice", "revl_approve", {"hash": ticket_hash})
    first = act("bob", "revl_approve", {"hash": ticket_hash})
    second = act("carol", "revl_approve", {"hash": ticket_hash})
    fired = act("alice", "revl_call", call)
    votes = [r for r in _wal(session) if r.get("record") == "quorum-vote"]
    return by_proposer, first, second, fired, votes


@needs_cordis
def test_a_quorum_is_gathered_from_distinct_authenticated_callers(serving, fresh,
                                                                  tmp_path):
    _load_shout(fresh, quorum=True)
    transport = serving()
    by_proposer, first, second, fired, votes = _quorum_run(
        _port(transport), fresh, tmp_path, over_http=True)
    assert by_proposer["ok"] is False, "the proposer cannot count"
    assert first["ok"] is True and second["ok"] is True
    assert fired["ok"] is True
    assert [(v["voter"], v["boundBy"], v["principal"]) for v in votes] == [
        ("bob", "transport", "session:bob"), ("carol", "transport", "session:carol")]


@needs_cordis
def test_over_http_a_caller_cannot_cast_for_another_operator(serving, fresh, tmp_path):
    _load_shout(fresh, quorum=True)
    transport = serving()
    port = _port(transport)
    sink = str(tmp_path / "q.log")
    asked = _payload(port, "alice", "revl_call",
                     {"key": "ops", "method": "shout", "args": [sink, "q"]})
    ticket_hash = asked["ticket"]["hash"]
    # bob presents carol's real secret: on stdio (#979) that counts as carol;
    # over HTTP each operator casts on its own authenticated request
    forged = _payload(port, "bob", "revl_approve",
                      {"hash": ticket_hash, "asToken": "carol",
                       "asSecret": SECRETS["carol"]})
    assert forged["ok"] is False
    assert "casts only as itself" in forged["diagnostics"][0]["message"]
    assert [r for r in _wal(fresh) if r.get("record") == "quorum-vote"] == []


@needs_cordis
def test_the_cast_label_is_the_only_difference_from_stdio(fresh, serving, tmp_path,
                                                          monkeypatch):
    """stdio records `boundBy: "session"`, HTTP records `"transport"`, and every
    other field of the vote row is the same."""
    from revl.mcp.leases import LeaseBook
    from revl.mcp.session import Session

    _load_shout(fresh, quorum=True)
    (tmp_path / "a").mkdir()
    stdio = _quorum_run(None, fresh, tmp_path / "a", over_http=False)
    fresh.unload()

    second = Session()
    second._wal_path = str(tmp_path / "http.wal")
    second.leases = LeaseBook()
    monkeypatch.setattr(server, "SESSION", second)
    _load_shout(second, quorum=True)
    transport = serving()
    (tmp_path / "b").mkdir()
    over_http = _quorum_run(_port(transport), second, tmp_path / "b", over_http=True)

    stdio_votes, http_votes = stdio[4], over_http[4]
    assert [v["boundBy"] for v in stdio_votes] == ["session", "session"]
    assert [v["boundBy"] for v in http_votes] == ["transport", "transport"]

    def rest(votes):
        return [{k: v for k, v in row.items() if k not in ("boundBy", "at")}
                for row in votes]

    # the sinks differ by directory, so the ticket and its question differ;
    # compare the fields a cast decides
    keys = ("voter", "vote", "principal", "counted", "require", "proposer")
    assert [{k: r[k] for k in keys} for r in rest(stdio_votes)] == \
        [{k: r[k] for k in keys} for r in rest(http_votes)]
    assert [r["ok"] for r in stdio[:4]] == [r["ok"] for r in over_http[:4]]


_TWO = """
service Cache { fn get(k: Str) -> Opt[Str]
                fn size() -> Int }
component UserCache provides uc: Cache {
  let s = effect Map.new() undo s.drop()
  provide uc { fn get(k) = s.get(k)
               fn size() = 0 }
}
component OtherCache provides oc: Cache {
  let s = effect Map.new() undo s.drop()
  provide oc { fn get(k) = s.get(k)
               fn size() = 0 }
}
"""


@needs_cordis
def test_a_lease_held_by_one_caller_fences_the_other(serving, fresh):
    from revl.policy import parse_policy

    fresh.sandbox = parse_policy("leases enforced")
    fresh.load(compile_source(_TWO), origin={"source": _TWO})
    transport = serving()
    port = _port(transport)
    assert _payload(port, "bob", "revl_lease",
                    {"action": "claim", "component": "UserCache"})["ok"] is True
    swapped = _TWO.replace("fn size() = 0 }\n}\ncomponent OtherCache",
                           "fn size() = 42 }\n}\ncomponent OtherCache", 1)
    for name, arguments in [("revl_swap", {"source": swapped}),
                            ("revl_unload", {}),
                            ("revl_abort", {})]:
        refused = _payload(port, "alice", name, arguments)
        assert refused["ok"] is False, name
        assert refused.get("lease", {}).get("heldBy") == "bob", (name, refused)
    manifest = _payload(port, "alice", "revl_commit")["manifest"]
    refused = _payload(port, "alice", "revl_commit_confirm", {"hash": manifest["hash"]})
    assert refused["ok"] is False and refused["lease"]["heldBy"] == "bob"
    assert fresh.loaded, "the leased composition is still serving"
    # the holder may do what the other caller could not
    assert _payload(port, "bob", "revl_swap", {"source": swapped})["ok"] is True


# ---------------------------------------------------------------- 8, 12: proxy

@pytest.fixture
def proxied(fresh, monkeypatch):
    """A proxy over the fake upstream, served over HTTP."""
    from revl.mcp import proxy as proxy_mod
    from revl.mcp.http_transport import ProxyDispatcher
    from revl.mcp.schema import parse_undo_specs

    made = []

    def build(*, undo=(), flags=(), trust_read_only=False):
        upstream = proxy_mod.Upstream([sys.executable, str(FAKE), *flags], timeout=90)
        proxy = proxy_mod.Proxy(upstream, undo=parse_undo_specs(list(undo)),
                                trust_read_only=trust_read_only,
                                stdout=proxy_mod._Discard())
        proxy.activate()
        upstream.start()
        made.append(proxy)
        proxy.connect()
        transport = HttpTransport(ProxyDispatcher(proxy),
                                  registry=parse_profile(PROFILE),
                                  exposure=Exposure("127.0.0.1", 0),
                                  server_module=server)
        transport.start()
        made.append(transport)
        return proxy, transport

    yield build
    for thing in reversed(made):
        if isinstance(thing, HttpTransport):
            thing.stop()
        else:
            thing.deactivate()
            thing.upstream.close()


def _upstream_calls(proxy):
    return proxy.upstream.request("tools/call", {"name": "list_notes",
                                                "arguments": {}})["structuredContent"]


@needs_cordis
def test_a_proxied_tool_needs_the_call_grant(proxied):
    proxy, transport = proxied(trust_read_only=True)
    refused = _payload(_port(transport), "viewer", "list_notes")
    assert refused["ok"] is False
    assert refused["authority"]["verb"] == "call"
    assert _upstream_calls(proxy)["calls"] == []
    assert _tool(_port(transport), "agent", "list_notes")["isError"] is False


@needs_cordis
def test_proxy_abort_is_session_wide_and_a_lease_fences_it(proxied, fresh):
    from revl.policy import parse_policy

    proxy, transport = proxied(undo=["delete_note=restore_note"])
    port = _port(transport)
    fresh.sandbox = parse_policy("leases enforced")
    assert _tool(port, "agent", "delete_note", {"id": "n1"})["isError"] is False
    assert _payload(port, "agent", "revl_lease",
                    {"action": "claim", "component": "UpstreamProvider"})["ok"]
    refused = _payload(port, "bob", "revl_abort")
    assert refused["ok"] is False and refused["lease"]["heldBy"] == "agent"
    assert "n1" in _upstream_calls(proxy)["trash"], "the agent's work stands"
    # without the lease, another caller's abort reverts the agent's work: the
    # session is shared, and commit and abort are session-wide (documented)
    assert _payload(port, "agent", "revl_lease",
                    {"action": "release", "component": "UpstreamProvider"})["ok"]
    assert _payload(port, "bob", "revl_abort")["ok"] is True
    state = _upstream_calls(proxy)
    assert "n1" in state["notes"] and state["calls"][-1] == "restore_note"


@needs_cordis
def test_estop_lands_while_another_callers_request_holds_the_session(proxied,
                                                                     tmp_path):
    """Real concurrency: the agent's call is blocked inside the upstream and
    holds the session; the human's E-Stop must not wait for it."""
    import runtime as rt  # the py backend (cordis-gated test)

    proxy, transport = proxied(flags=("--slow-tool",), trust_read_only=True)
    port = _port(transport)
    release = tmp_path / "release"
    outcome = {}

    def blocked_call():
        outcome["result"] = _tool(port, "agent", "wait_note",
                                  {"release": str(release)})

    try:
        caller = threading.Thread(target=blocked_call)
        caller.start()
        deadline = time.monotonic() + 30
        while not transport.binding.lock.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.binding.lock.locked(), "the agent's call holds the session"
        time.sleep(0.2)

        started = time.monotonic()
        halted = _payload(port, "human", "revl_estop", {"reason": "test halt"})
        took = time.monotonic() - started
        assert halted["ok"] is True and halted["latched"] is True
        assert halted["operator"] == "human"
        assert took < 10, f"the E-Stop waited {took:.1f}s behind the busy session"
        assert caller.is_alive(), "the agent's call was still in flight"

        release.write_text("go", encoding="utf-8")
        caller.join(timeout=60)
        assert outcome["result"]["isError"] is False, "the in-flight call finished"

        refused = _payload(port, "agent", "list_notes")
        assert refused["ok"] is False
        assert "E-STOPPED" in refused["diagnostics"][0]["message"]
        report = _payload(port, "human", "revl_estop_report")
        assert report["halted"] is True and report["operator"] == "human"
        assert _upstream_calls(proxy)["calls"] == ["wait_note"]
    finally:
        release.write_text("go", encoding="utf-8")
        rt.clear_estop()
        rt._LIVE_FRAMES.clear()


@needs_cordis
def test_estop_on_an_idle_session_halts_at_once(proxied):
    import runtime as rt

    proxy, transport = proxied(trust_read_only=True)
    try:
        halted = _payload(_port(transport), "human", "revl_estop",
                          {"reason": "idle halt"})
        assert halted["ok"] is True and halted.get("latched") is None
        assert proxy.session.halted is True
    finally:
        rt.clear_estop()
        rt._LIVE_FRAMES.clear()


@needs_cordis
def test_the_transport_restores_the_runtime_latch(proxied):
    import runtime as rt

    before = rt._ESTOP_LATCH
    proxy, transport = proxied(trust_read_only=True)
    assert rt.estop_latch_path() == transport.latch.path
    transport.stop()
    assert rt._ESTOP_LATCH == before


# ---------------------------------------------------------------- 9: headers

def test_mirrored_headers_must_match_the_body(serving):
    transport = serving()
    port = _port(transport)
    token = SECRETS["alice"]
    status, body, _ = _rpc(port, "tools/call",
                           {"name": "revl_state", "arguments": {}}, token=token,
                           headers={"Mcp-Name": "revl_lease"})
    assert status == 400 and body["error"]["code"] == -32020
    status, body, _ = _rpc(port, "tools/list", token=token,
                           headers={"Mcp-Method": "tools/call"})
    assert status == 400 and body["error"]["code"] == -32020
    status, body, _ = _rpc(port, "tools/list", token=token,
                           headers={"MCP-Protocol-Version": "2025-11-25"})
    assert status == 400 and body["error"]["code"] == -32020
    status, body, _ = _rpc(port, "tools/list", token=token, meta={
        "io.modelcontextprotocol/protocolVersion": "1900-01-01",
        "io.modelcontextprotocol/clientCapabilities": {}},
        headers={"MCP-Protocol-Version": "1900-01-01"})
    assert status == 400 and body["error"]["code"] == -32022
    assert body["error"]["data"] == {"supported": [PROTOCOL_VERSION],
                                     "requested": "1900-01-01"}
    status, body, _ = _rpc(port, "tools/list", token=token, meta=None)
    assert status == 400 and body["error"]["code"] == -32602


def test_an_encoded_mcp_name_is_decoded_before_comparison(serving):
    import base64

    transport = serving()
    encoded = "=?base64?" + base64.b64encode(b"revl_state").decode() + "?="
    status, body, _ = _rpc(_port(transport), "tools/call",
                           {"name": "revl_state", "arguments": {}},
                           token=SECRETS["alice"], headers={"Mcp-Name": encoded})
    assert status == 200, body


# ---------------------------------------------------------------- 10: exposure

def test_a_foreign_origin_or_host_is_403_before_authentication(serving):
    counting = _Counting(ServerDispatcher(server))
    transport = serving(counting)
    port = _port(transport)
    for headers in ({"Origin": "http://rebind.attacker.example"},
                    {"Host": f"rebind.attacker.example:{port}"}):
        status, _, _ = _rpc(port, "tools/list", token=SECRETS["alice"],
                            headers=headers)
        assert status == 403, headers
        status, _, _ = _rpc(port, "tools/list", headers=headers)
        assert status == 403, "refused before the credential is even read"
    assert counting.handled == []
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        status, _, _ = _rpc(port, "tools/list", token=SECRETS["alice"],
                            headers={"Host": host})
        assert status == 200, host


def test_an_allowed_origin_is_served(serving):
    transport = serving(exposure=Exposure("127.0.0.1", 0,
                                          allow_origins=("https://app.example",)))
    status, _, _ = _rpc(_port(transport), "tools/list", token=SECRETS["alice"],
                        headers={"Origin": "https://app.example"})
    assert status == 200


@pytest.mark.parametrize("exposure,expected", [
    (Exposure("0.0.0.0", 0), "without TLS"),
    (Exposure("10.1.2.3", 0), "without TLS"),
    (Exposure("0.0.0.0", 0, tls_cert="c", tls_key="k"), "--allow-host"),
    (Exposure("127.0.0.1", 0, tls_cert="c"), "go together"),
    (Exposure("127.0.0.1", 0, allow_origins=("*",)), "every page"),
])
def test_an_unsafe_listener_refuses_to_start(exposure, expected):
    with pytest.raises(ExposureError) as caught:
        check_exposure(exposure)
    assert expected in str(caught.value)


def test_the_guard_refuses_a_repeated_host_or_origin():
    headers = http.client.parse_headers(io.BytesIO(
        b"Host: 127.0.0.1\r\nHost: evil.example\r\n\r\n"))
    assert request_refusal(headers, Exposure("127.0.0.1", 8080)) is not None
    headers = http.client.parse_headers(io.BytesIO(
        b"Host: 127.0.0.1\r\nOrigin: https://a\r\nOrigin: https://b\r\n\r\n"))
    assert request_refusal(headers, Exposure("127.0.0.1", 8080,
                                             allow_origins=("https://a",))) is not None


# ---------------------------------------------------------------- 11: mTLS

def _pki(tmp_path, names):
    """A throwaway CA, a server certificate for 127.0.0.1, and one client
    certificate per name (commonName = the operator token)."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def pem_key(key, path):
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))

    def issue(cn, issuer_key, issuer_name, *, ca=False, san=None):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        builder = (x509.CertificateBuilder().subject_name(subject)
                   .issuer_name(issuer_name or subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=5))
                   .not_valid_after(now + datetime.timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None),
                                  critical=True))
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName(san),
                                            critical=False)
        cert = builder.sign(issuer_key or key, hashes.SHA256())
        return key, cert

    ca_key, ca_cert = issue("revl test CA", None, None, ca=True)
    out = {"ca": tmp_path / "ca.pem"}
    out["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    for cn, san in [("server", [x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                x509.DNSName("localhost")])] + [(n, None) for n in names]:
        key, cert = issue(cn, ca_key, ca_cert.subject, san=san)
        (tmp_path / f"{cn}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        pem_key(key, tmp_path / f"{cn}.key")
        out[cn] = (str(tmp_path / f"{cn}.pem"), str(tmp_path / f"{cn}.key"))
    return out


def _client_context(pki, name=None):
    context = ssl.create_default_context(cafile=str(pki["ca"]))
    if name is not None:
        context.load_cert_chain(*pki[name])
    return context


def test_mutual_tls_binds_the_certificate_common_name(serving, tmp_path):
    pki = _pki(tmp_path, ("alice", "mallory"))
    transport = serving(profile="operator alice may lease on *\n"
                                "operator bob may lease on *\n",
                        exposure=Exposure("127.0.0.1", 0, tls_cert=pki["server"][0],
                                          tls_key=pki["server"][1],
                                          tls_client_ca=str(pki["ca"])),
                        auth="mtls")
    port = _port(transport)
    status, body, _ = _rpc(port, "tools/call",
                           {"name": "revl_lease",
                            "arguments": {"action": "claim", "component": "X"}},
                           context=_client_context(pki, "alice"))
    assert status == 200
    assert body["result"]["structuredContent"]["holder"] == "alice"

    # a certificate the CA signed for a name the profile does not declare
    status, _, _ = _rpc(port, "tools/list", context=_client_context(pki, "mallory"))
    assert status == 401
    # a certificate for alice AND a bearer header is two identities
    status, _, _ = _rpc(port, "tools/list", context=_client_context(pki, "alice"),
                        headers={"Authorization": "Bearer anything"})
    assert status == 401
    # no client certificate: the handshake itself is refused
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        _rpc(port, "tools/list", context=_client_context(pki))


def test_mutual_tls_needs_a_client_ca():
    with pytest.raises(TransportError):
        HttpTransport(ServerDispatcher(server),
                      registry=parse_profile("operator a may lease on *\n"),
                      exposure=Exposure("127.0.0.1", 0), auth="mtls")


# ---------------------------------------------------------------- 13: spec shape

def test_get_and_delete_are_405_and_a_session_id_is_ignored(serving):
    transport = serving()
    port = _port(transport)
    for method in ("GET", "DELETE"):
        status, _, headers = _send(port, None, token=SECRETS["alice"], method=method)
        assert status == 405 and headers["allow"] == "POST"
    status, body, headers = _rpc(port, "tools/list", token=SECRETS["alice"],
                                 headers={"Mcp-Session-Id": "session-from-2025"})
    assert status == 200 and "mcp-session-id" not in headers
    assert body["result"]["resultType"] == "complete"
    assert body["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "revl"


def test_a_notification_is_202_and_initialize_names_the_supported_version(serving):
    transport = serving()
    port = _port(transport)
    status, body, _ = _send(port, {"jsonrpc": "2.0", "method": "notifications/x",
                                   "params": {}}, token=SECRETS["alice"],
                            headers={"MCP-Protocol-Version": PROTOCOL_VERSION,
                                     "Mcp-Method": "notifications/x"})
    assert status == 202 and body is None
    status, body, _ = _send(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}},
                            token=SECRETS["alice"])
    assert status == 400 and body["error"]["code"] == -32022
    assert body["error"]["data"]["supported"] == [PROTOCOL_VERSION]


def test_server_discover_describes_the_server(serving):
    transport = serving()
    status, body, _ = _rpc(_port(transport), "server/discover", token=SECRETS["alice"])
    assert status == 200
    result = body["result"]
    assert result["supportedVersions"] == [PROTOCOL_VERSION]
    assert result["resultType"] == "complete"
    assert "listChanged" not in result["capabilities"].get("tools", {})
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "revl"
