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
        """
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

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
        doc = params.get("textDocument") or {}
        uri = doc.get("uri")
        self._documents[uri] = doc.get("text", "")
        return [self._publish(uri)]

    def _did_change(self, params: dict) -> list[dict]:
        uri = (params.get("textDocument") or {}).get("uri")
        changes = params.get("contentChanges") or []
        if changes and uri is not None:
            # full sync: the last change carries the complete new text
            self._documents[uri] = changes[-1].get("text", "")
        return [self._publish(uri)]

    def _did_close(self, params: dict) -> list[dict]:
        uri = (params.get("textDocument") or {}).get("uri")
        self._documents.pop(uri, None)
        # clear the client's squiggles for a document we no longer track
        return [protocol.notification("textDocument/publishDiagnostics",
                                      {"uri": uri, "diagnostics": []})]

    def _publish(self, uri: str) -> dict:
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
        uri = (params.get("textDocument") or {}).get("uri")
        if uri not in self._documents:
            return []
        lsp_range = params.get("range") or {
            "start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
        return compute_code_actions(self._documents[uri], uri, lsp_range, _filename(uri))


def _locate(params: dict) -> tuple[str, Position]:
    uri = (params.get("textDocument") or {}).get("uri")
    pos = params.get("position") or {}
    return uri, Position(pos.get("line", 0), pos.get("character", 0))


def _filename(uri: str | None) -> str:
    """A display filename for the checker from a `file://` URI. The name only
    reaches diagnostic text, never the disk, so a best-effort strip is fine."""
    if not uri:
        return "<lsp>.rvl"
    return uri[len("file://"):] if uri.startswith("file://") else uri


def serve(stdin=None, stdout=None) -> int:
    """Read framed JSON-RPC from stdin, dispatch, and frame replies onto stdout
    until `exit` or EOF."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    server = LspServer()
    while True:
        message = protocol.read_message(stdin)
        if message is None:
            break
        for outgoing in server.handle(message):
            protocol.write_message(stdout, outgoing)
        if server.shutting_down and message.get("method") == "exit":
            break
    return 0
