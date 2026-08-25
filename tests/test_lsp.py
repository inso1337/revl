"""Slice-1 language server (roadmap item 126): diagnostics, hover, go-to-def,
and the stdio framing loop.

The three capabilities are asserted through the plain analysis functions (text
+ position in, LSP payload out — no socket), and one test drives real messages
through the `Content-Length` framing to prove the loop parses and serializes.
"""

from __future__ import annotations

import io

import pytest

from revl.lsp import (
    LspServer,
    Position,
    compute_definition,
    compute_diagnostics,
    compute_hover,
)
from revl.lsp import protocol

# a program with one undeclared-variable rejection: `c` on line 2 is neither a
# parameter nor a `let`, which the checker refuses under G1
REJECTED = "fn add(a: Int, b: Int) -> Int {\n  return a + c\n}\n"

# a clean two-function program for cross-declaration go-to-definition
CLEAN = (
    "fn helper(x: Int) -> Int {\n"
    "  return x\n"
    "}\n"
    "fn main() -> Int {\n"
    "  return helper(1)\n"
    "}\n"
)


# ------------------------------------------------------------ diagnostics

def test_diagnostics_report_the_rejection_code_and_range():
    diags = compute_diagnostics(REJECTED)
    assert len(diags) == 1
    diag = diags[0]
    # the code is the guarantee the checker names, via diagnostics.classify
    assert diag["code"] == "G1"
    assert diag["severity"] == 1  # error
    assert diag["source"] == "revl"
    # the range lands on `c` (line 2 -> zero-based line 1), tightened onto the
    # named token rather than the whole line
    assert diag["range"]["start"]["line"] == 1
    assert diag["range"]["start"]["character"] == 13
    assert diag["range"]["end"]["character"] == 14
    assert "not declared" in diag["message"]


def test_clean_program_has_no_diagnostics():
    assert compute_diagnostics(CLEAN) == []


def test_syntax_error_still_produces_a_diagnostic():
    diags = compute_diagnostics("fn broken( {\n")
    assert len(diags) == 1
    assert diags[0]["message"]


# ------------------------------------------------------------ hover

def test_hover_on_a_diagnostic_token_surfaces_the_guarantee():
    # cursor on `c`, the offending token (line 2, column 13 zero-based)
    hover = compute_hover(REJECTED, Position(1, 13))
    value = hover["contents"]["value"]
    assert "G1" in value
    # the GUARANTEES text and the FIXES text, both from diagnostics.explain
    assert "declared access" in value
    assert "Fix:" in value


def test_hover_on_a_valid_symbol_beside_the_error_shows_its_type():
    # `a` sits on the same line as the rejection but is a valid parameter; its
    # hover is its own type, not the neighbouring diagnostic's guarantee
    hover = compute_hover(REJECTED, Position(1, 9))
    assert hover["contents"]["value"] == "```revl\na: Int\n```"


def test_hover_on_a_declaration_shows_its_signature():
    hover = compute_hover(CLEAN, Position(0, 3))  # `helper` at its declaration
    assert "fn helper(x: Int) -> Int" in hover["contents"]["value"]


def test_hover_off_an_identifier_is_none():
    assert compute_hover(CLEAN, Position(2, 0)) is None  # the `}` line


# ------------------------------------------------------------ definition

def test_definition_jumps_to_the_declaration():
    # `helper` used on line 5 (zero-based 4) resolves to its declaration line 1
    loc = compute_definition(CLEAN, "file:///m.rvl", Position(4, 9))
    assert loc["uri"] == "file:///m.rvl"
    assert loc["range"]["start"]["line"] == 0
    assert loc["range"]["start"]["character"] == 3  # the `helper` token


def test_definition_of_a_parameter_lands_on_the_parameter():
    # `x` used in helper's body resolves to the parameter binding on line 1
    loc = compute_definition(CLEAN, "file:///m.rvl", Position(1, 9))
    assert loc["range"]["start"]["line"] == 0
    assert loc["range"]["start"]["character"] == 10  # `x` in the parameter list


def test_definition_of_an_unknown_symbol_is_none():
    assert compute_definition(CLEAN, "file:///m.rvl", Position(1, 2)) is None


# ------------------------------------------------------------ server dispatch

def test_initialize_advertises_the_slice_one_capabilities():
    server = LspServer()
    out = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    caps = out[0]["result"]["capabilities"]
    assert caps["hoverProvider"] is True
    assert caps["definitionProvider"] is True
    assert caps["textDocumentSync"] == 1


def test_did_open_publishes_diagnostics():
    server = LspServer()
    out = server.handle({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": "file:///a.rvl", "text": REJECTED}},
    })
    assert len(out) == 1
    note = out[0]
    assert note["method"] == "textDocument/publishDiagnostics"
    assert note["params"]["uri"] == "file:///a.rvl"
    assert note["params"]["diagnostics"][0]["code"] == "G1"


def test_did_change_republishes_against_the_new_text():
    server = LspServer()
    server.handle({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": "file:///a.rvl", "text": REJECTED}},
    })
    out = server.handle({
        "jsonrpc": "2.0", "method": "textDocument/didChange",
        "params": {"textDocument": {"uri": "file:///a.rvl"},
                   "contentChanges": [{"text": CLEAN}]},
    })
    # the edit fixed the program, so the client's squiggles are cleared
    assert out[0]["params"]["diagnostics"] == []


def test_hover_request_over_a_tracked_document():
    server = LspServer()
    server.handle({
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": "file:///a.rvl", "text": CLEAN}},
    })
    out = server.handle({
        "jsonrpc": "2.0", "id": 7, "method": "textDocument/hover",
        "params": {"textDocument": {"uri": "file:///a.rvl"},
                   "position": {"line": 0, "character": 3}},
    })
    assert out[0]["id"] == 7
    assert "fn helper" in out[0]["result"]["contents"]["value"]


def test_unknown_request_is_a_method_not_found_error():
    server = LspServer()
    out = server.handle({"jsonrpc": "2.0", "id": 9, "method": "textDocument/rename"})
    assert out[0]["error"]["code"] == -32601


# ------------------------------------------------------------ stdio framing

def test_framing_round_trip_drives_initialize_and_hover():
    """Feed two real Content-Length frames through the byte-level loop and read
    the framed replies back, proving the wire codec parses and serializes."""
    server = LspServer()

    def frame(message: dict) -> bytes:
        buf = io.BytesIO()
        protocol.write_message(buf, message)
        return buf.getvalue()

    inbound = (
        frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + frame({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                 "params": {"textDocument": {"uri": "file:///a.rvl", "text": REJECTED}}})
    )
    stdin = io.BytesIO(inbound)
    stdout = io.BytesIO()

    # drive the two messages by hand through the same read/handle/write path
    # the serve loop uses
    for _ in range(2):
        message = protocol.read_message(stdin)
        assert message is not None
        for outgoing in server.handle(message):
            protocol.write_message(stdout, outgoing)

    stdout.seek(0)
    first = protocol.read_message(stdout)
    second = protocol.read_message(stdout)
    assert first["id"] == 1
    assert first["result"]["capabilities"]["hoverProvider"] is True
    assert second["method"] == "textDocument/publishDiagnostics"
    assert second["params"]["diagnostics"][0]["code"] == "G1"
    # the stream holds exactly the two framed replies
    assert protocol.read_message(stdout) is None


def test_read_message_returns_none_at_eof():
    assert protocol.read_message(io.BytesIO(b"")) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
