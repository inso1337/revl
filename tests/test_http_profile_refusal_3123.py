"""A broken operator profile tells a caller nothing about the file (#3123).

While the profile is settling or broken, the HTTP transport answers `503`. That
text used to quote the profile's absolute path and the parser error, and the
parser error quotes the offending line verbatim, so a caller with no credential
at all learned the server's filesystem layout and the line being edited, which
is typically a `key sha256:` line. The body was also read (up to 4 MB) before
the refusal.

The refusal is now fixed text. The path and parser error go to the operator's
stderr log, and the body is read only for a caller that authenticated against
the last adopted profile, because only that caller can send an E-Stop, the one
verb never fenced by a profile edit.
"""

import hashlib
import http.client
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server  # noqa: E402
from revl.mcp.http_guard import Exposure  # noqa: E402
from revl.mcp.http_transport import (PROTOCOL_VERSION, HttpTransport,  # noqa: E402
                                     ServerDispatcher)

SECRET = "alice-secret-3123"
#: The digest on the rejected line, a `key sha256:` credential being edited.
LEAKED_DIGEST = hashlib.sha256(b"the line being edited").hexdigest()
#: The wire contract, written out rather than imported: a test that compares
#: against the same constant the code sends would pass if both changed.
EXPECTED = "the operator profile is not available, so the request is refused"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _profile(extra: str = "") -> str:
    return (f"operator alice key sha256:{_digest(SECRET)}\n"
            f"operator alice may lease on *\n" + extra)


#: A line the parser rejects, quoting a fresh `key` digest being edited.
_BROKEN_LINE = f"operator agent bogus-directive {LEAKED_DIGEST}\n"


def _list(port, token=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {}}}}
    headers = {"Content-Type": "application/json",
               "MCP-Protocol-Version": PROTOCOL_VERSION,
               "Mcp-Method": "tools/list"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/mcp", body=json.dumps(body).encode("utf-8"),
                 headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read() or b"null")
    conn.close()
    return response.status, payload


@pytest.fixture
def transport(tmp_path, monkeypatch):
    from revl.mcp.session import Session

    monkeypatch.setattr(server, "SESSION", Session())
    path = tmp_path / "ops.profile"
    path.write_text(_profile(), encoding="utf-8")
    httpd = HttpTransport(ServerDispatcher(server),
                          exposure=Exposure("127.0.0.1", 0),
                          server_module=server, profile_path=str(path),
                          profile_settle_ms=0)
    yield path, httpd
    httpd.stop()


def test_a_broken_profile_tells_a_caller_without_a_credential_nothing(
        transport, capsys):
    path, httpd = transport
    httpd.start()
    port = httpd.exposure.port_in_use
    path.write_text(_profile(_BROKEN_LINE), encoding="utf-8")
    # the first request sees the edit settle, the second reads it broken; both
    # are refused the same way
    replies = [_list(port) for _ in range(2)]
    # a caller with a credential the last adopted profile knows is refused too
    replies += [_list(port, SECRET) for _ in range(2)]
    for status, payload in replies:
        assert status == 503, payload
        text = json.dumps(payload)
        assert payload["error"]["message"] == EXPECTED, payload
        assert str(path) not in text, "the caller learned the profile path"
        assert LEAKED_DIGEST not in text, "the caller learned the rejected line"
        assert "bogus-directive" not in text
        assert "agent" not in text
        assert "expected" not in text, "the caller learned the parser error"
    # the operator still gets the path and the rejected line, in its own log,
    # for the settling state and for the broken one
    logged = capsys.readouterr().err
    assert str(path) in logged, "the operator log keeps the profile path"
    assert LEAKED_DIGEST in logged, "the operator log keeps the rejected line"
    assert "changing" in logged, "the settling state was exercised"
    assert "does not parse" in logged, "the broken state was exercised"


def test_each_outage_logs_its_reason_once(transport, capsys):
    """One line per distinct reason, and a readable profile, even through a
    refused credential, starts the next outage."""
    path, httpd = transport
    httpd.start()
    port = httpd.exposure.port_in_use

    def break_profile():
        path.write_text(_profile(_BROKEN_LINE), encoding="utf-8")
        # changing, broken, broken again: the repeated broken read is deduped
        for _ in range(3):
            assert _list(port)[0] == 503

    break_profile()
    first = capsys.readouterr().err
    assert first.count("revl mcp: every request is refused:") == 2, first
    path.write_text(_profile(), encoding="utf-8")
    assert _list(port)[0] == 503, "the healthy edit is still settling"
    assert _list(port)[0] == 401, "readable, though this credential is refused"
    capsys.readouterr()
    break_profile()
    second = capsys.readouterr().err
    assert second.count("revl mcp: every request is refused:") == 2, second


def test_a_second_credential_line_does_not_name_the_operator(transport):
    """The other parse error the issue names: a second credential for one
    operator names that operator and the rule text."""
    path, httpd = transport
    httpd.start()
    port = httpd.exposure.port_in_use
    path.write_text(_profile(f"operator alice key sha256:{_digest('second')}\n"),
                    encoding="utf-8")
    for _ in range(2):
        status, payload = _list(port)
        assert status == 503, payload
        text = json.dumps(payload)
        assert payload["error"]["message"] == EXPECTED, payload
        assert str(path) not in text
        assert "already declares a vote credential" not in text
        assert "alice" not in text, "the caller learned the operator name"


class _Headers:
    """The `get`/`get_all` view `_frame_request` expects."""

    def __init__(self, values):
        self._values = dict(values)

    def get_all(self, name):
        for key, value in self._values.items():
            if key.lower() == name.lower():
                return [value]
        return None

    def get(self, name):
        values = self.get_all(name)
        return values[0] if values else None


class _UnreadBody:
    """A request whose body no refusal may read."""

    def __init__(self):
        self.headers = _Headers({"Host": "127.0.0.1",
                                 "Content-Length": str(4 * 1024 * 1024)})
        self.path = "/mcp"
        self.request = object()
        self.rfile = self

    def read(self, length):
        raise AssertionError(
            "the transport read an unauthenticated caller's body before refusing")


def test_a_broken_profile_refuses_before_reading_an_unauthenticated_body(
        transport):
    path, httpd = transport
    path.write_text(_profile(_BROKEN_LINE), encoding="utf-8")
    # settle 0 still needs the second read to adopt the broken content
    statuses = [httpd.respond(_UnreadBody(), "POST")[0] for _ in range(2)]
    assert statuses == [503, 503]
