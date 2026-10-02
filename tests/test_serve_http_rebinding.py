"""`revl serve --http` refuses DNS-rebinding requests and clear-text exposure.

The reproducer, on the tree before issue #1463's slice 1: a composition served on
loopback answered a request carrying a foreign `Host` and `Origin`, which is what
a web page gets when it re-points its own name at 127.0.0.1. A `text/plain` POST
needs no CORS preflight, so the page reached an `emission` operation:

    POST /app/bus/send   Host: rebind.attacker.example:<port>
                         Origin: http://rebind.attacker.example:<port>
    -> 200 {"ok": true, "value": 1}, and the session ran bus.send

The face now shares `revl.mcp.http_guard` with the MCP HTTP transport: the Host
must be one of the listener's own names, an Origin must be allowed by name, and
a non-loopback bind needs TLS.
"""

import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.mcp.http_face import build_http_server  # noqa: E402
from revl.mcp.http_guard import Exposure, ExposureError  # noqa: E402
from test_serve_http import BUS, _face  # noqa: E402


@pytest.fixture
def served():
    started = []

    def serve(exposure=None):
        face, session = _face(BUS, result_value=1)
        httpd = build_http_server(face, "127.0.0.1", 0, exposure=exposure)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started.append((httpd, thread))
        return httpd.server_address[1], session

    yield serve
    for httpd, thread in started:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _send(port, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/app/bus/send", body=b'["from a web page"]',
                 headers={"Content-Type": "text/plain", **headers})
    response = conn.getresponse()
    body = json.loads(response.read())
    conn.close()
    return response.status, body


def test_a_rebinding_request_is_refused_and_reaches_nothing(served):
    port, session = served()
    evil = f"rebind.attacker.example:{port}"
    status, body = _send(port, {"Host": evil, "Origin": f"http://{evil}"})
    assert status == 403
    assert body["ok"] is False
    assert session.calls == [], "the emission operation never ran"


@pytest.mark.parametrize("headers", [
    {"Origin": "http://rebind.attacker.example"},          # right Host, page Origin
    {"Host": "rebind.attacker.example"},                   # no Origin, wrong Host
])
def test_either_half_alone_is_refused(served, headers):
    port, session = served()
    status, _ = _send(port, headers)
    assert status == 403
    assert session.calls == []


def test_a_loopback_client_is_still_served(served):
    port, session = served()
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        status, body = _send(port, {"Host": host})
        assert status == 200 and body == {"ok": True, "value": 1}
    assert len(session.calls) == 2


def test_a_named_origin_is_served(served):
    port, session = served(Exposure("127.0.0.1", 0,
                                    allow_origins=("https://notes.example",)))
    status, _ = _send(port, {"Origin": "https://notes.example"})
    assert status == 200
    assert session.calls


def test_a_non_loopback_bind_without_tls_refuses_to_start():
    face, _ = _face(BUS)
    with pytest.raises(ExposureError, match="without TLS"):
        build_http_server(face, "0.0.0.0", 0,
                          exposure=Exposure(allow_hosts=("notes.example",)))


def test_the_cli_refuses_a_clear_text_public_bind(tmp_path):
    source = tmp_path / "bus.rvl"
    source.write_text(BUS, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    done = subprocess.run(
        [sys.executable, "-m", "revl", "serve", "--http", "--host", "0.0.0.0",
         "--allow-host", "notes.example", str(source)],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path), env=env)
    assert done.returncode == 1, done.stderr
    assert "without TLS" in done.stderr
