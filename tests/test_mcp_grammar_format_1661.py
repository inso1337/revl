"""MCP `revl_grammar` with `format` / `category` (issue #1661).

With no arguments the tool answers as before: the prose summary, the
guarantees and their fixes. With `format` it returns the grammar of revl source
derived from the parser, the same text `revl grammar --format F --category C`
prints, so a hole-filling client can constrain a decoder without a checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import source_grammar as sg  # noqa: E402
from revl.cli.parser import build_parser  # noqa: E402
from revl.diagnostics import FIXES, GUARANTEES  # noqa: E402
from revl.grammar_summary import PROSE_GRAMMAR  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _result(arguments: dict) -> dict:
    """The tool result, without the session footer every response carries
    (issue #1693; pinned in test_mcp_state_footer_1693.py), so these cases
    compare the grammar payload alone."""
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_grammar", "arguments": arguments}})
    result = response["result"]
    result["structuredContent"].pop("sessionState")
    return result


def test_no_arguments_is_still_the_prose_summary():
    payload = _result({})["structuredContent"]
    assert payload == {"ok": True, "grammar": PROSE_GRAMMAR,
                       "guarantees": GUARANTEES, "fixes": FIXES}


@pytest.mark.parametrize("fmt", sg.FORMATS)
@pytest.mark.parametrize("category", sorted(sg.CATEGORIES))
def test_format_and_category_return_the_derived_grammar(fmt, category):
    result = _result({"format": fmt, "category": category})
    assert not result.get("isError")
    assert result["structuredContent"] == {
        "ok": True, "format": fmt, "category": category,
        "grammar": sg.render(fmt, category)}


def test_category_defaults_to_program():
    payload = _result({"format": "lark"})["structuredContent"]
    assert payload["category"] == "program"
    assert payload["grammar"] == sg.render("lark", "program")
    assert payload["grammar"] == (ROOT / "grammar" / "revl.lark").read_text()


@pytest.mark.parametrize("arguments, needle", [
    ({"format": "yacc"}, "`format` must be one of lark, gbnf, ebnf"),
    ({"category": "expression"}, "when `category` is given"),
    ({"format": "gbnf", "category": "requires"}, "`category` must be one of"),
])
def test_a_bad_argument_is_a_refusal_not_the_summary(arguments, needle):
    result = _result(arguments)
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert payload["ok"] is False
    assert needle in payload["diagnostics"][0]["message"]


def test_the_schema_and_the_cli_offer_the_same_choices():
    listed = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool = next(t for t in listed["result"]["tools"] if t["name"] == "revl_grammar")
    props = tool["inputSchema"]["properties"]
    assert props["format"]["enum"] == list(sg.FORMATS)
    assert props["category"]["enum"] == list(sg.CATEGORIES)
    assert "required" not in tool["inputSchema"]

    sub = build_parser()._subparsers._group_actions[0].choices["grammar"]
    choices = {a.dest: a.choices for a in sub._actions if a.choices}
    assert tuple(choices["format"]) == sg.FORMATS
    assert tuple(choices["category"]) == tuple(sg.CATEGORIES)
