"""The LSP wire protocol: `Content-Length` framed JSON-RPC 2.0 over a byte
stream.

Hand-rolled, with no third-party dependency — the same stance the MCP server
takes. The framing is the only part of the language server that touches raw
bytes, so it is isolated here and driven over `BytesIO` in tests; the server
above it deals only in decoded messages.

`read_message` separates the two ways a read can fail to produce a message
(issue #314). EOF returns None and the loop stops. A frame that arrived but
could not be decoded raises `MalformedFrame`: the peer is still connected, so
it is owed a `-32700` and the loop must keep reading. Collapsing the two — as
this module used to — turned one bad byte into a silent exit 0, which an editor
sees as the language server dying.
"""

from __future__ import annotations

import json

_HEADER_SEP = b"\r\n"
_CONTENT_LENGTH = b"Content-Length:"


class MalformedFrame(Exception):
    """A frame arrived but could not be decoded into a message.

    Raised instead of returning None so the caller can tell this apart from
    EOF: the peer is still there and is owed a `-32700` parse error, and the
    read loop has to continue rather than exit.
    """


def read_message(stream) -> dict | None:
    """Read one framed JSON-RPC message from a binary stream.

    Returns the decoded message, or None at EOF — including a frame truncated
    by EOF, where there is no longer a peer to answer.

    Raises `MalformedFrame` when a complete-looking frame cannot be decoded: a
    non-integer or negative `Content-Length`, headers with no `Content-Length`
    at all, or a body that is not valid UTF-8 JSON. Only the last of those
    leaves the stream on a frame boundary; the others resynchronize by reading
    the remaining bytes as headers, which terminates because every iteration
    consumes at least one line and EOF ends the loop.
    """
    length: int | None = None
    while True:
        line = stream.readline()
        if not line:
            return None  # EOF, mid-headers or between frames
        if line in (_HEADER_SEP, b"\n"):
            break  # end of headers
        if line.lower().startswith(_CONTENT_LENGTH.lower()):
            raw = line.split(b":", 1)[1].strip()
            try:
                length = int(raw)
            except ValueError:
                raise MalformedFrame(
                    f"Content-Length is not an integer: {raw!r}") from None
    if length is None:
        raise MalformedFrame("frame carries no Content-Length header")
    if length < 0:
        # `stream.read(-1)` would swallow the rest of the stream and then try
        # to parse it as one body.
        raise MalformedFrame(f"Content-Length is negative: {length}")
    body = stream.read(length)
    if body is None or len(body) < length:
        return None  # truncated at EOF
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedFrame(f"body is not valid UTF-8 JSON: {exc}") from None


def write_message(stream, message: dict) -> None:
    """Serialize and frame one message onto a binary stream."""
    body = json.dumps(message).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def response(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}
