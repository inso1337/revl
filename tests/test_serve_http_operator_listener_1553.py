"""`revl serve --http` with an operator listener beside it (issue #1553).

Design 569 settled who calls the face: the application's users (option B). Its
operators get a second listener on the same session (option C2), the MCP HTTP
transport of `http_transport`, and that is where an E-Stop (B2) and the answer to
an app-triggered class-(c) ticket (B3) come from. The claims under test:

  * an app caller reaches no operator verb, on the face's port or the
    operator's;
  * an operator credential sent to the face grants nothing there;
  * a class-(c) crossing an app request reaches does not fire until an operator
    approves its ticket on the operator listener, and a revoke there refuses it;
  * an E-Stop on the operator listener halts the app requests in flight;
  * the operator listener is never a browser's, and never the face's port.

Before this change there was no operator listener, `revl serve` had no approval
policy, and a class-(c) crossing an app request reached fired unapproved.

The live tests drive a real cordis-py composition and skip without the pinned
fork; a skip is not a pass.
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
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:  # `runtime`, imported only by cordis tests
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, build_http_server  # noqa: E402
from revl.mcp.http_guard import Exposure, ExposureError  # noqa: E402
from revl.mcp.http_transport import PROTOCOL_VERSION, TransportError  # noqa: E402
from revl.mcp.operator import parse_profile  # noqa: E402
from revl.mcp.operator_listener import (APP_CALLER, OperatorListener,  # noqa: E402
                                        check_operator_exposure)

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SECRETS = {"ops": "ops-secret-0", "reader": "reader-secret-1"}


def _profile() -> str:
    grants = {"ops": "approve, estop, call, fork", "reader": "lease"}
    lines = []
    for token, verbs in grants.items():
        digest = hashlib.sha256(SECRETS[token].encode("utf-8")).hexdigest()
        lines.append(f"operator {token} key sha256:{digest}")
        lines.append(f"operator {token} may {verbs} on *")
    return "\n".join(lines) + "\n"


PROFILE = _profile()

SOURCE = '''
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }
extern emission fn announce(sink: Str, msg: Str) = @py {
    with open(sink, 'a') as _f:
        _f.write('announce:' + msg + '\\n')
    return
}
extern pure fn wait_for(release: Str) -> Int = @py {
    import os, time
    deadline = time.monotonic() + 60
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.02)
    return 1
}
extern pure fn unstash(w: Stash) -> Unit = @py {
    import os
    if os.path.exists(w['bak']):
        os.replace(w['bak'], w['path'])
    return
}
extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError] undo unstash(result) = @py {
    import os
    bak = p + '.bak'
    os.replace(p, bak)
    return Ok({'path': p, 'bak': bak})
}
service Ops {
  emission fn shout(sink: Str, msg: Str)
  emission fn slow_stash(release: Str, p: Str)
}
component Agent provides ops: Ops {
  provide ops {
    fn shout(sink, msg) { emit announce(sink, msg) }
    fn slow_stash(release, p) {
      let w = wait_for(release)
      effect stash_path(p)
    }
  }
}
'''

PUBLIC = {("ops", "shout"), ("ops", "slow_stash")}

# every verb the operator listener serves that an app caller must never reach
OPERATOR_VERBS = ("revl_approve", "revl_revoke", "revl_estop", "revl_state",
                  "revl_call", "revl_unload")


# ---------------------------------------------------------------- clients

def _app(port, path, args=None, *, headers=None, method="POST"):
    """One request to the app face: (status, JSON body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    sent = {"Content-Type": "application/json"}
    sent.update(headers or {})
    body = json.dumps(args).encode("utf-8") if args is not None else None
    conn.request(method, path, body=body, headers=sent)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, (json.loads(payload) if payload else None)


def _mcp(port, name, arguments=None, *, token=None, headers=None):
    """One MCP `tools/call` over HTTP: (status, JSON body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    sent = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "tools/call", "Mcp-Name": name}
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    sent.update(headers or {})
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": name, "arguments": arguments or {},
        "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                  "io.modelcontextprotocol/clientCapabilities": {}}}}
    conn.request("POST", "/mcp", body=json.dumps(message).encode("utf-8"),
                 headers=sent)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, (json.loads(payload) if payload else None)


def _operator(port, who, name, arguments=None):
    """An operator's verdict (`structuredContent`) for one tool call."""
    status, body = _mcp(port, name, arguments, token=SECRETS[who])
    assert status == 200, (status, body)
    return body["result"]["structuredContent"]


def _lines(path) -> list:
    return Path(path).read_text(encoding="utf-8").splitlines() \
        if os.path.exists(path) else []


# ---------------------------------------------------------------- the stack

class _Stack:
    """A live face and its operator listener, as `serve_http` wires them."""

    def __init__(self, tmp_path):
        from revl.mcp.session import Session

        self.session = Session()
        self.session._wal_path = str(tmp_path / "session.wal")
        self.session.approval_policy = "auto"
        self.session.load(compile_source(SOURCE, "ops_1553.rvl"), record=True,
                          origin={"source": SOURCE})
        self.face = HttpComposedServer(self.session, composition="app",
                                       public=PUBLIC)
        self.httpd = build_http_server(self.face, "127.0.0.1", 0)
        self._serving = threading.Thread(target=self.httpd.serve_forever,
                                         daemon=True)
        self._serving.start()
        self.listener = OperatorListener(
            self.face, exposure=Exposure("127.0.0.1", 0),
            app_exposure=self.httpd.exposure, registry=parse_profile(PROFILE),
            server_module=server)
        self.listener.start()
        self.app_port = self.httpd.server_address[1]
        self.op_port = self.listener.exposure.port_in_use

    def close(self):
        import runtime as rt

        self.listener.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self._serving.join(timeout=5)
        try:
            live = self.face.session   # the confirmed branch after a fork
            if live.loaded and not live.halted:
                live.unload()
        finally:
            rt.clear_estop()
            rt._LIVE_FRAMES.clear()


@pytest.fixture
def stack(tmp_path, monkeypatch):
    from revl.mcp.leases import LeaseBook
    from revl.mcp.session import Session

    placeholder = Session()
    placeholder.leases = LeaseBook()
    monkeypatch.setattr(server, "SESSION", placeholder)
    built = _Stack(tmp_path)
    yield built
    built.close()
    assert server.SESSION is placeholder, "the listener put the server back"


def _ask(stack, sink):
    """An app request that reaches a class-(c) crossing: its pending ticket."""
    status, body = _app(stack.app_port, "/app/ops/shout", [sink, "hi"])
    assert status == 403, (status, body)
    assert body["pendingApproval"] is True and body["code"] == "pending_approval"
    return body["ticket"]["hash"]


# ---------------------------------------------------------------- B3

@needs_cordis
def test_an_app_class_c_crossing_fires_only_after_an_operator_approves_it(stack,
                                                                          tmp_path):
    sink = str(tmp_path / "sink.log")
    ticket = _ask(stack, sink)
    assert _lines(sink) == [], "held, not fired"
    # waiting is not approving: the identical re-issue is still held
    assert _ask(stack, sink) == ticket
    assert _lines(sink) == []

    # an operator without `approve` cannot answer it
    refused = _operator(stack.op_port, "reader", "revl_approve", {"hash": ticket})
    assert refused["ok"] is False
    assert _lines(sink) == []

    approved = _operator(stack.op_port, "ops", "revl_approve", {"hash": ticket})
    assert approved["ok"] is True
    status, body = _app(stack.app_port, "/app/ops/shout", [sink, "hi"])
    assert (status, body) == (200, {"ok": True, "value": None})
    assert _lines(sink) == ["announce:hi"], "fired once"
    # the yes was spent: asking again is a new question
    assert _ask(stack, sink) == ticket
    assert _lines(sink) == ["announce:hi"]


@needs_cordis
def test_a_revoke_on_the_operator_listener_refuses_the_held_request(stack, tmp_path):
    sink = str(tmp_path / "sink.log")
    ticket = _ask(stack, sink)
    revoked = _operator(stack.op_port, "ops", "revl_revoke",
                        {"hash": ticket, "reason": "not today"})
    assert revoked["ok"] is True and revoked["outcome"] == "refused"
    assert revoked["by"] == "ops"
    # a revoke closes the round: no yes can follow it
    late = _operator(stack.op_port, "ops", "revl_approve", {"hash": ticket})
    assert late["ok"] is False and "revoked in this round" in \
        late["diagnostics"][0]["message"]

    status, body = _app(stack.app_port, "/app/ops/shout", [sink, "hi"])
    assert status == 403, body
    assert body["approvalRefused"] is True and body["code"] == "approval_refused"
    assert body["ticket"] == {"hash": ticket}
    assert "ops" not in json.dumps(body), "the app is not told who refused"
    assert _lines(sink) == []
    # one no refuses one re-issue; asking again is a new question
    assert _ask(stack, sink) == ticket
    assert _lines(sink) == []


@needs_cordis
def test_a_revoke_withdraws_a_yes_not_yet_spent(stack, tmp_path):
    sink = str(tmp_path / "sink.log")
    ticket = _ask(stack, sink)
    assert _operator(stack.op_port, "ops", "revl_approve", {"hash": ticket})["ok"]
    revoked = _operator(stack.op_port, "ops", "revl_revoke", {"hash": ticket})
    assert revoked["ok"] is True and revoked["withdrewApproval"]
    status, body = _app(stack.app_port, "/app/ops/shout", [sink, "hi"])
    assert status == 403 and body["approvalRefused"] is True
    assert _lines(sink) == []


@needs_cordis
def test_the_pending_answer_carries_the_ticket_id_and_no_way_to_approve(stack,
                                                                        tmp_path):
    status, body = _app(stack.app_port, "/app/ops/shout",
                        [str(tmp_path / "sink.log"), "hi"])
    assert status == 403
    assert set(body) == {"ok", "approvalRequired", "pendingApproval", "code",
                         "message", "ticket"}
    assert set(body["ticket"]) == {"hash"}
    text = json.dumps(body)
    for leak in ("revl_approve", "/mcp", "approve", "Agent", "announce",
                 *SECRETS.values()):
        assert leak not in text, leak
    # the ticket is the session's own, answerable on the operator listener, and
    # the app caller was bound for that one request only
    assert body["ticket"]["hash"] in stack.session._tickets
    assert stack.session.operator.token != APP_CALLER


# ---------------------------------------------------------------- no operator verb for an app caller

@needs_cordis
def test_an_app_caller_reaches_no_operator_verb_on_either_port(stack, tmp_path):
    sink = str(tmp_path / "sink.log")
    ticket = _ask(stack, sink)
    for verb in OPERATOR_VERBS:
        # the face serves no MCP endpoint and no operator route
        status, _ = _mcp(stack.app_port, verb, {"hash": ticket})
        assert status == 404, verb
        status, _ = _app(stack.app_port, f"/app/{verb}", [ticket])
        assert status == 404, verb
        # the operator listener answers nobody without an operator credential:
        # no header, and an app user's own bearer token
        status, _ = _mcp(stack.op_port, verb, {"hash": ticket})
        assert status == 401, verb
        status, _ = _mcp(stack.op_port, verb, {"hash": ticket},
                         token="an-app-users-session-token")
        assert status == 401, verb
    assert stack.session._ledger == [], "nothing was approved"
    assert not stack.session.halted
    assert _ask(stack, sink) == ticket and _lines(sink) == []


@needs_cordis
def test_a_browser_page_cannot_use_the_operator_listener(stack, tmp_path):
    """Even holding a real operator secret: an Origin header is refused, and the
    listener takes no --allow-origin to admit one."""
    sink = str(tmp_path / "sink.log")
    ticket = _ask(stack, sink)
    status, _ = _mcp(stack.op_port, "revl_approve", {"hash": ticket},
                     token=SECRETS["ops"],
                     headers={"Origin": "https://app.example.com"})
    assert status == 403
    assert stack.session._ledger == []
    assert _ask(stack, sink) == ticket and _lines(sink) == []


@needs_cordis
def test_an_operator_credential_on_the_app_port_grants_nothing(stack, tmp_path):
    sink = str(tmp_path / "sink.log")
    ticket = _ask(stack, sink)
    operator = {"Authorization": f"Bearer {SECRETS['ops']}"}
    for verb in ("revl_approve", "revl_estop"):
        status, _ = _mcp(stack.app_port, verb, {"hash": ticket},
                         token=SECRETS["ops"])
        assert status == 404, verb
    # the operator's secret on an app request is only an app request
    status, body = _app(stack.app_port, "/app/ops/shout", [sink, "hi"],
                        headers=operator)
    assert status == 403 and body["pendingApproval"] is True
    assert stack.session._ledger == []
    assert not stack.session.halted
    assert _lines(sink) == []


# ---------------------------------------------------------------- B2

@needs_cordis
def test_an_estop_on_the_operator_listener_halts_the_app_requests_in_flight(
        stack, tmp_path):
    """The first app request is inside its host code and holds the session; a
    second is queued behind it. The operator's E-Stop does not wait for either:
    it latches, the request in flight is refused at its next crossing, the
    queued one is never dispatched, and every later one is refused."""
    release = tmp_path / "release"
    first, queued = tmp_path / "first.txt", tmp_path / "queued.txt"
    first.write_text("1", encoding="utf-8")
    queued.write_text("2", encoding="utf-8")
    replies = {}

    def call(name, target):
        replies[name] = _app(stack.app_port, "/app/ops/slow_stash",
                             [str(release), str(target)])

    in_flight = threading.Thread(target=call, args=("first", first))
    in_flight.start()
    deadline = time.monotonic() + 30
    while not stack.face.dispatch_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stack.face.dispatch_lock.locked(), "the first request holds the session"
    behind = threading.Thread(target=call, args=("queued", queued))
    behind.start()
    time.sleep(0.3)
    try:
        started = time.monotonic()
        halted = _operator(stack.op_port, "ops", "revl_estop", {"reason": "drill"})
        assert time.monotonic() - started < 10, "the E-Stop did not queue"
        assert halted["ok"] is True and halted["latched"] is True
        assert in_flight.is_alive() and behind.is_alive()
    finally:
        release.write_text("go", encoding="utf-8")
        in_flight.join(timeout=60)
        behind.join(timeout=60)

    status, body = replies["first"]
    assert status == 503 and body["halted"] is True, replies["first"]
    status, body = replies["queued"]
    assert status == 503 and body["halted"] is True, replies["queued"]
    assert queued.exists(), "the queued request never ran"
    assert _app(stack.app_port, "/app/ops/shout",
                [str(tmp_path / "sink.log"), "late"])[0] == 503
    report = _operator(stack.op_port, "ops", "revl_estop_report")
    assert report["halted"] is True and report["operator"] == "ops"


@needs_cordis
def test_the_app_face_never_serves_an_estop(stack, tmp_path):
    for token in (None, SECRETS["ops"]):
        status, _ = _mcp(stack.app_port, "revl_estop", {"reason": "app"}, token=token)
        assert status == 404
    assert not stack.session.halted and not stack.face.halted()
    status, body = _app(stack.app_port, "/app/ops/shout",
                        [str(tmp_path / "sink.log"), "hi"])
    assert status == 403 and body["pendingApproval"] is True


# ---------------------------------------------------------------- fork

@needs_cordis
def test_after_a_fork_on_the_operator_listener_the_face_serves_the_branch(
        stack, tmp_path):
    """`revl_fork_confirm` freezes the parent and makes the branch the only live
    continuation. The face follows the session the operator listener holds, so
    an app request after the fork runs on the branch, not the frozen parent."""
    release = tmp_path / "release"
    release.write_text("go", encoding="utf-8")
    first = tmp_path / "first.txt"
    first.write_text("1", encoding="utf-8")
    assert _app(stack.app_port, "/app/ops/slow_stash",
                [str(release), str(first)])[0] == 200
    report = _operator(stack.op_port, "ops", "revl_fork", {"at": 1})
    assert report["ok"] is True and report.get("hash"), report
    confirmed = _operator(stack.op_port, "ops", "revl_fork_confirm",
                          {"hash": report["hash"]})
    assert confirmed["ok"] is True and confirmed["forked"] is True, confirmed
    branch = server.SESSION
    assert branch is not stack.session and stack.session._frozen

    sink = str(tmp_path / "sink.log")
    ticket = _ask(stack, sink)
    assert stack.face.session is branch
    assert ticket in branch._tickets, "the branch raised the ticket"
    assert ticket not in stack.session._tickets
    # and the branch is the session the operator answers on
    assert _operator(stack.op_port, "ops", "revl_approve", {"hash": ticket})["ok"]
    assert _app(stack.app_port, "/app/ops/shout", [sink, "hi"])[0] == 200
    assert _lines(sink) == ["announce:hi"]


# ---------------------------------------------------------------- wiring

@needs_cordis
def test_both_listeners_share_one_session_and_one_lock(stack):
    assert stack.face.dispatch_lock is stack.listener.transport.binding.lock
    assert server.SESSION is stack.session


def test_the_operator_listener_refuses_the_face_port_and_any_origin():
    app = Exposure("127.0.0.1", 8080)
    with pytest.raises(ExposureError, match="app face's port 8080"):
        check_operator_exposure(Exposure("127.0.0.1", 8080), app)
    with pytest.raises(ExposureError, match="never called from a browser"):
        check_operator_exposure(
            Exposure("127.0.0.1", 8471, allow_origins=("https://app.example.com",)),
            app)
    with pytest.raises(ExposureError, match="without TLS"):
        check_operator_exposure(Exposure("10.0.0.5", 8471), app)
    check_operator_exposure(Exposure("127.0.0.1", 8471), app)


def test_the_operator_listener_needs_a_profile():
    with pytest.raises(TransportError, match="--operator-profile"):
        OperatorListener(object(), exposure=Exposure("127.0.0.1", 0))


def test_the_cli_refuses_an_operator_listener_it_cannot_serve(tmp_path, capsys):
    """Each refused before the composition boots (these need no runtime)."""
    from revl.__main__ import main

    app = tmp_path / "app.rvl"
    app.write_text("service S { fn f() -> Int }\n"
                   "component C provides s: S { provide s { fn f() = 1 } }\n",
                   encoding="utf-8")
    profile = tmp_path / "ops.profile"
    profile.write_text(PROFILE, encoding="utf-8")
    cases = [
        (["--operator-listen", "127.0.0.1:8471"], "needs --operator-profile"),
        (["--operator-profile", str(profile)], "needs --operator-listen"),
        (["--operator-listen", "127.0.0.1:8080", "--operator-profile", str(profile)],
         "app face's port 8080"),
        (["--operator-listen", "10.0.0.5:8471", "--operator-profile", str(profile)],
         "without TLS"),
    ]
    for extra, why in cases:
        code = main(["serve", "--http", str(app), "--port", "8080", *extra])
        err = capsys.readouterr().err
        assert code != 0, extra
        assert why in err, (extra, err)
    code = main(["serve", "--mcp", str(app), "--approval-policy", "auto"])
    assert code == 2 and "revl serve --http` only" in capsys.readouterr().err


ROUTED = '''
extern emission fn announce(msg: Str) = @py {
    import os
    with open(os.environ['REVL_1553_SINK'], 'a') as _f:
        _f.write('announce:' + msg + '\\n')
    return
}
service Shout {
  route post "/shout/{msg}"
  emission fn shout(msg: Str)
}
component Crier provides shout: Shout {
  provide shout {
    fn shout(msg) { emit announce(msg) }
  }
}
'''


def _started_ports(process) -> tuple[int, int]:
    """The app and operator ports `revl serve --http` prints at start."""
    import re

    app = op = None
    deadline = time.monotonic() + 120
    lines = []
    while (app is None or op is None) and time.monotonic() < deadline:
        line = process.stderr.readline()
        if not line:
            break
        lines.append(line)
        found = re.search(r"revl serve --http: \S+ on http://127\.0\.0\.1:(\d+)", line)
        if found:
            app = int(found.group(1))
        found = re.search(r"operator listener: MCP on http://127\.0\.0\.1:(\d+)/mcp",
                          line)
        if found:
            op = int(found.group(1))
    assert app and op, "".join(lines)
    return app, op


@needs_cordis
def test_revl_serve_http_holds_an_app_crossing_for_the_operator_listener(tmp_path):
    """The CLI end to end: `--approval-policy auto --operator-listen` on one
    process. Before, `revl serve` had neither option and the crossing fired."""
    import subprocess

    app = tmp_path / "crier.rvl"
    app.write_text(ROUTED, encoding="utf-8")
    profile = tmp_path / "ops.profile"
    profile.write_text(PROFILE, encoding="utf-8")
    sink = tmp_path / "sink.log"
    env = {**os.environ, "REVL_1553_SINK": str(sink),
           "REVL_WAL_DIR": str(tmp_path / "wal"),
           "PYTHONPATH": os.pathsep.join(
               [str(ROOT / "src")] + ([os.environ["PYTHONPATH"]]
                                      if os.environ.get("PYTHONPATH") else []))}
    env.pop("REVL_ESTOP_LATCH", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "revl", "serve", "--http", str(app), "--port", "0",
         "--approval-policy", "auto", "--operator-listen", "127.0.0.1:0",
         "--operator-profile", str(profile)],
        cwd=str(tmp_path), env=env, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
        text=True)
    try:
        app_port, op_port = _started_ports(process)
        status, body = _app(app_port, "/shout/hi")
        assert status == 403 and body["code"] == "pending_approval", body
        assert _lines(sink) == []
        ticket = body["ticket"]["hash"]
        assert _operator(op_port, "ops", "revl_approve", {"hash": ticket})["ok"]
        assert _app(app_port, "/shout/hi")[0] == 204
        assert _lines(sink) == ["announce:hi"]
        halted = _operator(op_port, "ops", "revl_estop", {"reason": "drill"})
        assert halted["ok"] is True
        status, body = _app(app_port, "/shout/hi")
        assert status == 503 and body["code"] == "halted"
        assert _lines(sink) == ["announce:hi"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        process.stderr.close()
