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
    face = HttpComposedServer(session, composition="app")
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


def test_a_revocation_takes_effect_on_the_next_request(from_file):
    path, port = from_file
    assert _list(port, "bob")[0] == 200
    path.write_text(_profile({"bob": "operator bob revoked"}), encoding="utf-8")
    status, body = _list(port, "bob")
    assert status == 401
    assert "REVOKED" in body["error"]["message"]
    assert _list(port, "alice")[0] == 200, "only bob was revoked"


def test_an_edit_the_stat_signature_cannot_see_is_still_read(from_file, monkeypatch):
    """The racy-clean rule. On a filesystem with coarse timestamps, two writes in
    one tick can leave mtime, ctime and size all unchanged. The stat below is
    frozen to exactly that, and the edit must still be seen, because the file's
    mtime is within `RACY_NS` of the last read."""
    from revl.mcp import http_transport

    path, port = from_file
    assert _list(port, "bob")[0] == 200
    frozen = os.stat(path)
    real_stat = os.stat

    def stat(target, *args, **kwargs):
        if os.fspath(target) == str(path):
            return frozen
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(http_transport.os, "stat", stat)
    original = "operator bob may lease on *"
    revoked = "operator bob revoked".ljust(len(original))
    path.write_text(path.read_text(encoding="utf-8").replace(original, revoked),
                    encoding="utf-8")
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
    for who in ("alice", "bob"):
        status, body = _list(port, who)
        assert status == 503, (who, status)
        assert "operator profile" in body["error"]["message"]
    # fixing the file restores service without a restart
    path.write_text(_profile({}), encoding="utf-8")
    assert _list(port, "alice")[0] == 200


def test_a_profile_that_cannot_load_at_start_refuses_to_start(tmp_path):
    path = tmp_path / "bad.profile"
    path.write_text("operator x key sha256:not-a-digest\n", encoding="utf-8")
    with pytest.raises(TransportError):
        HttpTransport(ServerDispatcher(server), exposure=Exposure("127.0.0.1", 0),
                      server_module=server, profile_path=str(path))
