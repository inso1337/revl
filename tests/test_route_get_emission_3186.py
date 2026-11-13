"""A `route get` on an emission is refused, and the face refuses the browser
request that has no `Origin` (issue #3186).

A browser sends no `Origin` on a cross-site `<img>`, `<script>`, `<link>`, form
GET or top-level navigation, so the Origin guard passed the request that a page
on any site can fire. The face dispatched it like any other call, and a
state-changing `GET` route on an emission fired from the page the operator was
visiting.

Two layers close it. The compiler refuses `route get`/`route head` on an
operation whose reach is an emission: the declaration itself is the defect, and
every derivation that serves it (`revl serve --http`, the ts emitter, the
exporters) would otherwise carry the exposure. The shared HTTP guard
(`http_guard.request_refusal`) additionally refuses a request whose Fetch
Metadata says a browser made it cross-site, which covers a route served by any
listener.
"""

import http.client
import io
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402
from revl.mcp.http_face import build_http_server  # noqa: E402
from revl.mcp.http_guard import Exposure, request_refusal  # noqa: E402
from test_serve_http import _face  # noqa: E402


def _compile(tmp_path, source: str) -> dict:
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    return compile_files([str(app)])


def _errmsg(exc) -> str:
    err = exc.value
    if isinstance(err, RevlErrors):
        return "\n".join(str(e) for e in err.errors)
    return str(err)


# ---------------------------------------------------------- the compile layer

@pytest.mark.parametrize("method", ["get", "head"])
def test_a_safe_verb_on_an_emission_is_refused(tmp_path, method):
    """The mutating verb is refused by name, and the refusal points at `post`."""
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path,
                 f'service S {{ route {method} "/wipe" emission fn wipe() -> Str }}')
    msg = _errmsg(exc)
    assert "S.wipe" in msg, msg
    assert "emission" in msg, msg
    assert "post" in msg, msg


def test_get_on_a_plain_operation_is_admitted(tmp_path):
    ir = _compile(tmp_path, 'service S { route get "/probe" fn probe() -> Str }')
    assert ir["services"]["S"]["methods"]["probe"]["route"]["method"] == "get"


def test_post_on_an_emission_is_admitted(tmp_path):
    ir = _compile(tmp_path,
                  'service S { route post "/wipe" emission fn wipe() -> Str }')
    assert ir["services"]["S"]["methods"]["wipe"]["route"]["method"] == "post"


def test_delete_on_an_emission_is_admitted(tmp_path):
    """`delete` binds its remaining parameters from the query string like `get`,
    but no page can send it without a preflighted `Origin`; the browser CSRF this
    issue fixes needs a GET."""
    ir = _compile(
        tmp_path,
        'service S { route delete "/wipe" emission fn wipe(q: Str) -> Str }')
    assert ir["services"]["S"]["methods"]["wipe"]["route"]["method"] == "delete"


# ------------------------------------------------------------ the guard layer

def _headers(raw: bytes):
    return http.client.parse_headers(io.BytesIO(raw))


LOOPBACK = Exposure("127.0.0.1", 8080)


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_browser_request_with_no_origin_is_refused(site):
    headers = _headers(f"Host: 127.0.0.1\r\nSec-Fetch-Site: {site}\r\n\r\n".encode())
    refusal = request_refusal(headers, LOOPBACK)
    assert refusal is not None
    assert "Sec-Fetch-Site" in refusal


@pytest.mark.parametrize("site", ["none", "same-origin"])
def test_a_user_navigation_or_same_origin_request_is_served(site):
    headers = _headers(f"Host: 127.0.0.1\r\nSec-Fetch-Site: {site}\r\n\r\n".encode())
    assert request_refusal(headers, LOOPBACK) is None


def test_a_named_origin_still_admits_its_page():
    headers = _headers(b"Host: 127.0.0.1\r\nOrigin: https://notes.example\r\n"
                       b"Sec-Fetch-Site: cross-site\r\n\r\n")
    exposure = Exposure("127.0.0.1", 8080,
                        allow_origins=("https://notes.example",))
    assert request_refusal(headers, exposure) is None


def test_a_repeated_sec_fetch_site_is_refused():
    headers = _headers(b"Host: 127.0.0.1\r\nSec-Fetch-Site: cross-site\r\n"
                       b"Sec-Fetch-Site: none\r\n\r\n")
    assert request_refusal(headers, LOOPBACK) is not None


# ------------------------------------------------------- the wire, end to end

APP = """
service Probe { route get "/probe" fn probe() -> Str }
component ProbeHttp provides probe: Probe {
  provide probe { fn probe() = "ok" }
}
"""


@pytest.fixture
def served():
    face, session = _face(APP, result_value="ok")
    httpd = build_http_server(face, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], session
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(port, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/probe",
                 headers={"Host": f"127.0.0.1:{port}", **headers})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, body


def test_a_cross_site_get_with_no_origin_never_reaches_the_handler(served):
    port, session = served
    status, body = _get(port, {"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert b"forbidden-origin" in body
    assert session.calls == [], "the routed handler never ran"


def test_a_user_navigation_reaches_the_handler(served):
    port, session = served
    status, _ = _get(port, {"Sec-Fetch-Site": "none"})
    assert status == 200
    assert session.calls == [("probe", "probe", [])]
