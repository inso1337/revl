"""`revl mcp serve` runs the approval gate by default, with no self-approval
(issue #1706).

With no flag the effect-class gate (item 246) runs: a class-(c) crossing returns
a ticket and fires nothing. The identity that raised a ticket cannot approve it,
so a stdio session with no operator profile can raise tickets but never answer
them, and approval needs a separate operator identity (over `--http`, each
request's authenticated operator). `--approval-policy advisory` is what `auto`
meant before, and `--approval-policy off` is the old no-policy default; each
says so at startup.

The modes:

    no flag / --approval-policy auto   gate on, raiser cannot approve
    --approval-policy advisory         gate on, raiser may approve (old `auto`)
    --approval-policy off              gate off (old default), startup warning

The CLI and `initialize` tests need no runtime. The ones that boot a composition
need the cordis-py runtime and skip without it, like every session test.
"""

import copy
import hashlib
import http.client
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:  # `runtime`, imported only by cordis tests
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402
from revl.cli.interop import (_bind_session_authority,  # noqa: E402
                              _resolve_serve_approval_mode)
from revl.cli.parser import build_parser  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.http_guard import Exposure  # noqa: E402
from revl.mcp.http_transport import (PROTOCOL_VERSION, HttpTransport,  # noqa: E402
                                     ServerDispatcher)
from revl.mcp.operator import parse_profile  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

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

SECRETS = {"agent": "agent-secret-1706", "human": "human-secret-1706"}
# the agent holds `approve` too: separation, not the profile, is what stops it
# answering its own ticket
GRANTS = {"agent": "call, approve, load", "human": "approve"}


def _profile() -> str:
    lines = []
    for token, verbs in GRANTS.items():
        digest = hashlib.sha256(SECRETS[token].encode("utf-8")).hexdigest()
        lines.append(f"operator {token} key sha256:{digest}")
        lines.append(f"operator {token} may {verbs} on *")
    return "\n".join(lines) + "\n"


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
        if session.loaded and not session.halted:
            session.unload()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


def _why(verdict) -> str:
    """The refusal message of a session-error verdict."""
    return " ".join(d.get("message", "") for d in verdict.get("diagnostics", ()))


def _serve_args(*argv):
    args = build_parser().parse_args(["mcp", "serve", *argv])
    _resolve_serve_approval_mode(args)
    return args


# ---------------------------------------------------------------- the CLI


def test_no_flag_binds_the_gate_with_separation(fresh, capsys):
    args = _serve_args()
    assert _bind_session_authority(args) is None
    assert fresh.approval_policy == "auto"
    assert fresh.approval_separation is True
    err = capsys.readouterr().err
    assert "approval gate: on (--approval-policy auto)" in err
    assert "cannot approve it" in err and "--operator-profile" in err
    assert "--approval-policy off" in err
    # the pre-#1706 self-approvable warning does not apply: separation closes it
    assert "advisory, not a gate" not in err


def test_explicit_auto_is_the_default(fresh, capsys):
    assert _bind_session_authority(_serve_args("--approval-policy", "auto")) is None
    assert (fresh.approval_policy, fresh.approval_separation) == ("auto", True)


def test_off_is_the_old_no_policy_mode_and_warns(fresh, capsys):
    assert _bind_session_authority(_serve_args("--approval-policy", "off")) is None
    assert fresh.approval_policy is None
    assert fresh.approval_separation is False
    err = capsys.readouterr().err
    assert "warning: --approval-policy off: the approval gate is OFF" in err
    assert "fires unprompted" in err


def test_advisory_is_the_old_auto_and_says_the_raiser_may_approve(fresh, capsys):
    assert _bind_session_authority(_serve_args("--approval-policy",
                                               "advisory")) is None
    assert (fresh.approval_policy, fresh.approval_separation) == ("auto", False)
    err = capsys.readouterr().err
    assert "approval gate: advisory" in err
    assert "may approve it" in err
    # the pre-#1706 self-approvable warning still names the hole
    assert "advisory, not a gate" in err


def test_an_unknown_mode_is_refused():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mcp", "serve", "--approval-policy", "nope"])


def test_initialize_says_the_gate_is_on_and_who_may_approve(fresh):
    fresh.approval_policy = "auto"
    fresh.approval_separation = True
    out = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {}})
    text = out["result"]["instructions"]
    assert "The approval gate is on" in text
    assert "You cannot approve a ticket you raised" in text


# ---------------------------------------------------------------- stdio, no flags


class _Stdio:
    """`revl mcp serve` over stdio, one request at a time."""

    def __init__(self, tmp_path, argv=()):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "src"), str(_BACKEND), env.get("PYTHONPATH", "")])
        env["REVL_WAL_DIR"] = str(tmp_path / "wal")
        self.err_path = tmp_path / "serve.err"
        self._err = open(self.err_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "revl", "mcp", "serve", *argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err,
            text=True, env=env, cwd=str(tmp_path))
        self._id = 0
        self.rpc("initialize", {})

    def rpc(self, method, params):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                          "method": method,
                                          "params": params}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, self.stderr()
        return json.loads(line)

    def tool(self, name, arguments):
        reply = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return reply["result"]["structuredContent"]

    def stderr(self):
        self._err.flush()
        return self.err_path.read_text(encoding="utf-8")

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=60)
        self._err.close()


@pytest.fixture
def stdio(tmp_path):
    opened = []

    def start(*argv):
        client = _Stdio(tmp_path, argv)
        opened.append(client)
        return client

    yield start
    for client in opened:
        client.close()


@needs_cordis
def test_no_flag_class_c_call_returns_a_ticket_and_self_approval_is_refused(
        stdio, tmp_path):
    # no approval flag; `--author-trust trusted` only lets this test load its
    # own `@py` fixture
    serve = stdio("--author-trust", "trusted")
    sink = str(tmp_path / "sink.log")
    call = {"key": "ops", "method": "shout", "args": [sink, "a"]}
    loaded = serve.tool("revl_load", {"source": _SHOUT})
    assert loaded["ok"] is True, loaded
    asked = serve.tool("revl_call", call)
    assert asked["approvalRequired"] is True, asked
    assert not os.path.exists(sink), "a class-(c) call with no flags fired"

    ticket = asked["ticket"]["hash"]
    approved = serve.tool("revl_approve", {"hash": ticket})
    assert approved["ok"] is False
    assert "cannot approve it" in _why(approved), approved
    assert "no operator profile is bound" in _why(approved)
    granted = serve.tool("revl_approve", {"hash": ticket, "uses": 3})
    assert granted["ok"] is False and "cannot approve it" in _why(granted)
    proactive = serve.tool("revl_approve", {"capability": "announce", "uses": 3})
    assert proactive["ok"] is False
    assert "no operator profile is bound" in _why(proactive), proactive
    again = serve.tool("revl_call", call)
    assert again["approvalRequired"] is True, "a refused approval minted nothing"
    assert not os.path.exists(sink)
    assert "approval gate: on" in serve.stderr()


@needs_cordis
def test_off_restores_the_old_ungated_behaviour(stdio, tmp_path):
    serve = stdio("--author-trust", "trusted", "--approval-policy", "off")
    sink = str(tmp_path / "sink.log")
    loaded = serve.tool("revl_load", {"source": _SHOUT})
    assert loaded["ok"] is True, loaded
    fired = serve.tool("revl_call",
                       {"key": "ops", "method": "shout", "args": [sink, "a"]})
    assert fired["ok"] is True and "approvalRequired" not in fired, fired
    assert Path(sink).read_text(encoding="utf-8") == "announce:a\n"
    assert "the approval gate is OFF" in serve.stderr()


# ---------------------------------------------------------------- an operator approves


def _load_gated(session):
    """The session as `revl mcp serve` binds it with no flag."""
    session.approval_policy = "auto"
    session.approval_separation = True
    session.load(copy.deepcopy(compile_source(_SHOUT, "shout.rvl")), record=True,
                 origin={"source": _SHOUT})


def _http_tool(port, who, name, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments,
                       "_meta": {"io.modelcontextprotocol/protocolVersion":
                                 PROTOCOL_VERSION,
                                 "io.modelcontextprotocol/clientCapabilities": {}}}}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    conn.request("POST", "/mcp", body=json.dumps(body).encode("utf-8"), headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {SECRETS[who]}",
        "MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Method": "tools/call",
        "Mcp-Name": name})
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    assert response.status == 200, payload
    return payload["result"]["structuredContent"]


@needs_cordis
def test_the_raiser_cannot_approve_and_a_separate_operator_can(fresh, tmp_path):
    _load_gated(fresh)
    transport = HttpTransport(ServerDispatcher(server),
                              registry=parse_profile(_profile()),
                              exposure=Exposure("127.0.0.1", 0), auth="bearer",
                              server_module=server)
    transport.start()
    try:
        port = transport.exposure.port_in_use
        sink = str(tmp_path / "sink.log")
        call = {"key": "ops", "method": "shout", "args": [sink, "a"]}
        asked = _http_tool(port, "agent", "revl_call", call)
        assert asked["approvalRequired"] is True
        ticket = asked["ticket"]["hash"]
        assert not os.path.exists(sink)

        # the agent may `approve` by its grants, and is still refused: it raised
        # this ticket
        own = _http_tool(port, "agent", "revl_approve", {"hash": ticket})
        assert own["ok"] is False
        assert "raised by operator `agent`" in _why(own), own
        proactive = _http_tool(port, "agent", "revl_approve",
                               {"capability": "announce", "uses": 1})
        assert proactive["ok"] is False
        assert "may make calls" in _why(proactive), proactive
        assert not os.path.exists(sink)

        approved = _http_tool(port, "human", "revl_approve", {"hash": ticket})
        assert approved["ok"] is True, approved
        fired = _http_tool(port, "agent", "revl_call", call)
        assert fired["ok"] is True, fired
        assert Path(sink).read_text(encoding="utf-8") == "announce:a\n"
    finally:
        transport.stop()


@needs_cordis
def test_an_operator_that_did_not_raise_the_ticket_may_grant_from_it(fresh,
                                                                     tmp_path):
    registry = parse_profile(_profile())
    _load_gated(fresh)
    sink = str(tmp_path / "sink.log")
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import SessionError

    fresh.operator = registry.get("agent")
    with pytest.raises(ApprovalRequired) as exc:
        fresh.call("ops", "shout", [sink, "a"])
    ticket = exc.value.ticket["hash"]
    with pytest.raises(SessionError, match="cannot approve it"):
        fresh.mint_standing_grant(ticket_hash=ticket, uses=2)

    fresh.operator = registry.get("human")
    grant = fresh.mint_standing_grant(ticket_hash=ticket, uses=2)
    assert grant
    fresh.operator = registry.get("agent")
    fresh.call("ops", "shout", [sink, "a"])
    assert Path(sink).read_text(encoding="utf-8") == "announce:a\n"


@needs_cordis
def test_without_separation_the_python_session_default_is_unchanged(fresh,
                                                                    tmp_path):
    # the Session object's default is unchanged: separation is a serve binding
    from revl.mcp.approval import ApprovalRequired

    fresh.approval_policy = "auto"
    fresh.load(copy.deepcopy(compile_source(_SHOUT, "shout.rvl")), record=True)
    sink = str(tmp_path / "sink.log")
    with pytest.raises(ApprovalRequired) as exc:
        fresh.call("ops", "shout", [sink, "a"])
    fresh.approve_ticket(exc.value.ticket["hash"])
    fresh.call("ops", "shout", [sink, "a"])
    assert Path(sink).read_text(encoding="utf-8") == "announce:a\n"


@needs_cordis
def test_a_ticket_raised_by_a_failed_load_keeps_its_raiser(fresh, tmp_path):
    # the activation body emits, so the load raises a ticket and does not boot;
    # the failed load puts the session back, but the ticket stays answerable,
    # and its raiser must stay known or the raiser could approve it
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import SessionError

    sink = str(tmp_path / "boot.log")
    source = (
        "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('announce:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        "component Boot provides ops: Ops {\n"
        f"  emit announce(\"{sink}\", \"boot\")\n"
        "  provide ops { fn ping() = 1 }\n"
        "}\n")
    fresh.approval_policy = "auto"
    fresh.approval_separation = True
    with pytest.raises(ApprovalRequired) as exc:
        fresh.load(compile_source(source, "boot.rvl"), record=True)
    ticket = exc.value.ticket["hash"]
    assert not fresh.loaded and not os.path.exists(sink)
    with pytest.raises(SessionError, match="cannot approve it"):
        fresh.approve_ticket(ticket)


@needs_cordis
def test_advisory_lets_the_raiser_approve_its_own_ticket(stdio, tmp_path):
    serve = stdio("--author-trust", "trusted", "--approval-policy", "advisory")
    sink = str(tmp_path / "sink.log")
    call = {"key": "ops", "method": "shout", "args": [sink, "a"]}
    assert serve.tool("revl_load", {"source": _SHOUT})["ok"] is True
    asked = serve.tool("revl_call", call)
    assert asked["approvalRequired"] is True and not os.path.exists(sink)
    approved = serve.tool("revl_approve", {"hash": asked["ticket"]["hash"]})
    assert approved["ok"] is True, approved
    assert serve.tool("revl_call", call)["ok"] is True
    assert Path(sink).read_text(encoding="utf-8") == "announce:a\n"
    assert "approval gate: advisory" in serve.stderr()
