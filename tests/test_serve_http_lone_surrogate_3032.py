"""`revl serve --http` answers a reply whose body carries a lone surrogate.

Issue #3032. `_encode_response_value` wrote a handler-owned `Response` body with
`str(payload).encode("utf-8")`. A `Str` is a sequence of Unicode scalar values
(`docs/strings.md`), but a value that reached the handler from an upstream it
does not control can carry an unpaired UTF-16 code unit -- the raw `"\ud83d"` a
JSON decoder produces for an escape with no partner -- and that has no UTF-8
spelling. The encode raised `UnicodeEncodeError` inside `_respond`, nothing
caught it, and the connection closed with no status line at all. A program that
had already committed its side effects (recorded a usage row, settled a
reservation) lost the answer, and the client could not tell the dropped
connection from a network failure.

The face now encodes its two body kinds totally:

  * a `Json` payload is JSON text the handler already serialized, and JSON has a
    spelling for an unpaired code unit -- the `\\ud83d` escape that produced it
    -- so the raw unit is written as that escape and the body stays text a peer
    can read;
  * a `Text` payload has no escape of its own, so the unit becomes U+FFFD, the
    replacement character a UTF-8 decoder reports for bytes that name no
    scalar, and the two lossy directions agree.

Both halves are pinned here, plus the controls that this is not a blanket
re-encoding: valid ASCII and astral text are written byte-for-byte unchanged.

Every test drives the REAL handler over a REAL loopback socket, because the
defect is the absence of a status line on the wire, not the shape of a value
returned by `dispatch_http`. The lone surrogate is built FROM the request body
(the route binds a record whose `Str` field comes from the JSON body), so what
is under test reaches the handler from a client, not from a constant this file
chose.
"""

import contextlib
import http.client
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, build_http_server  # noqa: E402

# A routed operation returning `Response` -- `lower.py`'s `{"kind": "response"}`
# return rule, the one path where the handler owns the body. The body parameter
# is a record (a `POST` body binds one record), and its `value` field is the
# client-supplied string the handler writes into the reply.
APP = """\
use "stdlib/http.rvl" { ApiError, Response }

type Payload = { value: Str }

service Echo {
  route post "/json"
  fn as_json(payload: Payload) -> Response

  route post "/text"
  fn as_text(payload: Payload) -> Response
}

component EchoHttp provides echo: Echo {
  provide echo {
    fn as_json(payload) = { status: 200, status_text: "OK", headers: [],
                            body: Json("{\\"v\\": \\"" + payload.value + "\\"}") }
    fn as_text(payload) = { status: 200, status_text: "OK", headers: [],
                            body: Text(payload.value) }
  }
}
"""


def _body_with(fragment: str) -> bytes:
    return b'{"value": "' + fragment.encode("utf-8") + b'"}'


# The JSON escapes an upstream sends: each has no partner, so the router's own
# JSON decoder turns it into a raw lone surrogate, which is the value the
# handler puts in the reply body. Per case, `value` is that decoded string,
# `wire` its spelling in the request, `text` what a text body must carry,
# and `json` the fragment the Json document must carry. A lone HIGH unit, a lone
# LOW unit, one inside surrounding text and one beside an astral scalar are all
# pinned: a repair that recognised only one half of the range, or escaped every
# non-ASCII character, would pass a single-case test.
LONE_CASES = [
    ("\ud83d", "\\ud83d", "\ufffd", "\\ud83d"),
    ("\ude00", "\\ude00", "\ufffd", "\\ude00"),
    ("a\ud83db", "a\\ud83db", "a\ufffdb", "a\\ud83db"),
    ("a\ud83d\U0001F600b", "a\\ud83d\\ud83d\\ude00b",
     "a\ufffd\U0001F600b", "a\\ud83d\U0001F600b"),
]
JSON_CASES = [(value, wire, json_fragment)
              for value, wire, _, json_fragment in LONE_CASES]
TEXT_CASES = [(value, wire, text)
              for value, wire, text, _ in LONE_CASES]
LONE_BODY = _body_with("\\ud83d")
# A valid pair decodes to ONE astral scalar, so it must reach the wire unchanged
# -- the control that the repair is not an escape of every non-ASCII character.
VALID = "\u00e9 \U0001F600"
VALID_BODY = b'{"value": "\\u00e9 \\ud83d\\ude00"}'
PLAIN_BODY = b'{"value": "a*b"}'

STATUS_PREFIX = b"HTTP/1.1 "


def _json_response(value: str):
    """A handler-owned `Response` with a `Json` body, in the canonical wire
    encoding (`_encode_value`'s shape for the record + `Body` ADT). The body is
    the JSON document the handler serialized, `{"v": "<value>"}`, so the client
    can read it back; the raw value is interpolated, which is how a handler
    assembling a document without escaping its input hands a lone surrogate to
    the face."""
    return {"status": 200, "status_text": "OK", "headers": [],
            "body": {"$kind": "Json", "$value": '{"v": "' + value + '"}'}}


def _text_response(value: str):
    return {"status": 200, "status_text": "OK", "headers": [],
            "body": {"$kind": "Text", "$value": value}}


class _Handler:
    """A runtime-free session: it carries the compiled IR so the route table and
    the body schema exist, and builds its canned reply from the argument the
    router bound and decoded."""

    def __init__(self, ir):
        self.ir = ir
        self.calls: list[tuple] = []

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        value = args[0]["value"]
        build = _json_response if method == "as_json" else _text_response
        return {"result": build(value), "trace": []}

    def state(self, drain: bool = False):
        return {"loaded": True}


def _face(tmp_path):
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    return HttpComposedServer(_Handler(compile_files([str(app)])),
                              composition="revl")


@contextlib.contextmanager
def _serving(tmp_path):
    """The face on a real loopback socket, with a stub session behind it."""
    face = _face(tmp_path)
    httpd = build_http_server(face, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], face.session
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _raw(port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    """Send `payload` verbatim and collect every byte the server sends back
    until it closes or stops answering."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    sock.sendall(payload)
    chunks = []
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except socket.timeout:
        pass
    finally:
        sock.close()
    return b"".join(chunks)


def _post(path: bytes, body: bytes) -> bytes:
    return (b"POST " + path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Connection: close\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


def _one_reply(blob: bytes) -> tuple[int, dict, bytes]:
    """The single reply `blob` carries, asserting there IS one. A connection
    closed with no answer is the defect under test, and it would otherwise pass
    an assertion written against an empty parse."""
    assert blob, "the connection closed with no reply at all"
    assert blob.startswith(STATUS_PREFIX), f"no status line: {blob[:80]!r}"
    head, sep, rest = blob.partition(b"\r\n\r\n")
    assert sep, f"truncated reply head: {head[:200]!r}"
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode("latin1")] = \
            value.strip().decode("latin1")
    length = int(headers.get("content-length", "0"))
    assert rest[length:] == b"", f"bytes after the reply body: {rest[length:]!r}"
    return status, headers, rest[:length]


# ------------------------------------------------- the defect on the wire

@pytest.mark.parametrize("value, wire, json_fragment", JSON_CASES)
def test_a_json_body_with_a_lone_surrogate_still_answers(tmp_path, value,
                                                         wire,
                                                         json_fragment):
    """THE case. Pre-fix the encode raised out of `_respond` and the client got
    zero bytes: no status line, no body, just a closed connection. Now the unit
    is written as the JSON escape that spells it, so the document is still one a
    client can parse and read back."""
    with _serving(tmp_path) as (port, session):
        status, headers, body = _one_reply(
            _raw(port, _post(b"/json", _body_with(wire))))
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert body == ('{"v": "' + json_fragment + '"}').encode("utf-8"), body
    assert json.loads(body) == {"v": value}
    assert session.calls[-1][2][0] == {"value": value}


@pytest.mark.parametrize("value, wire, text", TEXT_CASES)
def test_a_text_body_with_a_lone_surrogate_still_answers(tmp_path, value,
                                                         wire, text):
    """A text body has no escape to carry the unit, so it becomes U+FFFD. The
    property under test is the same: the client gets a status line and a body
    that is valid UTF-8."""
    with _serving(tmp_path) as (port, session):
        status, headers, body = _one_reply(
            _raw(port, _post(b"/text", _body_with(wire))))
    assert status == 200
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert body == text.encode("utf-8"), f"expected {text!r}, got {body!r}"
    assert body.decode("utf-8") == text
    assert session.calls[-1][2][0] == {"value": value}


def test_the_answer_is_a_client_readable_status_line(tmp_path):
    """Read it the way a client does, not only as raw bytes: the status line
    was absent, and `http.client` would have raised on the closed connection."""
    with _serving(tmp_path) as (port, _):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("POST", "/json", body=LONE_BODY,
                         headers={"Content-Type": "application/json"})
            reply = conn.getresponse()
            payload = reply.read()
        finally:
            conn.close()
    assert reply.status == 200
    assert payload == b'{"v": "\\ud83d"}'
    assert json.loads(payload) == {"v": "\ud83d"}


# --------------------------------------------- the controls, not a blanket rule

def test_a_valid_unicode_json_payload_is_written_unchanged(tmp_path):
    """A valid UTF-16 pair decodes to an astral scalar and an ordinary non-ASCII
    scalar is UTF-8 already: neither is escaped, so the repair did not change
    every `Json` body the face writes."""
    with _serving(tmp_path) as (port, _):
        status, _, body = _one_reply(
            _raw(port, _post(b"/json", VALID_BODY)))
    assert status == 200
    assert body == ('{"v": "' + VALID + '"}').encode("utf-8")
    assert json.loads(body) == {"v": VALID}


def test_a_valid_unicode_text_payload_is_written_unchanged(tmp_path):
    with _serving(tmp_path) as (port, _):
        status, _, body = _one_reply(
            _raw(port, _post(b"/text", VALID_BODY)))
    assert status == 200
    assert body == VALID.encode("utf-8")


def test_an_ordinary_ascii_body_is_byte_identical(tmp_path):
    """The byte-identical control for the common case: no escape, no
    replacement, the same body as before the change."""
    with _serving(tmp_path) as (port, _):
        json_status, _, json_body = _one_reply(
            _raw(port, _post(b"/json", PLAIN_BODY)))
        text_status, _, text_body = _one_reply(
            _raw(port, _post(b"/text", PLAIN_BODY)))
    assert (json_status, json_body) == (200, b'{"v": "a*b"}')
    assert json.loads(json_body) == {"v": "a*b"}
    assert (text_status, text_body) == (200, b"a*b")
