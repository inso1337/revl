"""The language server answers bad input instead of exiting on it.

Issue #314. `python -P -m revl.lsp` used to die on structurally malformed
JSON-RPC — `AttributeError: 'list' object has no attribute 'get'`, exit 1 —
and on any compiler crash, and to exit 0 in silence on a frame it could not
decode. An editor holds one server for the whole session, so each of those is
the user's tooling going dark until they restart it. (The `revl lsp` form is
the documented happy path; the absolute-interpreter fallback `python -P -m
revl.lsp` needs the `-P` because `-m` puts the CWD at `sys.path[0]`, issue
#317.)

The property under test is not "no traceback". It is that the server is STILL
ANSWERING afterwards: every case here feeds one bad message followed by a
perfectly good `initialize`, and asserts the `initialize` was answered.

Covered:
  * malformed frames (bad JSON, no `Content-Length`, a negative one) get
    `-32700` and the loop keeps reading;
  * a message that is not an object gets `-32600`, `params` that is not an
    object gets `-32602`;
  * every nested-field type confusion in the issue is absorbed;
  * a compiler crash on the open document becomes a diagnostic, not an exit.

Non-vacuity: revert the `try/except` in `serve`, or the `isinstance` guard at
the top of `handle`, and the shape cases fail with a dead server.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.lsp import protocol  # noqa: E402
from revl.lsp.analysis import compute_diagnostics  # noqa: E402
from revl.lsp.server import LspServer, serve  # noqa: E402

#: the "are you still there" probe appended after every bad message
PROBE_ID = 4242
PROBE = {"jsonrpc": "2.0", "id": PROBE_ID, "method": "initialize", "params": {}}


def _frame(payload) -> bytes:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def _drive(*payloads) -> tuple[list[dict], int]:
    """Feed raw bytes through `serve` over BytesIO; return the replies."""
    stdin = io.BytesIO(b"".join(payloads))
    stdout = io.BytesIO()
    code = serve(stdin, stdout)
    stdout.seek(0)
    replies = []
    while (one := protocol.read_message(stdout)) is not None:
        replies.append(one)
    return replies, code


def _answered_probe(replies: list[dict]) -> bool:
    return any(r.get("id") == PROBE_ID and "result" in r for r in replies)


def _errors(replies: list[dict]) -> list[dict]:
    return [r["error"] for r in replies if "error" in r]


# ------------------------------------------------------- malformed framing

@pytest.mark.parametrize(("label", "frame"), [
    ("body is not JSON", _frame(b"{oops")),
    ("body is not UTF-8", _frame(b'\xff\xfe"nope"')),
])
def test_a_bad_body_gets_a_parse_error_and_the_server_keeps_reading(label, frame):
    replies, code = _drive(frame, _frame(PROBE))

    assert code == 0
    assert [e["code"] for e in _errors(replies)][:1] == [-32700], label
    # A body of the declared length WAS consumed, so the stream is still on a
    # frame boundary and the next request is served normally.
    assert _answered_probe(replies), f"server stopped answering after {label}"


@pytest.mark.parametrize(("label", "raw"), [
    ("no Content-Length header", b"X-Bogus: 1\r\n\r\n{}"),
    ("Content-Length is not a number", b"Content-Length: abc\r\n\r\n{}"),
    ("Content-Length is negative", b"Content-Length: -5\r\n\r\nzzzzz"),
])
def test_a_bad_header_gets_a_parse_error_instead_of_a_silent_exit(label, raw):
    # These cannot resynchronize — the reader never learns how many body bytes
    # to skip — so the contract is narrower than above: an error goes out, the
    # loop does not raise, and the process ends only at EOF. Before the fix
    # this path returned None and `serve` read it as EOF: exit 0, no reply at
    # all, which an editor sees as the server dying for no stated reason.
    replies, code = _drive(raw, _frame(PROBE))

    assert code == 0
    codes = [e["code"] for e in _errors(replies)]
    assert codes and codes[0] == -32700, f"{label}: {replies}"


def test_eof_is_still_a_clean_stop():
    replies, code = _drive(b"")
    assert (replies, code) == ([], 0)


def test_a_truncated_frame_at_eof_is_eof_not_a_parse_error():
    # Nobody is left to answer, so this must stay a quiet stop.
    replies, code = _drive(b"Content-Length: 500\r\n\r\n{}")
    assert (replies, code) == ([], 0)


def test_read_message_separates_eof_from_a_bad_frame():
    assert protocol.read_message(io.BytesIO(b"")) is None
    with pytest.raises(protocol.MalformedFrame):
        protocol.read_message(io.BytesIO(_frame(b"not json")))


# --------------------------------------------------------- malformed shapes

#: every shape from the issue, each one previously a process-ending exception
BAD_MESSAGES = {
    "message is a list": [1, 2],
    "message is a string": "hello",
    "message is a number": 7,
    "params is a list": {"jsonrpc": "2.0", "id": 1, "method": "textDocument/hover",
                         "params": [1]},
    "params is a string": {"jsonrpc": "2.0", "id": 1, "method": "textDocument/hover",
                           "params": "x"},
    "textDocument is a string": {
        "jsonrpc": "2.0", "id": 1, "method": "textDocument/hover",
        "params": {"textDocument": "x", "position": {"line": 0, "character": 0}}},
    "didOpen text is an int": {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": "file:///a.rvl", "text": 5}}},
    "didOpen uri is a list": {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": [1], "text": "fn f() {}"}}},
    "contentChanges is an object": {
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": "file:///a.rvl"},
                   "contentChanges": {"a": 1}}},
    "contentChanges entry is a string": {
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": "file:///a.rvl"},
                   "contentChanges": ["whole new text"]}},
    "position line is a string": {
        "jsonrpc": "2.0", "id": 1, "method": "textDocument/hover",
        "params": {"textDocument": {"uri": "file:///a.rvl"},
                   "position": {"line": "a", "character": 0}}},
    "position is a list": {
        "jsonrpc": "2.0", "id": 1, "method": "textDocument/definition",
        "params": {"textDocument": {"uri": "file:///a.rvl"}, "position": [0, 0]}},
    "codeAction range start is empty": {
        "jsonrpc": "2.0", "id": 1, "method": "textDocument/codeAction",
        "params": {"textDocument": {"uri": "file:///a.rvl"}, "range": {"start": {}}}},
    "codeAction range is a string": {
        "jsonrpc": "2.0", "id": 1, "method": "textDocument/codeAction",
        "params": {"textDocument": {"uri": "file:///a.rvl"}, "range": "0:0"}},
    "didClose textDocument is null": {
        "jsonrpc": "2.0", "method": "textDocument/didClose",
        "params": {"textDocument": None}},
    "id is an object": {"jsonrpc": "2.0", "id": {"nested": 1}, "method": "shutdown"},
}


@pytest.mark.parametrize("label", sorted(BAD_MESSAGES))
def test_a_malformed_message_never_stops_the_server(label):
    replies, code = _drive(_frame(BAD_MESSAGES[label]), _frame(PROBE))

    assert code == 0, label
    assert _answered_probe(replies), f"server stopped answering after {label}: {replies}"


def test_a_non_object_message_is_an_invalid_request():
    assert LspServer().handle([1, 2]) == [
        protocol.error(None, -32600,
                       "invalid request: a JSON-RPC message must be a JSON object, "
                       "got list")]


def test_non_object_params_on_a_request_is_invalid_params():
    out = LspServer().handle(
        {"jsonrpc": "2.0", "id": 1, "method": "textDocument/hover", "params": [1]})
    assert [one["error"]["code"] for one in out] == [-32602]


def test_non_object_params_on_a_notification_is_dropped_silently():
    # A notification has no id to answer at; the spec says do not reply.
    assert LspServer().handle(
        {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": [1]}) == []


def test_an_unforeseen_crash_in_handle_becomes_an_error_response(monkeypatch):
    # The backstop, exercised directly: whatever else `handle` might raise in
    # future, `serve` must turn it into a reply rather than a traceback.
    real = LspServer.handle

    def boom(self, message):
        if message.get("method") == "textDocument/hover":
            raise ZeroDivisionError("synthetic")
        return real(self, message)

    monkeypatch.setattr(LspServer, "handle", boom)
    replies, code = _drive(
        _frame({"jsonrpc": "2.0", "id": 5, "method": "textDocument/hover",
                "params": {}}),
        _frame(PROBE))

    assert code == 0
    crash = [r for r in replies if r.get("id") == 5]
    assert crash and crash[0]["error"]["code"] == -32603
    assert "ZeroDivisionError" in crash[0]["error"]["message"]
    assert _answered_probe(replies)


def test_an_unforeseen_crash_on_a_notification_is_logged_to_the_client(monkeypatch):
    real = LspServer.handle

    def boom(self, message):
        if message.get("method") == "textDocument/didChange":
            raise ZeroDivisionError("synthetic")
        return real(self, message)

    monkeypatch.setattr(LspServer, "handle", boom)
    replies, code = _drive(
        _frame({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {}}),
        _frame(PROBE))

    assert code == 0
    logs = [r for r in replies if r.get("method") == "window/logMessage"]
    assert logs and logs[0]["params"]["type"] == 1
    assert "ZeroDivisionError" in logs[0]["params"]["message"]
    assert _answered_probe(replies)


# ---------------------------------------------------------- compiler crashes

#: half-typed source the editor will happily send on a keystroke, each of which
#: took the compiler out through a path that is not a `RevlError`
CRASHING_DOCUMENTS = {
    "nesting deeper than the parser's stack":
        "fn f() -> Int { return " + "(" * 300 + "1" + ")" * 300 + " }",
    "an integer literal past CPython's digit limit":
        "fn f() -> Int { return " + "9" * 5000 + " }",
}


@pytest.mark.parametrize("label", sorted(CRASHING_DOCUMENTS))
def test_a_compiler_crash_becomes_a_diagnostic(label):
    diagnostics = compute_diagnostics(CRASHING_DOCUMENTS[label])

    assert len(diagnostics) == 1, label
    only = diagnostics[0]
    assert only["code"] == "REVL-INTERNAL"
    assert only["severity"] == 1
    assert only["range"] == {"start": {"line": 0, "character": 0},
                             "end": {"line": 0, "character": 0}}
    # Reported, not swallowed: an empty list would read as "your file is fine".
    assert only["message"].strip()


@pytest.mark.parametrize("label", sorted(CRASHING_DOCUMENTS))
def test_a_crashing_document_does_not_stop_the_server(label):
    replies, code = _drive(
        _frame({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                "params": {"textDocument": {"uri": "file:///a.rvl",
                                            "text": CRASHING_DOCUMENTS[label]}}}),
        _frame(PROBE))

    assert code == 0
    published = [r for r in replies if r.get("method") == "textDocument/publishDiagnostics"]
    assert published and published[0]["params"]["diagnostics"], label
    assert _answered_probe(replies)


def test_a_clean_document_still_reports_no_diagnostics():
    # The crash handler must not turn every compile into an internal error.
    assert compute_diagnostics("fn f() -> Int { return 1 }") == []


# ------------------------------------------------------ the real process

def test_the_installed_entry_point_exits_zero_on_a_malformed_frame():
    # The unit tests drive `serve` in-process; this one proves the shipped
    # `python -P -m revl.lsp` behaves the same (the `-P` is the PYTHONSAFEPATH
    # safety bit, issue #317). Before the fix it exited 1 with an
    # AttributeError traceback on stderr.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    proc = subprocess.run(
        [sys.executable, "-m", "revl.lsp"],
        input=_frame([1, 2]) + _frame(PROBE),
        capture_output=True, env=env, timeout=180)

    assert proc.returncode == 0, proc.stderr.decode()
    assert b"Traceback" not in proc.stderr
    replies = []
    stream = io.BytesIO(proc.stdout)
    while (one := protocol.read_message(stream)) is not None:
        replies.append(one)
    assert [e["code"] for e in _errors(replies)] == [-32600]
    assert _answered_probe(replies), proc.stdout
