"""The `Bearer` binding is duplicate-visible: one request presents ONE credential.

The routed HTTP face binds a `Bearer` parameter from the `Authorization` header.
That reader used `.get()`, which answers with the FIRST occurrence and cannot
see a doubled field at all -- the blind spot `_frame_request`'s own docstring
names, and the reason that function uses `get_all`. The same request's `Request`
escape hatch carries EVERY header (`stdlib/http.rvl`'s `Header` list exists
because "a real request may repeat a header name... a plain map loses both"), so
one request exposed two disagreeing views of its credential: the first field
when a handler read `auth.token`, and both fields when it read `req.headers`.

That divergence is the defect, not the duplicate itself. An intermediary that
appends a credential it validated AFTER a client-supplied one hands the first
field the authority of the second, and the two readers disagree about which one
that is. A request presenting more than one credential presents no unambiguous
one, so the face now makes no claim and the handler's `validate` denies -- the
answer a missing credential already gets.

Driven over a REAL loopback socket: the premise is that two `Authorization`
fields survive the request head, which a constructed dict cannot show.
"""

import email
import http.client
import io
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.mcp.http_face import (  # noqa: E402
    HttpComposedServer,
    _bearer_token,
    _header_values,
    build_http_server,
)

APP = """\
type Bearer = { token: Opt[Str] }
type Note = { id: Str }

service S {
  route get "/notes/{id}"
  fn get_note(auth: Bearer, id: Str) -> Note
}

component H provides s: S {
  provide s { fn get_note(auth, id) = { id: id } }
}
"""

ONE = b"Authorization: Bearer tok-1\r\n"
TWO = b"Authorization: Bearer forged-first\r\n" \
      b"Authorization: Bearer validated-second\r\n"


class _Stub:
    """A runtime-free session that records every call, so a test can assert what
    the handler was handed."""

    def __init__(self, ir):
        self.ir = ir
        self.calls = []

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        return {"result": {"id": "7"}, "trace": []}

    def state(self, drain=False):
        return {}


class _Serving:
    def __init__(self):
        self.stub = None
        self.port = None
        self._httpd = None
        self._thread = None


def _serving(tmp_path):
    app = tmp_path / "auth.rvl"
    app.write_text(APP, encoding="utf-8")
    stub = _Stub(compile_files([str(app)]))
    face = HttpComposedServer(stub, composition="revl")
    httpd = build_http_server(face, "127.0.0.1", 0)
    serving = _Serving()
    serving.stub = stub
    serving.port = httpd.server_address[1]
    serving._httpd = httpd
    serving._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving._thread.start()
    return serving


def _stop(serving):
    serving._httpd.shutdown()
    serving._httpd.server_close()
    serving._thread.join(timeout=2)


def _request(serving, auth: bytes, path: str = "/notes/7"):
    """Send one request verbatim and return `(status, calls)`."""
    del serving.stub.calls[:]
    payload = (b"GET " + path.encode() + b" HTTP/1.1\r\nHost: x\r\n"
               + auth + b"Connection: close\r\n\r\n")
    sock = socket.create_connection(("127.0.0.1", serving.port), timeout=3)
    sock.settimeout(3)
    sock.sendall(payload)
    blob = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            blob += chunk
    except socket.timeout:
        pass
    finally:
        sock.close()
    assert blob.startswith(b"HTTP/1."), blob[:200]
    return int(blob.split(b" ")[1]), list(serving.stub.calls)


def _request_head(auth: bytes):
    """The exact header object `BaseHTTPRequestHandler` hands the handler:
    `http.client.parse_headers` over the header lines, so a doubled field stays
    doubled and names compare case-insensitively."""
    return http.client.parse_headers(io.BytesIO(b"Host: x\r\n" + auth + b"\r\n"))


# ------------------------------------------------------------------ premise

def test_the_wire_carries_both_fields():
    """The premise, stated so the rest is not vacuous: a doubled field survives
    the request head as TWO fields, and `.get()` sees only the first."""
    headers = _request_head(TWO)
    assert [v for k, v in headers.items() if k == "Authorization"] == [
        "Bearer forged-first", "Bearer validated-second"]
    assert headers.get_all("Authorization") == ["Bearer forged-first",
                                                "Bearer validated-second"]
    # the blind spot this fix stops relying on
    assert headers.get("Authorization") == "Bearer forged-first"


def test_a_single_field_is_unaffected():
    """The false-positive half: one field is still exactly one value."""
    headers = _request_head(ONE)
    assert _header_values(headers, "Authorization") == ["Bearer tok-1"]
    assert _bearer_token(headers) == "tok-1"


# ------------------------------------------------------------------- binding

def test_a_doubled_authorization_binds_no_claim(tmp_path):
    serving = _serving(tmp_path)
    try:
        status, calls = _request(serving, TWO)
        assert 200 <= status < 300, status
        assert calls, "the handler was not reached"
        assert calls[-1][2][0] == {"token": None}
        assert calls[-1][2][1] == "7"
    finally:
        _stop(serving)


def test_the_same_route_binds_a_single_authorization_verbatim(tmp_path):
    """Non-vacuity: the SAME route over the SAME socket binds a single field, so
    the arm above is the duplicate and not a broken fixture."""
    serving = _serving(tmp_path)
    try:
        status, calls = _request(serving, ONE)
        assert 200 <= status < 300, status
        assert calls[-1][2][0] == {"token": "tok-1"}
    finally:
        _stop(serving)


def test_the_claim_and_the_header_list_do_not_disagree(tmp_path):
    """One request, one answer. The claim is absent (no unambiguous credential),
    and the escape hatch still shows both fields -- an absence and a list, not
    two different credentials."""
    serving = _serving(tmp_path)
    try:
        _request(serving, TWO)
        headers = _request_head(TWO)
        assert _bearer_token(headers) is None
        assert len(_header_values(headers, "Authorization")) == 2
    finally:
        _stop(serving)


def test_a_mapping_carrying_both_spellings_binds_no_claim():
    """A plain mapping cannot hold one key twice, but it can hold the same field
    under two spellings -- the duplicate defect spelled differently."""
    assert _bearer_token({"Authorization": "Bearer a",
                          "authorization": "Bearer b"}) is None
    assert _bearer_token({"authorization": "Bearer b"}) == "b"
    assert _bearer_token({"Authorization": "Bearer a"}) == "a"


def test_the_scheme_and_absence_rules_are_unchanged():
    """A single field still reads exactly as it did; the only new absences are the
    duplicated field and the scheme presented without credentials."""
    assert _bearer_token(_request_head(ONE)) == "tok-1"
    # a non-bearer scheme is still handed over as presented, untouched
    assert _bearer_token(_request_head(b"Authorization: Basic dXNlcg==\r\n")) \
        == "Basic dXNlcg=="
    # no field at all is still no credential
    assert _bearer_token(_request_head(b"")) is None
    assert _bearer_token(None) is None
    # a bare `Bearer` is the scheme with no credentials, so it is no credential
    # either -- it must not be handed on as a token spelled "Bearer"
    assert _bearer_token(_request_head(b"Authorization: Bearer\r\n")) is None
    assert _bearer_token(_request_head(b"Authorization: Bearer   \r\n")) is None
    # the scheme match stays case-insensitive
    assert _bearer_token(_request_head(b"Authorization: bEaReR tok-9\r\n")) \
        == "tok-9"


def test_the_header_reader_is_duplicate_visible():
    """`_header_values` is the reader `.get()` cannot be, and it answers for both
    the `Message` and the mapping shape."""
    headers = _request_head(TWO)
    assert _header_values(headers, "Authorization") == ["Bearer forged-first",
                                                        "Bearer validated-second"]
    assert _header_values({"Authorization": "x"}, "Authorization") == ["x"]
    assert _header_values({}, "Authorization") == []
    assert _header_values(None, "Authorization") == []


def test_a_doubled_field_is_not_a_framing_refusal(tmp_path):
    """The credential rule is the credential's, not the framing layer's. A
    doubled `Authorization` is a readable request, so the handler runs and the
    claim is absent -- it is not a `400`."""
    serving = _serving(tmp_path)
    try:
        status, calls = _request(serving, TWO)
        assert status != 400, status
        assert calls
    finally:
        _stop(serving)


def test_email_message_shape_is_the_one_used():
    """`_request_head` builds the same type the server passes in, so the arms
    above exercise the production reader rather than a stand-in."""
    headers = _request_head(TWO)
    assert isinstance(headers, email.message.Message)
