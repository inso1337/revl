"""`revl serve --http`'s RESPONSE framing — the other half of `stdlib/framing.rvl`.

Item 867 (`stdlib/framing.rvl`, `docs/design/867-request-framing.md`) made ONE
primitive decide how many body bytes a REQUEST carries, and its residue section
named the compiler's own host-side HTTP face as the reader it does not retire.
That reader is closed (`tests/test_serve_http_framing.py`); this file closes the
same face's WRITER, which the design note does not discuss because the module
frames requests and says nothing about replies.

The face's writer was

    self.send_response(reply.status)
    self.send_header("Content-Type", reply.content_type)
    for name, value in reply.headers:
        if name and name.lower() != "content-type":
            self.send_header(name, value)
    self.send_header("Content-Length", str(len(reply.body)))
    self.end_headers()
    self.wfile.write(reply.body)

Three defects, all of them reachable from a request:

1. `http.server`'s `send_header` validates NOTHING — it is
   ``self._headers_buffer.append(("%s: %s\\r\\n" % (keyword, value))…)`` and
   nothing more (`_is_illegal_header_value` exists only in `http.client`, i.e. on
   the RECEIVING side). A handler that returns a `Response` whose header value
   came from a decoded path or query scalar therefore writes its own field
   separator: `?tag=%0d%0aContent-Length:%2011%0d%0a%0d%0aFORGED-BODY` puts a
   complete, attacker-framed body on the wire ahead of the real one.
2. The face appends its OWN `Content-Length` after the handler's, so a reply can
   carry two framings (or a `Transfer-Encoding` beside a `Content-Length` — the
   classic desync), and which one a reader believes is its choice.
3. `do_HEAD` wrote the body anyway, with the `Content-Length` that says it is
   there; on a kept-alive connection those bytes are read as the head of the
   next reply.

Every test here drives the REAL handler over a REAL loopback socket, because all
three are properties of the byte stream and not of a function's return value. The
header value in each hostile case is built FROM the bound argument — the stub
echoes what the router decoded — so the tests are about a remote-controlled
value and not about a constant this file chose. The controls at the end pin that
the new checks did not become stricter than the framing rule they enforce: an
ordinary handler header still round-trips, a handler `content-type` is still
replaced rather than refused, and a plain reply is unchanged.
"""

import contextlib
import json
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, build_http_server  # noqa: E402

# A routed operation returning `Response` — `lower.py`'s `{"kind": "response"}`
# return rule, the one path where the HANDLER owns the reply's headers
# (`_encode_response_value`: "the handler owns status, headers and body, sent
# as-is"). `tag` is a bound query scalar, so whatever reaches the header is
# whatever the request put in the query.
APP = """\
use "stdlib/http.rvl" { ApiError, Response, header }

service Echo {
  route get "/echo"
  fn echo(tag: Str) -> Response

  route head "/echo"
  fn echo_head(tag: Str) -> Response

  route get "/ping"
  fn ping() -> Response

  route get "/note/{id}"
  fn note(id: Str) -> Result[Str, ApiError]
}

component EchoHttp provides echo: Echo {
  provide echo {
    fn echo(tag) = { status: 200, status_text: "OK",
                     headers: [header("x-tag", tag)],
                     body: Text("ok") }
    fn echo_head(tag) = { status: 200, status_text: "OK",
                          headers: [header("x-tag", tag)],
                          body: Text("ok") }
    fn ping() = { status: 200, status_text: "OK", headers: [], body: Text("ok") }
    fn note(id) = Ok(id)
  }
}
"""

BODY = b"LEGITIMATE-BODY"
FORGED = b"FORGED-BODY"


class Err:
    """The `Err` half of `Result`, as the runtime-free stub hands it over: the
    status of an error reply is the handler's `ApiError.status`, so it is
    handler-chosen for the same reason a `Response.status` is."""

    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value


def _text_response(headers, status: int = 200, body: str = "LEGITIMATE-BODY"):
    """A handler-owned `Response`, in the canonical wire encoding
    (`_encode_value`'s shape for `stdlib/http.rvl`'s record + `Body` ADT)."""
    return {"status": status, "status_text": "OK", "headers": headers,
            "body": {"$kind": "Text", "$value": body}}


def _reflect(value, name: str = "x-tag"):
    """The ordinary reflecting handler — `headers: [header("x-tag", tag)]` — as
    a runtime-free stub: the result is built FROM the bound argument, so the
    header value under test is remote-controlled rather than a test constant."""
    return _text_response([{"name": name, "value": value}])


class _Handler:
    """A runtime-free session (the harness `tests/test_serve_http.py` builds):
    it carries the compiled IR so the route table exists, and builds its canned
    result from the arguments the router bound."""

    def __init__(self, ir, build=_reflect):
        self.ir = ir
        self._build = build
        self.calls: list[tuple] = []

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        return {"result": self._build(args[0]), "trace": []}

    def state(self, drain: bool = False):
        return {"loaded": True}


def _face(tmp_path, build=_reflect):
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    ir = compile_files([str(app)])
    return HttpComposedServer(_Handler(ir, build), composition="revl")


@contextlib.contextmanager
def _serving(tmp_path, build=_reflect):
    """The face on a real loopback socket, with a stub session behind it."""
    face = _face(tmp_path, build)
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
    until it closes or stops answering. Raw bytes on purpose: whether the body
    the handler asked for is on the wire is the property under test."""
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


def _replies(blob: bytes) -> list[tuple[int, dict, bytes]]:
    """Every HTTP reply in `blob`, in wire order: `(status, headers, body)`.

    A duplicated `Content-Length` collapses to the LAST one here, which is the
    point of asserting on the raw blob in the tests that care: this parser is
    one reader's choice, and the defect is that there is a choice at all."""
    out = []
    while blob:
        if not blob.startswith(b"HTTP/1."):
            raise AssertionError(f"not an HTTP reply on the wire: {blob[:200]!r}")
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
        out.append((status, headers, rest[:length]))
        blob = rest[length:]
    return out


def _one_reply(blob: bytes) -> tuple[int, dict, bytes]:
    """The single reply `blob` carries, asserting there is exactly one: a
    refused reply must not be followed by the bytes the handler meant as a
    body, which is exactly what a forged `Content-Length` produces."""
    replies = _replies(blob)
    assert len(replies) == 1, f"expected one reply, got {replies}"
    return replies[0]


def _head_fields(blob: bytes) -> bytes:
    """The raw head of the first reply, before the blank line — what the tests
    that count a field on the wire assert against, since a dict cannot show a
    field twice, and what the tests that care about a field NOT being there
    assert against, since the refusal's own message names the field it refused.
    """
    head, sep, _ = blob.partition(b"\r\n\r\n")
    assert sep, f"truncated reply head: {head[:200]!r}"
    return head


def _diagnostic(body: bytes) -> dict:
    payload = json.loads(body)
    assert payload["ok"] is False
    diag = payload["diagnostics"][0]
    # the module's error envelope, unchanged: a refusal is a RESULT, not a crash
    assert set(diag) == {"severity", "code", "category", "message"}
    assert diag["severity"] == "error"
    assert diag["code"] == "REVL"
    return diag


def _get(path: bytes, header_block: bytes = b"") -> bytes:
    return (b"GET " + path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + header_block + b"\r\n")


def _head(path: bytes) -> bytes:
    return b"HEAD " + path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"


# --------------------------------------------- a header value that ends the field

def test_a_decoded_query_scalar_cannot_write_its_own_fields(tmp_path):
    """THE case, on the wire. `?tag=%0d%0aContent-Length:%2011%0d%0a%0d%0a
    FORGED-BODY` is percent-decoded by the router into the bound `tag`, the
    handler puts it in `x-tag`, and the pre-fix face interpolated it into
    ``"%s: %s\\r\\n"`` — so the reply carried a second, attacker-framed
    `Content-Length` and an attacker body AHEAD of the real one. The handler's
    reply is refused whole instead."""
    with _serving(tmp_path) as (port, session):
        blob = _raw(port, _get(b"/echo?tag=%0d%0aContent-Length:%2011"
                               b"%0d%0a%0d%0aFORGED-BODY"))
    status, headers, body = _one_reply(blob)
    assert status == 500, "the request was well-formed; the program is what broke"
    assert headers["connection"] == "close"
    assert _diagnostic(body)["category"] == "unsafe_response_header"
    assert session.calls[-1] == ("echo", "echo", ["\r\nContent-Length: 11"
                                                  "\r\n\r\nFORGED-BODY"])


def test_the_forged_body_never_reaches_the_wire(tmp_path):
    """The impact half, asserted on the raw bytes: the refused reply is
    substituted BEFORE `send_response`, so nothing of the handler's reply —
    forged body included — is on the wire, and there is no second framing field
    for a reader to prefer."""
    with _serving(tmp_path) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=%0d%0aContent-Length:%2011"
                               b"%0d%0a%0d%0aFORGED-BODY"))
    assert FORGED not in blob
    assert BODY not in blob
    head = _head_fields(blob)
    assert head.lower().count(b"content-length") == 1
    assert b"x-tag" not in head.lower()


def test_a_bare_crlf_in_a_header_value_is_refused(tmp_path):
    """The minimal shape of the same defect: no forged field, just the newline
    that ends the one the handler was given. A value carrying CR or LF is not a
    field value at all (RFC 9110 5.5), so it is refused rather than truncated."""
    with _serving(tmp_path) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=x%0d%0aX-Injected:%20yes"))
    status, _, body = _one_reply(blob)
    assert status == 500
    assert _diagnostic(body)["category"] == "unsafe_response_header"
    assert b"x-injected" not in blob.lower()


def test_a_control_character_that_is_not_crlf_is_refused_too(tmp_path):
    """A bare NUL or DEL is no less of a defect: neither is a field value, and a
    reader that keeps them is reading a field the sender did not write."""
    with _serving(tmp_path) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a%00b"))
    status, _, body = _one_reply(blob)
    assert status == 500
    assert _diagnostic(body)["category"] == "unsafe_response_header"


def test_a_header_name_that_is_not_a_token_is_refused(tmp_path):
    """A name is what a reader splits the field on (RFC 9110 5.1), so a name
    carrying a colon or a space lets the handler's name be read as a different
    field — `x-tag` becoming `x-tag: a, x-evil` is a field the sender did not
    write."""
    with _serving(tmp_path,
                  build=lambda args: _reflect(args[0], name="x-tag: a, x-evil")
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, _, body = _one_reply(blob)
    assert status == 500
    assert _diagnostic(body)["category"] == "unsafe_response_header"
    assert b"x-evil" not in blob.lower()


# ------------------------------------------- a second framing from the handler

def test_a_handler_content_length_is_refused_not_duplicated(tmp_path):
    """Pre-fix this reply carried `Content-Length: 3` AND `Content-Length: 15`:
    two framings on one message, and which one a reader believes is its choice.
    The face owns where the body ends, so the handler's is refused — not
    silently overwritten, because a program hand-framing its own reply is a
    defect to be told about."""
    with _serving(tmp_path,
                  build=lambda args: _text_response(
                      [{"name": "content-length", "value": "3"}])
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, _, body = _one_reply(blob)
    assert status == 500
    assert _diagnostic(body)["category"] == "handler_framed_response"
    assert _head_fields(blob).lower().count(b"content-length") == 1


def test_a_handler_transfer_encoding_is_refused_not_stapled_to_the_length(tmp_path):
    """The desync proper: a `Transfer-Encoding` beside the face's own
    `Content-Length` is how one message is read as two (RFC 9112 6.1). A face
    that does not decode chunked encoding has no reply that can declare it."""
    with _serving(tmp_path,
                  build=lambda args: _text_response(
                      [{"name": "Transfer-Encoding", "value": "chunked"}])
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, _, body = _one_reply(blob)
    assert status == 500
    assert _diagnostic(body)["category"] == "handler_framed_response"
    assert b"transfer-encoding" not in _head_fields(blob).lower()


def test_a_handler_connection_header_is_refused(tmp_path):
    """Connection persistence is the face's decision too: a handler that
    declares `keep-alive` on a reply the face is about to close (or the reverse)
    is describing a connection state the face owns."""
    with _serving(tmp_path,
                  build=lambda args: _text_response(
                      [{"name": "Connection", "value": "keep-alive"}])
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, headers, body = _one_reply(blob)
    assert status == 500
    assert _diagnostic(body)["category"] == "handler_framed_response"
    assert headers["connection"] == "close"


def test_a_doubled_framing_field_from_the_handler_is_the_same_refusal(tmp_path):
    """Two of the handler's own, either order: the refusal is about the field
    being the face's, so it does not depend on which one came first."""
    with _serving(tmp_path,
                  build=lambda args: _text_response(
                      [{"name": "Content-Length", "value": "0"},
                       {"name": "content-length", "value": "15"}])
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, _, body = _one_reply(blob)
    assert status == 500
    assert _diagnostic(body)["category"] == "handler_framed_response"
    assert _head_fields(blob).lower().count(b"content-length") == 1


# ------------------------------------------------------------- HEAD and no-content

def test_head_carries_the_length_the_get_would_have_and_no_body(tmp_path):
    """RFC 9110 9.3.2: a HEAD response carries the header fields the GET would
    have carried — the `Content-Length` included — and no body. Pre-fix the body
    was written anyway, so the client got bytes its own framing said were not
    there.

    Asserted on the raw bytes and not through `_replies`, because a HEAD reply is
    exactly the reply a `Content-Length`-driven parser cannot read: the field is
    present and the body is not, by design."""
    with _serving(tmp_path) as (port, _):
        blob = _raw(port, _head(b"/echo?tag=a"))
    head, sep, rest = blob.partition(b"\r\n\r\n")
    assert sep, f"truncated reply head: {blob[:200]!r}"
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert b"content-length: " + str(len(BODY)).encode() in head.lower()
    assert rest == b"", f"a HEAD reply wrote a body: {rest[:200]!r}"
    assert BODY not in blob


def test_head_then_get_on_one_connection_reads_two_replies(tmp_path):
    """Why the HEAD body is a security defect and not a cosmetic one: on a
    connection that is kept alive, the bytes the face wrote after the HEAD head
    are read by the client as the beginning of the NEXT reply. Pre-fix the second
    reply was preceded by `LEGITIMATE-BODY`, so a client that trusts the framing
    reads those bytes as a reply head — here the second reply would not even
    start where the first one ended."""
    with _serving(tmp_path) as (port, _):
        blob = _raw(port, _head(b"/echo?tag=a") + _get(b"/echo?tag=b"))
    head, sep, rest = blob.partition(b"\r\n\r\n")
    assert sep
    assert head.startswith(b"HTTP/1.1 200 OK")
    # the next reply begins at the very next byte, with nothing in between
    assert rest.startswith(b"HTTP/1.1 "), f"not a reply boundary: {rest[:80]!r}"
    replies = _replies(rest)
    assert [status for status, _, _ in replies] == [200]
    assert replies[0][2] == BODY


def test_a_contentless_status_declares_no_length(tmp_path):
    """RFC 9110 8.6: `Content-Length` MUST NOT be sent on a 204. The face's own
    no-content reply is the common case, and a handler-supplied 204 is the same
    rule — a `Content-Length: 0` there is a framing field for content the status
    says does not exist."""
    with _serving(tmp_path,
                  build=lambda args: _text_response([], status=204, body="")
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, headers, body = _one_reply(blob)
    assert status == 204
    assert "content-length" not in headers
    assert body == b""


def test_a_contentless_status_writes_no_body_either(tmp_path):
    """The half the length alone does not cover, and the half a handler can
    still get wrong with a length of its own: RFC 9110 15.2 (1xx), 15.3.5 (204)
    and 15.4.5 (304) all end the reply at the end of the header section, so a
    body written after one is read by the client as the head of the NEXT reply.

    Measured pre-fix on `origin/main`, with a 15-byte handler body:

    * `204` put `LEGITIMATE-BODY` between the two replies with nothing
      delimiting it — `http.client` sets `length = 0` for a 204, so those bytes
      are exactly what it reads next;
    * `100` made `http.client` parse the handler's own body text as a status
      line: `BadStatusLine: FORGED STATUS LINE`.

    A 304 is the third member of the set and the one a `Content-Length` rule
    alone would have missed: it is permitted to carry a length, so withholding
    the length does not withhold the body — the same set has to decide both."""
    for status in (100, 204, 304):
        with _serving(tmp_path,
                      build=lambda args, s=status: _text_response([], status=s)
                      ) as (port, _):
            blob = _raw(port, _get(b"/echo?tag=a") + _get(b"/echo?tag=b"))
        head, sep, rest = blob.partition(b"\r\n\r\n")
        assert sep, f"{status}: truncated reply head: {head[:200]!r}"
        assert head.startswith(b"HTTP/1.1 %d" % status), \
            f"{status}: {head[:60]!r}"
        assert b"content-length" not in head.lower(), \
            f"{status}: a length for content the status says does not exist"
        # the second reply begins at the very next byte, with nothing between
        assert rest.startswith(b"HTTP/1.1 "), \
            f"{status}: not a reply boundary: {rest[:80]!r}"


# --------------------------------------------- a status line the reader can read

def test_a_status_that_is_not_three_digits_is_refused(tmp_path):
    """`_write` passed the handler's `status` straight to `send_response`, which
    writes it into the status line unchecked — so the wire carried the handler's
    own digits. Measured pre-fix on `origin/main`:

        status 1000  ->  HTTP/1.1 1000 ...  ->  BadStatusLine: HTTP/1.1 1000
        status   99  ->  HTTP/1.1 99   ...  ->  BadStatusLine: HTTP/1.1 99
        status    0  ->  HTTP/1.1 0    ...  ->  BadStatusLine: HTTP/1.1 0

    RFC 9110 15 makes a status code three digits, and `http.client` is required
    to reject one it cannot read that way, so each of those replies is
    unreadable by a conforming client. It is the contentless-body defect one
    line earlier in the reply: a head this face wrote that its own framing makes
    unparseable."""
    for status in (0, 99, 1000, 12345):
        with _serving(tmp_path,
                      build=lambda args, s=status: _text_response([], status=s)
                      ) as (port, _):
            blob = _raw(port, _get(b"/echo?tag=a"))
        got, _, body = _one_reply(blob)
        assert got == 500, f"{status}: written as {got}"
        assert _diagnostic(body)["category"] == "invalid_status"
        assert _head_fields(blob).startswith(b"HTTP/1.1 500"), \
            f"{status}: the refusal is not itself a readable status line"
        assert BODY not in blob, f"{status}: the handler's body reached the wire"


def test_a_status_outside_the_registry_is_still_the_handler_s_own(tmp_path):
    """The control for the check above. RFC 9110 15 defines 1xx-5xx and leaves
    6xx-9xx undefined, but every one of them is three digits a reader can read.
    The face checks the SYNTAX of the status line and not the registry, so a
    handler that deliberately answers 599 still answers 599 — the check is not
    stricter than the rule it enforces."""
    with _serving(tmp_path,
                  build=lambda args: _text_response([], status=599)
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, _, body = _one_reply(blob)
    assert status == 599
    assert body == BODY


def test_a_non_numeric_status_is_a_malformed_response(tmp_path):
    """The guard the surrounding `isinstance(enc, dict)` check already had in
    spirit: a `Response` whose `status` is not a number is malformed, and the
    encoder says so rather than letting `ValueError` out of the handler."""
    with _serving(tmp_path,
                  build=lambda args: {"status": "not-a-number",
                                      "status_text": "OK", "headers": [],
                                      "body": {"$kind": "Text",
                                               "$value": "LEGITIMATE-BODY"}}
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, _, body = _one_reply(blob)
    assert status == 500
    assert json.loads(body)["code"] == "internal_error"


def test_an_out_of_range_api_error_status_is_refused_too(tmp_path):
    """The other status the handler chooses. An `Err` reply's status is the
    handler's `ApiError.status` (`_encode_api_error`), so it reaches the same
    status line and the same rule."""
    with _serving(tmp_path,
                  build=lambda args: Err({"status": 1000, "code": "nope",
                                          "message": "nope"})
                  ) as (port, _):
        blob = _raw(port, _get(b"/note/7"))
    got, _, body = _one_reply(blob)
    assert got == 500
    assert _diagnostic(body)["category"] == "invalid_status"


def test_a_non_numeric_api_error_status_is_a_malformed_response(tmp_path):
    """And the same unguarded `int()` on the `Err` path: a status that is not a
    number is malformed, not a `ValueError` escaping the handler."""
    with _serving(tmp_path,
                  build=lambda args: Err({"status": "not-a-number",
                                          "code": "nope", "message": "nope"})
                  ) as (port, _):
        blob = _raw(port, _get(b"/note/7"))
    got, _, body = _one_reply(blob)
    assert got == 500
    assert json.loads(body)["code"] == "internal_error"


# ------------------------------------------------------------------- controls

def test_an_ordinary_handler_header_still_round_trips(tmp_path):
    """Non-vacuity: a benign handler-supplied header is not the defect, and the
    validation must not have made every routed reply a 500."""
    with _serving(tmp_path) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=hello"))
    status, headers, body = _one_reply(blob)
    assert status == 200
    assert headers["x-tag"] == "hello"
    assert body == BODY


def test_the_full_field_value_grammar_still_passes(tmp_path):
    """The validation is the field-value grammar, not a conservative subset of
    it: HTAB, SP, VCHAR and obs-text all stay legal, and a value that is legal
    is written byte-for-byte."""
    value = "a\tb c~!%+*&$#@'`|^_-.0123456789AZaz"
    query = urllib.parse.quote(value, safe="").encode("ascii")
    with _serving(tmp_path) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=" + query))
    status, headers, _ = _one_reply(blob)
    assert status == 200
    assert headers["x-tag"] == value


def test_a_handler_content_type_is_replaced_rather_than_refused(tmp_path):
    """`content-type` is the one handler header the face has always dropped, and
    it keeps dropping it: the face always has a content type of its own, so this
    is not a framing refusal and must not become a 500."""
    with _serving(tmp_path,
                  build=lambda args: _text_response(
                      [{"name": "content-type", "value": "text/x-hand-rolled"}])
                  ) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, headers, body = _one_reply(blob)
    assert status == 200
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert body == BODY


def test_a_plain_reply_is_framed_exactly_once(tmp_path):
    """The base case, so the new checks are not passing by refusing everything:
    one reply, one `Content-Length`, the body the handler asked for."""
    with _serving(tmp_path, build=lambda args: _text_response([])) as (port, _):
        blob = _raw(port, _get(b"/echo?tag=a"))
    status, headers, body = _one_reply(blob)
    assert status == 200
    assert headers["content-length"] == str(len(BODY))
    assert body == BODY
    assert _head_fields(blob).lower().count(b"content-length") == 1
