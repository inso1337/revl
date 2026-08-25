"""The LSP wire protocol: `Content-Length` framed JSON-RPC 2.0 over a byte
stream.

Hand-rolled, with no third-party dependency — the same stance the MCP server
takes. The framing is the only part of the language server that touches raw
bytes, so it is isolated here and driven over `BytesIO` in tests; the server
above it deals only in decoded messages.
"""

from __future__ import annotations

import json

_HEADER_SEP = b"\r\n"
_CONTENT_LENGTH = b"Content-Length:"


def read_message(stream) -> dict | None:
    """Read one framed JSON-RPC message from a binary stream, or None at EOF.

    Headers are read line by line until the blank separator, then exactly
    `Content-Length` bytes of UTF-8 JSON. A malformed or truncated frame at
    EOF returns None so the read loop stops cleanly.
    """
    length: int | None = None
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (_HEADER_SEP, b"\n"):
            break  # end of headers
        if line.lower().startswith(_CONTENT_LENGTH.lower()):
            try:
                length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                length = None
    if length is None:
        return None
    body = stream.read(length)
    if len(body) < length:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


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
