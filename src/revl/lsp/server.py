"""The human-facing language server: JSON-RPC method dispatch over open text
documents.

Slice 1 answers three requests — `textDocument/hover`,
`textDocument/definition` — and pushes `textDocument/publishDiagnostics` on
every open and edit. The document lifecycle (`didOpen`/`didChange`/`didClose`)
keeps a full-text mirror per URI; the analysis is stateless over that text.

`handle` maps one incoming message to the list of messages to send back (a
response, a diagnostics notification, or nothing). It is a plain method: a test
feeds it decoded messages and asserts on the returned list, no stream involved.
`serve` is the thin loop that frames those messages onto stdio.

Nothing a client can send may end the process (issue #314). An editor keeps one
long-lived server per session, so an exit is not a failed request — it is the
user's tooling going dark until they restart it. Three layers hold that line:
`handle` rejects a message whose shape is wrong with a JSON-RPC error and reads
every nested field defensively, so a `"textDocument": "x"` cannot raise;
`serve` wraps the dispatch so an unforeseen crash becomes `-32603` (or, for a
notification, a `window/logMessage`) instead of a traceback; and a malformed
frame gets `-32700` with the loop still reading. Only EOF, `exit`, or a signal
stops the server.
"""

from __future__ import annotations

import sys

from . import protocol
from .analysis import (
    compute_code_actions,
    compute_definition,
    compute_diagnostics,
    compute_hover,
)
from .document import Position

SERVER_INFO = {"name": "revl-lsp", "version": "2.0"}


class LspServer:
    def __init__(self) -> None:
        # uri -> current full text
        self._documents: dict[str, str] = {}
        # set once `exit` arrives, so the serve loop can stop
        self.shutting_down = False

    # ------------------------------------------------------------ dispatch

    def handle(self, message: dict) -> list[dict]:
        """One JSON-RPC message -> the messages to send in reply.

        A request yields a single response; a document notification yields a
        `publishDiagnostics`; anything else yields nothing.

        Every shape assumption is checked rather than assumed (issue #314). A
        message that is not an object is `-32600` (Invalid Request); `params`
        that is not an object is `-32602` (Invalid Params), the code the JSON-RPC
        spec reserves for exactly that; nested objects (`textDocument`,
        `position`, `range`, `contentChanges`) are read through the `_dict` /
        `_int` / `_str` coercions below, so a wrong type degrades to the default
        instead of raising.
        """
        if not isinstance(message, dict):
            return [protocol.error(
                None, -32600,
                "invalid request: a JSON-RPC message must be a JSON object, "
                f"got {type(message).__name__}")]

        method = message.get("method")
        request_id = _request_id(message.get("id"))
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if request_id is None:
                return []  # a malformed notification is dropped, per the spec
            return [protocol.error(
                request_id, -32602,
                "invalid params: `params` must be a JSON object, "
                f"got {type(params).__name__}")]

        if method == "initialize":
            return [protocol.response(request_id, self._initialize_result())]
        if method in ("initialized", "$/setTrace"):
            return []
        if method == "shutdown":
            self.shutting_down = True
            return [protocol.response(request_id, None)]
        if method == "exit":
            self.shutting_down = True
            return []
        if method == "textDocument/didOpen":
            return self._did_open(params)
        if method == "textDocument/didChange":
            return self._did_change(params)
        if method == "textDocument/didClose":
            return self._did_close(params)
        if method == "textDocument/hover":
            return [protocol.response(request_id, self._hover(params))]
        if method == "textDocument/definition":
            return [protocol.response(request_id, self._definition(params))]
        if method == "textDocument/codeAction":
            return [protocol.response(request_id, self._code_action(params))]

        if request_id is None:
            return []  # an unknown notification is ignored, per the spec
        return [protocol.error(request_id, -32601, f"method not found: {method}")]

    def _initialize_result(self) -> dict:
        return {
            "capabilities": {
                # 1 == full-document sync: every change carries the whole text
                "textDocumentSync": 1,
                "hoverProvider": True,
                "definitionProvider": True,
                # quick fixes for the diagnostics fixgen can rewrite (item 287)
                "codeActionProvider": {"codeActionKinds": ["quickfix"]},
            },
            "serverInfo": SERVER_INFO,
        }

    # ------------------------------------------------------------ documents

    def _did_open(self, params: dict) -> list[dict]:
        doc = _dict(params.get("textDocument"))
        uri = _str(doc.get("uri"))
        # `"text": 5` used to reach the analysis and raise there; a document
        # with no usable text is an empty document.
        self._documents[uri] = _str(doc.get("text"), "")
        return [self._publish(uri)]

    def _did_change(self, params: dict) -> list[dict]:
        uri = _str(_dict(params.get("textDocument")).get("uri"))
        changes = params.get("contentChanges")
        # full sync: the last change carries the complete new text. A
        # `contentChanges` that is not a list (an object, say) indexed as
        # `[-1]` used to raise KeyError and kill the server.
        if isinstance(changes, list) and changes and uri is not None:
            self._documents[uri] = _str(_dict(changes[-1]).get("text"), "")
        return [self._publish(uri)]

    def _did_close(self, params: dict) -> list[dict]:
        uri = _str(_dict(params.get("textDocument")).get("uri"))
        self._documents.pop(uri, None)
        # clear the client's squiggles for a document we no longer track
        return [protocol.notification("textDocument/publishDiagnostics",
                                      {"uri": uri, "diagnostics": []})]

    def _publish(self, uri: str | None) -> dict:
        diagnostics = compute_diagnostics(self._documents.get(uri, ""), _filename(uri))
        return protocol.notification("textDocument/publishDiagnostics",
                                     {"uri": uri, "diagnostics": diagnostics})

    # ------------------------------------------------------------ requests

    def _hover(self, params: dict):
        uri, position = _locate(params)
        if uri not in self._documents:
            return None
        return compute_hover(self._documents[uri], position, _filename(uri))

    def _definition(self, params: dict):
        uri, position = _locate(params)
        if uri not in self._documents:
            return None
        return compute_definition(self._documents[uri], uri, position, _filename(uri))

    def _code_action(self, params: dict):
        uri = _str(_dict(params.get("textDocument")).get("uri"))
        if uri not in self._documents:
            return []
        # `{"start": {}}` used to raise KeyError downstream: the range is
        # rebuilt field by field so every corner is an int.
        given = _dict(params.get("range"))
        lsp_range = {"start": _position_dict(given.get("start")),
                     "end": _position_dict(given.get("end"))}
        return compute_code_actions(self._documents[uri], uri, lsp_range, _filename(uri))


def _locate(params: dict) -> tuple[str | None, Position]:
    uri = _str(_dict(params.get("textDocument")).get("uri"))
    pos = _dict(params.get("position"))
    return uri, Position(_int(pos.get("line")), _int(pos.get("character")))


# ------------------------------------------------------------ coercions
#
# The client is not trusted to send the types the spec says it will (issue
# #314). Each of these turns "wrong type" into the harmless default rather
# than into an exception that would take the process down.

def _dict(value) -> dict:
    """A nested params object, or `{}` when the client sent something else."""
    return value if isinstance(value, dict) else {}


def _str(value, default=None):
    """A string field, or `default` when the client sent something else."""
    return value if isinstance(value, str) else default


def _int(value, default: int = 0) -> int:
    """An integer field, or `default`. `bool` is excluded deliberately: it is
    an `int` subclass, and `"line": true` is not a line number."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _position_dict(value) -> dict:
    pos = _dict(value)
    return {"line": _int(pos.get("line")), "character": _int(pos.get("character"))}


def _request_id(value):
    """The `id` to answer at. JSON-RPC allows a string, a number, or null; any
    other type cannot be echoed back meaningfully, so it is treated as absent
    (which makes the message a notification and the reply nothing)."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (str, int, float)) else None


def _filename(uri: str | None) -> str:
    """A display filename for the checker from a `file://` URI. The name only
    reaches diagnostic text, never the disk, so a best-effort strip is fine."""
    if not isinstance(uri, str) or not uri:
        return "<lsp>.rvl"
    return uri[len("file://"):] if uri.startswith("file://") else uri


def _crash_reply(message, exc: BaseException) -> list[dict]:
    """What to send when `handle` raised something nobody anticipated.

    A request gets `-32603` so the editor shows a failed request and moves on.
    A notification has no id to answer at, so the failure goes out as a
    `window/logMessage` — the client is told, and the server stays up either
    way.
    """
    detail = f"{type(exc).__name__}: {exc}"
    request_id = _request_id(message.get("id")) if isinstance(message, dict) else None
    if request_id is not None:
        return [protocol.error(request_id, -32603, f"internal error: {detail}")]
    return [protocol.notification(
        "window/logMessage",
        # 1 == MessageType.Error
        {"type": 1, "message": f"revl-lsp: internal error handling "
                               f"{_method_of(message)}: {detail}"})]


def _method_of(message) -> str:
    if isinstance(message, dict) and isinstance(message.get("method"), str):
        return message["method"]
    return "<unknown>"


def serve(stdin=None, stdout=None) -> int:
    """Read framed JSON-RPC from stdin, dispatch, and frame replies onto stdout
    until `exit` or EOF.

    The loop is the last line of defence for issue #314: a malformed frame is
    answered with `-32700` and the loop continues, and any exception escaping
    `handle` becomes a reply rather than a traceback. Nothing the client sends
    ends the process; only EOF and `exit` do.
    """
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    server = LspServer()
    while True:
        try:
            message = protocol.read_message(stdin)
        except protocol.MalformedFrame as exc:
            protocol.write_message(
                stdout, protocol.error(None, -32700, f"parse error: {exc}"))
            continue
        if message is None:
            break  # EOF: the peer is gone, there is nobody left to answer
        try:
            outgoing = server.handle(message)
        except Exception as exc:  # noqa: BLE001 — the whole point is to survive
            outgoing = _crash_reply(message, exc)
        for one in outgoing:
            protocol.write_message(stdout, one)
        if server.shutting_down and _method_of(message) == "exit":
            break
    return 0
