"""`revl serve --http`'s request framing — the host face of `stdlib/framing.rvl`.

Item 867 (`stdlib/framing.rvl`, `docs/design/867-request-framing.md`) retired the
doubled-`Content-Length` smuggling shape from every Revl component by making ONE
primitive decide how many body bytes a request carries. Its design note names the
one residue it does not retire
(`docs/design/867-request-framing.md:151`): the compiler's own host-side HTTP
face, `src/revl/mcp/http_face.py`, which framed a request with

    length = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(length) if length else b""

— `email.message.Message.get()` answers with the FIRST occurrence only, so a
doubled field was framed silently; the value went to `rfile.read` unbounded; and
`int()` raised straight out of the handler on anything that is not a decimal
integer.

Every test here drives the REAL handler over a REAL loopback socket with a stub
session (the harness `tests/test_serve_http.py` uses — the wire layer needs no
cordis runtime), because all of these are properties of the byte stream and of
what the socket is left holding, not of a function's return value. Each test in
the refusal block fails on the pre-fix reader and passes on the mirrored one; the
controls at the end pin that the mirror did not become stricter than the policy
it mirrors.
"""

import contextlib
import http.client
import io
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, build_http_server  # noqa: E402


def _internals():
    """The framing port's names, imported lazily on purpose.

    Against a build that predates the port these names do not exist. Resolving
    them at module scope would turn that into a collection error — which proves
    nothing — instead of the behavioural failures the wire tests below produce
    (a `200 OK` for a doubled field, an empty socket for an oversized one, a
    traceback for a non-numeric one). The black-box half of this file has to be
    able to RUN against the old reader to be evidence about it."""
    from revl.mcp import http_face
    return http_face


def _frame_request(headers, ceiling):
    return _internals()._frame_request(headers, ceiling)


CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

CELL = 1024 * 1024          # the ceiling `stdlib/framing.rvl:41` names
BODY = b'["abc"]'           # 7 bytes, so `Content-Length: 7` frames it exactly


class _StubSession:
    """Just enough of a Session for the runtime-free wire tests, exactly as
    `tests/test_serve_http.py` builds it."""

    def __init__(self, source: str = CACHE, result_value="row"):
        self.ir = compile_source(source)
        self.calls: list[tuple] = []
        self._result_value = result_value

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        return {"result": self._result_value, "trace": []}

    def state(self, drain: bool = False):
        return {"loaded": True}


@contextlib.contextmanager
def _serving():
    """The face on a real loopback socket, with a stub session behind it."""
    session = _StubSession()
    face = HttpComposedServer(session, composition="app")
    httpd = build_http_server(face, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], session
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _raw(port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    """Send `payload` verbatim and collect every byte the server sends back
    until it closes or stops answering. Raw bytes on purpose: what the server
    does with the bytes LEFT OVER after a framing decision is half the defect."""
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
    """Every HTTP reply in `blob`, in wire order: `(status, headers, body)`."""
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
    refused framing must not be followed by the parser starting on the bytes the
    sender meant as a body."""
    replies = _replies(blob)
    assert len(replies) == 1, f"expected one reply, got {replies}"
    return replies[0]


def _diagnostic(body: bytes) -> dict:
    payload = json.loads(body)
    assert payload["ok"] is False
    diag = payload["diagnostics"][0]
    # the module's error envelope, unchanged: a refusal is a RESULT, not a crash
    assert set(diag) == {"severity", "code", "category", "message"}
    assert diag["severity"] == "error"
    assert diag["code"] == "REVL"
    return diag


def _post(path: bytes, header_block: bytes, body: bytes = b"") -> bytes:
    return (b"POST " + path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + header_block + b"\r\n" + body)


# ------------------------------------------- the doubled field (the smuggling shape)

def test_the_doubled_content_length_from_the_issue_is_refused():
    """THE case, on the wire. `Content-Length: 0` then `Content-Length: 7` with a
    7-byte body: the pre-fix reader took the FIRST value, called the operation
    with NO arguments, answered 200, and left the 7 bytes in the socket to be
    parsed as the head of the next request — the smuggling shape
    `docs/design/867-request-framing.md` measured on the harness server. One
    request must earn one answer."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: 0\r\nContent-Length: 7\r\n",
                                BODY))
    status, headers, body = _one_reply(blob)
    assert status == 400
    assert headers["connection"] == "close"
    assert _diagnostic(body)["category"] == "duplicate_content_length"
    assert session.calls == []       # the operation was never reached


def test_the_leftover_bytes_are_never_parsed_as_the_next_request():
    """The other half of the smuggling shape, kept alive across requests: the
    bytes the sender declared as the body must not be re-read as a request line.
    Pre-fix this stream answers `200 OK` (the op, with no arguments) and THEN
    `400 Bad Request` (the leftover `["abc"]...` parsed as a request)."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: 0\r\nContent-Length: 7\r\n",
                                BODY)
                      + _post(b"/app/cache/size", b"Content-Length: 0\r\n"))
    replies = _replies(blob)
    assert [status for status, _, _ in replies] == [400]
    assert _diagnostic(replies[0][2])["category"] == "duplicate_content_length"
    assert session.calls == []


def test_the_reverse_order_is_the_same_refusal_not_a_framing_of_the_first_value():
    """`Content-Length: 5` then `Content-Length: 0`. Pre-fix this framed 5 of the
    7 body bytes and answered `400 request body is not JSON` — a 400, but the
    WRONG one, and with the trailing `]` left in the socket. The refusal has to
    name the doubled field."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: 5\r\nContent-Length: 0\r\n",
                                BODY))
    status, _, body = _one_reply(blob)
    assert status == 400
    assert _diagnostic(body)["category"] == "duplicate_content_length"
    assert session.calls == []


def test_an_identical_duplicate_content_length_is_refused_too():
    """The module's own documented decision (`stdlib/framing.rvl:77`): a repeated
    `Content-Length` is refused EVEN WHEN the values are byte-identical. RFC 7230
    3.3.2 / RFC 9110 8.6 permit accepting it and RFC 9112 6.3 item 5 keeps that
    exception for an all-identical list; the module takes the conservative half,
    because "the values are identical" is a judgement a second reader has to make
    anyway — on values an intermediary may have rewritten — and two readers on one
    server making it differently is the defect."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: 7\r\nContent-Length: 7\r\n",
                                BODY))
    status, _, body = _one_reply(blob)
    assert status == 400
    assert _diagnostic(body)["category"] == "duplicate_content_length"
    assert session.calls == []


def test_a_comma_list_in_one_field_is_the_duplicate_refusal_not_a_traceback():
    """`Content-Length: 5, 5` is the same defect spelled differently; the value
    grammar has no comma (RFC 9110 8.6: `Content-Length = 1*DIGIT`). Pre-fix this
    was `int("5, 5")` -> ValueError -> traceback -> no response at all."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: 5, 5\r\n", BODY))
    status, _, body = _one_reply(blob)
    assert status == 400
    assert _diagnostic(body)["category"] == "duplicate_content_length"
    assert session.calls == []


# ------------------------------------------------------------ the ceiling

def test_a_content_length_over_the_ceiling_is_a_413_before_any_read():
    """Pre-fix, a `Content-Length` of 1e12 (and the 4300-digit shape below) was
    handed straight to `rfile.read`: the request got NO response at all — an
    `OSError: [Errno 22] Invalid argument` or an `OverflowError` out of the
    read, a traceback on the server's stderr, and a dropped connection. The
    ceiling is the caller's policy (`stdlib/framing.rvl:252`), and a body over it
    is refused BEFORE any of it is read: the client here sends no body at all and
    still gets its answer."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                f"Content-Length: {CELL + 1}\r\n"
                                .encode()), timeout=1.5)
    status, headers, body = _one_reply(blob)
    assert status == 413
    assert headers["connection"] == "close"
    assert _diagnostic(body)["category"] == "over_ceiling"
    assert session.calls == []


def test_the_4300_digit_content_length_from_the_issue_gets_a_413_not_a_crash():
    """The fourth comment on issue #867: a `Content-Length` of 4300 and more
    digits passed the harness readers' validator and then defeated the integer
    conversion one statement later, so the request got no response at all. It is
    refused on its first few digits here and is never converted."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: " + b"9" * 4300 + b"\r\n"),
                    timeout=1.5)
    status, _, body = _one_reply(blob)
    assert status == 413
    assert _diagnostic(body)["category"] == "over_ceiling"
    assert session.calls == []


def test_the_refusal_sentence_never_echoes_the_offending_value():
    """The offending value can be thousands of digits long; reflecting it back is
    a defect of its own, so each refusal's sentence is static."""
    with _serving() as (port, _):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: " + b"9" * 4300 + b"\r\n"),
                    timeout=1.5)
    _, _, body = _one_reply(blob)
    assert b"9999" not in body


# ------------------------------------------- the value grammar (no tracebacks)

@pytest.mark.parametrize("value", [b"abc", b"-5", b"+5", b"0x5", b"5.0",
                                   b"five", b"0000000001", b"5 5", b""])
def test_a_value_that_is_not_one_plain_decimal_integer_is_a_clean_400(value):
    """`int()` raised `ValueError` straight out of `_respond` for every one of
    these — a traceback on stderr and no reply on the wire — and `-5` reached
    `rfile.read` and raised `ValueError: read length must be non-negative or -1`.
    Each is now a structured 400 in the module's own error envelope."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Content-Length: " + value + b"\r\n",
                                BODY), timeout=1.5)
    status, headers, body = _one_reply(blob)
    assert status == 400
    assert headers["connection"] == "close"
    assert _diagnostic(body)["category"] == "malformed_content_length"
    assert session.calls == []


def test_no_refusal_reaches_the_client_as_a_traceback(capsys):
    """The pre-fix reader's failure mode for four of these shapes was an
    unhandled exception out of `socketserver`'s handler — a traceback printed to
    the server's stderr and no response. Nothing on this path may raise."""
    shapes = [b"Content-Length: abc\r\n",
              b"Content-Length: -5\r\n",
              b"Content-Length: 5, 5\r\n",
              b"Content-Length: 0\r\nContent-Length: 7\r\n",
              b"Content-Length: " + b"9" * 4300 + b"\r\n",
              b"Transfer-Encoding: chunked\r\n"]
    with _serving() as (port, _):
        blobs = [_raw(port, _post(b"/app/cache/get", shape, BODY),
                      timeout=1.5) for shape in shapes]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Traceback" not in captured.err
    for blob, shape in zip(blobs, shapes):
        assert _replies(blob), f"no reply for {shape!r}"


# ------------------------------------------- Transfer-Encoding, rule 1

@pytest.mark.parametrize("coding", [b"chunked", b"gzip", b"identity",
                                    b"chunked, gzip"])
def test_a_transfer_encoding_is_refused_rather_than_framed_by_content_length(coding):
    """Rule 1 of `stdlib/framing.rvl` (`:59`, `:263`), and the same smuggling
    family: this face has no transfer-coding decoder, so a body framed by
    `Transfer-Encoding` has no length it can hand back. Pre-fix the field was
    ignored and the request was framed by its `Content-Length` instead, leaving
    the chunked body in the socket — the smuggled-request shape. `chunked` earns
    no exception (RFC 9112 6.1)."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get",
                                b"Transfer-Encoding: " + coding
                                + b"\r\nContent-Length: 7\r\n", BODY))
    status, headers, body = _one_reply(blob)
    assert status == 400
    assert headers["connection"] == "close"
    assert _diagnostic(body)["category"] == "unsupported_transfer_encoding"
    assert session.calls == []


# ------------------------------------------- controls: no over-refusal

def test_a_well_framed_request_still_round_trips():
    """The mirror must not refuse what the policy accepts."""
    with _serving() as (port, session):
        blob = _raw(port, _post(b"/app/cache/get", b"Content-Length: 7\r\n",
                                BODY))
    status, _, body = _one_reply(blob)
    assert status == 200
    assert json.loads(body) == {"ok": True, "value": "row"}
    assert session.calls == [("cache", "get", ["abc"])]


def test_a_request_with_no_content_length_is_still_a_bodyless_request():
    """RFC 9112 6.3 item 7 / `stdlib/framing.rvl:256`: neither field means no
    body, which is 0, not a refusal."""
    with _serving() as (port, _):
        blob = _raw(port, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    status, _, body = _one_reply(blob)
    assert status == 200
    assert "operations" in json.loads(body)


# ------------------------------------------- the mirror itself

def _headers(*lines: str):
    """The exact header object `BaseHTTPRequestHandler` hands the handler:
    `http.client.parse_headers` over the request head, so a doubled field stays
    doubled and names compare case-insensitively."""
    raw = b"".join(line.encode() + b"\r\n" for line in lines) + b"\r\n"
    return http.client.parse_headers(io.BytesIO(raw))


def _reason(refusal):
    return None if refusal is None else refusal.reason


def test_a_single_content_length_is_the_body_length():
    assert _frame_request(_headers("Content-Length: 5"), 100) == (5, None)
    assert _frame_request(_headers("Content-Length: 0"), 100) == (0, None)
    assert _frame_request(_headers("Content-Length: 100"), 100) == (100, None)


def test_no_content_length_and_no_transfer_encoding_is_no_body():
    assert _frame_request(_headers("Host: example.test"), CELL) == (0, None)


def test_field_names_compare_case_insensitively():
    assert _frame_request(_headers("content-length: 5"), 100) == (5, None)
    assert _frame_request(_headers("CONTENT-LENGTH: 5"), 100) == (5, None)
    assert _frame_request(_headers("Content-Length: 5", "content-length: 7"),
                          100) == (None, _internals()._DUPLICATE_CONTENT_LENGTH)


def test_ows_around_the_value_is_stripped():
    assert _frame_request(_headers("Content-Length: 5 "), 100) == (5, None)


@pytest.mark.parametrize("lines, expected", [
    # rule 2 — a repeated field, the issue's own shape, and the identical case
    (("Content-Length: 5", "Content-Length: 7"), "duplicate_content_length"),
    (("Content-Length: 5", "Content-Length: 5"), "duplicate_content_length"),
    (("Content-Length: 5", "Content-Length: 5", "Content-Length: 5"),
     "duplicate_content_length"),
    (("Content-Length: 5, 5",), "duplicate_content_length"),
    (("Content-Length: 5,7",), "duplicate_content_length"),
    # rule 3 — everything the value grammar excludes
    (("Content-Length: +5",), "malformed_content_length"),
    (("Content-Length: -5",), "malformed_content_length"),
    (("Content-Length:",), "malformed_content_length"),
    (("Content-Length: 0000000001",), "malformed_content_length"),
    (("Content-Length: 5 5",), "malformed_content_length"),
    (("Content-Length: 0x5",), "malformed_content_length"),
    (("Content-Length: 5.0",), "malformed_content_length"),
    (("Content-Length: five",), "malformed_content_length"),
    # rule 4 — the caller's ceiling, and the value that never gets converted
    ((f"Content-Length: {CELL + 1}",), "over_ceiling"),
    ((f"Content-Length: {'9' * 4300}",), "over_ceiling"),
    ((f"Content-Length: {'9' * 4301}",), "over_ceiling"),
    ((f"Content-Length: {'0' * 4300}",), "malformed_content_length"),
    ((f"Content-Length: {2 ** 64}",), "over_ceiling"),
    # rule 1 — Transfer-Encoding first, whatever else the head carries
    (("Transfer-Encoding: chunked",), "unsupported_transfer_encoding"),
    (("Transfer-Encoding: gzip",), "unsupported_transfer_encoding"),
    (("Transfer-Encoding: identity",), "unsupported_transfer_encoding"),
    (("Transfer-Encoding: chunked", "Content-Length: 5", "Content-Length: 7"),
     "unsupported_transfer_encoding"),
    # the order is part of the contract (`stdlib/framing.rvl:59`)
    (("Content-Length: 5", "Content-Length: nope"), "duplicate_content_length"),
    (("Content-Length: nope",), "malformed_content_length"),
])
def test_the_host_reader_mirrors_the_stdlib_case_table(lines, expected):
    """The same table `tests/test_framing_stdlib.py` pins for
    `stdlib/framing.rvl`'s `body_length`, on the host reader. The host face is
    Python and cannot call the primitive, so the whole point is that it agrees
    with it case for case — including the refusal ORDER, which the module fixes
    so two readers report the same refusal for a head that breaks two rules."""
    length, refusal = _frame_request(_headers(*lines), CELL)
    assert length is None
    assert _reason(refusal) == expected


def test_the_ceiling_is_inclusive():
    # exactly the ceiling is accepted (`stdlib/framing.rvl`'s `length_of`), one
    # byte past it is not
    assert _frame_request(_headers(f"Content-Length: {CELL}"), CELL) == (CELL, None)
    assert _frame_request(_headers(f"Content-Length: {CELL + 1}"), CELL) == \
        (None, _internals()._OVER_CEILING)


def test_the_host_refusals_are_the_stdlibs_own_statuses_tokens_and_sentences():
    """No drift, by construction: every status, machine token and sentence this
    reader ships is written in `stdlib/framing.rvl` — `status_for` (`:182`),
    `reason_of` (`:188`) and `message_of` (`:215`). Editing one without the other
    fails here."""
    face = _internals()
    source = (ROOT / "stdlib" / "framing.rvl").read_text(encoding="utf-8")
    refusals = (face._UNSUPPORTED_TRANSFER_ENCODING, face._DUPLICATE_CONTENT_LENGTH,
                face._MALFORMED_CONTENT_LENGTH, face._OVER_CEILING)
    for refusal in refusals:
        assert f'"{refusal.reason}"' in source, refusal.reason
        assert refusal.message in source, refusal.reason
    assert "DuplicateContentLength => 400," in source
    assert "MalformedContentLength => 400," in source
    assert "UnsupportedTransferEncoding => 400," in source
    assert "OverCeiling => 413," in source
    assert face._UNSUPPORTED_TRANSFER_ENCODING.status == 400
    assert face._DUPLICATE_CONTENT_LENGTH.status == 400
    assert face._MALFORMED_CONTENT_LENGTH.status == 400
    assert face._OVER_CEILING.status == 413


def test_the_ceiling_is_the_number_the_stdlib_names():
    """`stdlib/framing.rvl:41` — the module's own usage example names the
    ceiling, so the host face uses that number rather than inventing one."""
    source = (ROOT / "stdlib" / "framing.rvl").read_text(encoding="utf-8")
    assert "body_length(req.headers, 1024 * 1024)" in source
    assert _internals()._MAX_BODY == CELL
