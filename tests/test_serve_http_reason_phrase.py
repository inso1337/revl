"""`revl serve --http`'s reason phrase, and `stdlib/http.rvl`'s `status_text`.

`stdlib/http.rvl` declares

    // `status_text` is the human-readable reason phrase carried alongside the
    // numeric `status`, exactly as Cordis does -- revl no longer drops it.
    pub type Response = { status: Int, status_text: Str, headers: List[Header],
                          body: Body }

and `docs/design/456-http-contracts.md` repeats the claim; the constructors
`response`, `ok_text`, `ok_json` and `not_found` all populate the field, and
`status_text_of` reads it back in-language. So the type is consistent everywhere
it is used as a VALUE.

It was not carried to the WIRE. The face's own reply type had no `status_text`
member, `_encode_response_value` read `status` and never the phrase, and the
status line was therefore built by `BaseHTTPRequestHandler.send_response` from
`self.responses` — the handler's table, not the handler's text. Measured over a
real loopback socket before the change:

| handler's `status` | handler's `status_text` | status line written |
|---|---|---|
| `402` | `"Whatever"` | `HTTP/1.1 402 Payment Required` |
| `200` | `"Whatever"` | `HTTP/1.1 200 OK` |
| `599` | `"Whatever"` | `HTTP/1.1 599 ` (empty phrase) |

The handler's phrase was unreachable in all three directions: for a code the
table knows the client read the table's words, and for one it does not the
client read nothing.

The phrase is now carried, which also makes the exported TypeScript client's
`statusText` correct rather than locally invented — it reads `__res.statusText`
from its own HTTP stack, and that stack now reports the server's phrase instead
of one it chose (verified: Node's `fetch` surfaces the wire phrase as
`statusText`, and `node:http` as `statusMessage`).

Carrying it makes the phrase untrusted input written into the status line, so it
is validated the way a field value is: a phrase is HTAB, SP, VCHAR and obs-text
and nothing else, and a CR or LF in it ends the status line early — the same
defect `tests/test_serve_http_response_framing.py` pins for header values, one
line higher up. Every test here drives the REAL handler over a REAL loopback
socket, and the phrase in each case is built FROM the bound query scalar, so
what is under test is a remote-controlled value rather than a constant this file
chose.
"""

import contextlib
import http.client
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, build_http_server  # noqa: E402

# `lower.py`'s `{"kind": "response"}` return rule — the one path where the
# HANDLER owns the reply, `_encode_response_value`'s "sent as-is". `word` is a
# bound query scalar, so the phrase under test is whatever the request put in the
# query. `/note/{id}` returns a `Result`, i.e. the `HttpReply.json` path that
# expresses no phrase at all — the fallback control.
APP = """\
use "stdlib/http.rvl" { ApiError, Response, header }

service Echo {
  route get "/phrase"
  fn phrase(word: Str) -> Response

  route get "/note/{id}"
  fn note(id: Str) -> Result[Str, ApiError]

  route get "/plain"
  fn plain() -> Response
}

component EchoHttp provides echo: Echo {
  provide echo {
    fn phrase(word) = { status: 200, status_text: word,
                        headers: [], body: Text("ok") }
    fn note(id) = Ok(id)
    fn plain() = { status: 402, status_text: "", headers: [], body: Text("ok") }
  }
}
"""

CANARY = "Whatever-Phrase-9999"
FORGED = b"FORGED-BODY"


def _phrase_reply(status: int, phrase: str):
    """A handler-owned `Response` in the canonical wire encoding
    (`_encode_value`'s shape for `stdlib/http.rvl`'s record + `Body` ADT)."""
    return {"status": status, "status_text": phrase, "headers": [],
            "body": {"$kind": "Text", "$value": "ok"}}


class _Handler:
    """A runtime-free session: it carries the compiled IR so the route table
    exists, and builds its canned reply from the argument the router bound."""

    def __init__(self, ir, status: int = 200):
        self.ir = ir
        self._status = status
        self.calls: list[tuple] = []

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        if key == "echo" and method == "note":
            return {"result": {"$kind": "Ok", "$value": args[0]}, "trace": []}
        if key == "echo" and method == "plain":
            # a handler that declines to own the phrase, as the emitter renders
            # `status_text: ""` — the fallback control
            return {"result": _phrase_reply(402, ""), "trace": []}
        return {"result": _phrase_reply(self._status, args[0]), "trace": []}

    def state(self, drain: bool = False):
        return {"loaded": True}


@contextlib.contextmanager
def _serving(tmp_path, status: int = 200):
    """The face on a real loopback socket, with a stub session behind it."""
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    face = HttpComposedServer(_Handler(compile_files([str(app)]), status),
                              composition="revl")
    httpd = build_http_server(face, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _raw(port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    """Send `payload` verbatim and collect every byte the server sends back.
    Raw bytes on purpose: what the status line says is the property under test,
    and `http.client` would only report its own parse of it."""
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


def _get(path: bytes) -> bytes:
    return (b"GET " + path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Connection: close\r\n\r\n")


def _status_line(blob: bytes) -> bytes:
    """The first reply's status line, verbatim, minus its CRLF."""
    line = blob.split(b"\r\n", 1)[0]
    assert line.startswith(b"HTTP/1."), f"not a status line: {line!r}"
    return line


def _q(value: str) -> bytes:
    return urllib.parse.quote(value, safe="").encode("ascii")


def _reason(port: int, path: str) -> tuple[int, str]:
    """Read the reply with `http.client` and report what a CLIENT sees: the
    status code and the reason phrase its own parser recovered. This is the
    other half of the finding — the phrase is only carried if a reader gets it
    back, and the pre-fix reader got `Payment Required` for every 402."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", path)
        reply = conn.getresponse()
        reply.read()
        return reply.status, reply.reason
    finally:
        conn.close()


# ------------------------------------------- the handler's phrase reaches the wire

def test_a_handlers_phrase_reaches_the_status_line(tmp_path):
    """THE case. `402` is in the face's own table as `Payment Required`, so the
    pre-fix status line was the table's words and the handler's text was
    unreachable — the phrase was dropped at the face and re-invented at the
    reader."""
    with _serving(tmp_path, status=402) as port:
        blob = _raw(port, _get(b"/phrase?word=" + _q(CANARY)))
        assert _status_line(blob) == f"HTTP/1.1 402 {CANARY}".encode(), \
            _status_line(blob)
        assert _reason(port, f"/phrase?word={urllib.parse.quote(CANARY)}") \
            == (402, CANARY)


def test_a_code_outside_the_faces_table_gets_the_handlers_phrase(tmp_path):
    """`599` is not in `BaseHTTPRequestHandler.responses`, so `send_response`
    fell back to the empty string and the pre-fix status line ended in a bare
    space — a reader recovered `reason=''` for a field its declared type says is
    a non-nullable `string`."""
    with _serving(tmp_path, status=599) as port:
        blob = _raw(port, _get(b"/phrase?word=" + _q(CANARY)))
        assert _status_line(blob) == f"HTTP/1.1 599 {CANARY}".encode(), \
            _status_line(blob)
        assert _reason(port, f"/phrase?word={urllib.parse.quote(CANARY)}") \
            == (599, CANARY)


def test_a_code_the_table_knows_still_carries_the_handlers_phrase(tmp_path):
    """The `200` row: the table would have said `OK`, and the handler says
    something else. The handler's text wins, which is what makes the field
    handler-owned rather than advisory."""
    with _serving(tmp_path, status=200) as port:
        blob = _raw(port, _get(b"/phrase?word=" + _q(CANARY)))
        assert _status_line(blob) == f"HTTP/1.1 200 {CANARY}".encode(), \
            _status_line(blob)


def test_a_phrase_with_htab_is_carried_not_refused(tmp_path):
    """The validator is the reason-phrase grammar and not something narrower: a
    phrase is `1*( HTAB / SP / VCHAR / obs-text )` (RFC 9110 15), so a tab in
    one is legal and must not be refused. This is the control that keeps the
    check below from becoming stricter than the rule it enforces."""
    with _serving(tmp_path, status=200) as port:
        blob = _raw(port, _get(b"/phrase?word=" + _q("two\twords")))
        assert _status_line(blob) == b"HTTP/1.1 200 two\twords", \
            _status_line(blob)


# ------------------------------------------------------------- the fallback control

def test_a_reply_with_no_phrase_keeps_the_faces_own_table(tmp_path):
    """A canonical reply (`HttpReply.json`, the `Result` path) expresses no
    phrase, and an empty phrase means exactly that — so the status line falls
    back to the face's table and the head of every non-`Response` reply is what
    it always was. Passing `""` through to `send_response_only` instead would
    have written `HTTP/1.1 200 ` and changed every such reply."""
    with _serving(tmp_path, status=200) as port:
        blob = _raw(port, _get(b"/note/7"))
        assert _status_line(blob) == b"HTTP/1.1 200 OK", _status_line(blob)
        assert _reason(port, "/note/7") == (200, "OK")


def test_an_empty_phrase_from_a_handler_also_falls_back(tmp_path):
    """The same rule one level in: a handler that returns a `Response` with an
    empty `status_text` gets the table's phrase, not an empty one. The phrase is
    the one part of the head a handler may decline to own."""
    with _serving(tmp_path, status=402) as port:
        blob = _raw(port, _get(b"/plain"))
        assert _status_line(blob) == b"HTTP/1.1 402 Payment Required", \
            _status_line(blob)
        assert _reason(port, "/plain") == (402, "Payment Required")


# --------------------------------------------- a phrase that ends the status line

def test_a_phrase_that_ends_the_status_line_is_refused(tmp_path):
    """The security half, and the reason the phrase is validated at all. The
    phrase is interpolated into the status line, so a CR or LF in it ends that
    line early and everything after it is read as further header fields — or,
    after a blank line, as the message body. The phrase here comes from the
    decoded query scalar, so it is remote-controlled, and the reply is refused
    whole rather than written with a forged body ahead of the real one."""
    hostile = "\r\nContent-Length: 11\r\n\r\nFORGED-BODY"
    with _serving(tmp_path, status=402) as port:
        blob = _raw(port, _get(b"/phrase?word=" + _q(hostile)))
        assert FORGED not in blob, blob[:400]
        assert blob.count(b"HTTP/1.") == 1, blob[:400]
        assert b"unsafe_status_text" in blob, blob[:400]
        # the refusal is this face's own reply, so its phrase is the table's
        status, reason = _reason(port, "/phrase?word="
                                 + urllib.parse.quote(hostile))
        assert (status, reason) == (500, "Internal Server Error"), (status, reason)


def test_a_bare_lf_in_a_phrase_is_refused_too(tmp_path):
    """A lone LF, not just CRLF: `send_response_only` writes the phrase into
    ``"%s %d %s\\r\\n"``, so a bare LF is already a line ending by the time a
    reader splits on one, and a lenient reader that splits on LF alone is the
    common case rather than the exception."""
    with _serving(tmp_path, status=200) as port:
        blob = _raw(port, _get(b"/phrase?word=" + _q("ok\nX-Injected: 1")))
        assert b"X-Injected" not in blob, blob[:400]
        assert b"unsafe_status_text" in blob, blob[:400]
