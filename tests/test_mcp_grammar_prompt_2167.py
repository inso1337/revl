"""`revl_grammar {prompt: true}` serves the complete grammar over MCP, and the
prose payload stops deferring to an unpackaged path (issue #2167).

Two dead ends the issue measured: the complete grammar
(`grammar_summary.PROMPT_GRAMMAR`, the artifact "meant to be pinned verbatim
into an authoring system prompt") was reachable only through
`revl grammar --prompt`, and the one payload MCP *did* serve deferred its first
line to `docs/syntax-2.0.md` — a file the wheel omits (pyproject maps in only
`src/revl`), so an installed deployment does not have it.

The fix is a property on the existing tool, not a new verb: `format` is the
parser-derived decoder set (`source_grammar.FORMATS`) and the CLI offers the
same choices, so `prompt` is a separate boolean rather than a fourth enum value
that is not a decoder format. `disclosure.CORE` is untouched, and the flag is
the MCP twin of the CLI's own `--prompt`. Serving the complete grammar is what
newly exposes its own first line to an agent with no checkout, so both payloads
now name the reachable view that serves the other instead of a `docs/` path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import source_grammar as sg  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.diagnostics import FIXES, GUARANTEES  # noqa: E402
from revl.grammar_summary import PROMPT_GRAMMAR, PROSE_GRAMMAR  # noqa: E402
from revl.mcp import disclosure, repeat  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

ADVERTISED = {tool["name"]: tool for tool in server_mod._ADVERTISED}
#: CORE may name a verb a later PR adds; only the ones this server has
PRESENT_CORE = [name for name in disclosure.CORE if name in ADVERTISED]


@pytest.fixture(autouse=True)
def _tiered():
    before = disclosure.all_tools()
    disclosure.set_all_tools(False)
    repeat.forget()
    yield
    disclosure.set_all_tools(before)
    repeat.forget()


def _call(arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_grammar", "arguments": arguments}})
    return response["result"]


def _payload(arguments: dict) -> dict:
    """The tool payload without the session footer every response carries
    (issue #1693), so these cases compare the grammar alone."""
    result = _call(arguments)
    result["structuredContent"].pop("sessionState")
    return result["structuredContent"]


# ---- (a) the prompt artifact is served, byte for byte ------------------------

def test_prompt_returns_the_complete_grammar_byte_identically():
    result = _call({"prompt": True})
    assert result["isError"] is False
    payload = _payload({"prompt": True})
    assert payload["ok"] is True and payload["prompt"] is True
    assert payload["grammar"] == PROMPT_GRAMMAR
    # the artifact is pinned verbatim into an authoring prompt, so the served
    # text must be the constant's bytes, not a re-rendering of it
    assert payload["grammar"].encode("utf-8") == PROMPT_GRAMMAR.encode("utf-8")


def test_prompt_is_the_cli_prompt_text(capsys):
    assert main(["grammar", "--prompt"]) == 0
    assert capsys.readouterr().out == _payload({"prompt": True})["grammar"]


def test_prompt_is_not_the_prose_summary():
    # load-bearing: if the flag fell through to the default, (a) would still
    # pass on a payload that answers the wrong question
    assert PROMPT_GRAMMAR != PROSE_GRAMMAR
    assert _payload({"prompt": True})["grammar"] != _payload({})["grammar"]


def test_the_complete_grammar_is_not_cli_only():
    # the issue's defect: `PROMPT_GRAMMAR` was referenced nowhere under
    # src/revl/mcp/ — the served payload IS that constant, so the reference
    # exists and is the one the tool answers with
    assert server_mod._PROMPT_GRAMMAR == PROMPT_GRAMMAR


def test_prompt_is_truthy_only_as_a_flag():
    # an explicit false is the summary, exactly like omitting it
    assert _payload({"prompt": False}) == _payload({})


def test_prompt_with_a_format_is_refused_not_silently_ignored():
    result = _call({"prompt": True, "format": "lark"})
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert payload["ok"] is False
    assert "takes no `format`" in payload["diagnostics"][0]["message"]


# ---- (b) the prose payload stops naming an unpackaged path -------------------

def test_the_docs_path_the_prose_payload_used_to_name_is_not_packaged():
    """Why the old pointer was dead: the wheel maps in only `src/revl`, so a
    checkout's `docs/syntax-2.0.md` does not exist in an installed deployment."""
    assert 'packages = ["src/revl"]' in (ROOT / "pyproject.toml").read_text()
    assert (ROOT / "docs" / "syntax-2.0.md").exists()
    assert not (ROOT / "src" / "revl" / "docs").exists()


def test_the_prose_payload_no_longer_names_a_path_the_wheel_omits():
    assert "docs/syntax-2.0.md" not in PROSE_GRAMMAR
    assert "docs/" not in PROSE_GRAMMAR, (
        "the prose payload names a path under `docs/`, which the wheel omits — "
        "a dead reference an agent will go looking for"
    )
    assert "docs/" not in _payload({})["grammar"]


def test_the_prompt_payload_does_not_name_a_path_the_wheel_omits_either():
    """Remedy (1) is what newly exposes this payload to an agent with no
    checkout, so its opening directive had to stop sending that agent to
    `docs/syntax-2.0.md` too — the same dead reference, in the artifact an
    authoring prompt pins. (Its per-construct `docs/*.md` provenance
    citations are a different thing: annotations, not a "go read the spec
    here" pointer, and they are left as they were.)"""
    first_line = PROMPT_GRAMMAR.splitlines()[0]
    assert "docs/" not in first_line
    assert "docs/syntax-2.0.md" not in first_line
    assert "docs/" not in _payload({"prompt": True})["grammar"].splitlines()[0]


def test_the_prompt_payloads_replacement_pointer_names_a_reachable_view():
    """It names `revl_grammar`, whose default answer is that prose view."""
    assert "prose summary: revl_grammar" in PROMPT_GRAMMAR.splitlines()[0]
    assert "revl_grammar" in ADVERTISED
    assert _payload({})["grammar"] == PROSE_GRAMMAR


def test_the_replacement_pointer_names_a_reachable_mcp_flag():
    """The replacement names `revl_grammar {prompt: true}`: the verb is listed
    and the property is in its schema, and calling it answers with the complete
    grammar. (Checked structurally first, so a rename fails here and not with a
    confusing protocol error.)"""
    assert "revl_grammar {prompt: true}" in PROSE_GRAMMAR
    assert "revl_grammar" in ADVERTISED
    schema = ADVERTISED["revl_grammar"]["inputSchema"]
    assert "prompt" in schema["properties"]
    assert "prompt" not in (schema.get("required") or [])
    assert _payload({"prompt": True})["grammar"] == PROMPT_GRAMMAR


def test_the_replacement_pointer_names_a_reachable_cli_subcommand(capsys):
    assert "revl grammar --prompt" in PROSE_GRAMMAR
    assert main(["grammar", "--prompt"]) == 0
    assert capsys.readouterr().out == PROMPT_GRAMMAR


# ---- (c) the old behaviour, and the tier, are untouched ----------------------

def test_no_arguments_is_still_the_prose_summary():
    assert _payload({}) == {"ok": True, "grammar": PROSE_GRAMMAR,
                            "guarantees": GUARANTEES, "fixes": FIXES}


@pytest.mark.parametrize("fmt", sg.FORMATS)
@pytest.mark.parametrize("category", sorted(sg.CATEGORIES))
def test_every_derived_format_still_works(fmt, category):
    result = _call({"format": fmt, "category": category})
    assert not result["isError"]
    result["structuredContent"].pop("sessionState")
    assert result["structuredContent"] == {
        "ok": True, "format": fmt, "category": category,
        "grammar": sg.render(fmt, category)}


def test_the_format_enum_is_still_the_parser_derived_set():
    # `prompt` is not a decoder format, so it is not in `format`'s enum — that
    # is what keeps this tool's shape (and the CLI's `--format` choices) intact
    schema = ADVERTISED["revl_grammar"]["inputSchema"]
    assert schema["properties"]["format"]["enum"] == list(sg.FORMATS)
    assert "prompt" not in schema["properties"]["format"]["enum"]


def test_no_tool_was_added_to_the_listed_tier():
    listed = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == PRESENT_CORE
    assert len(disclosure.CORE) <= 14
    assert disclosure.CORE[-1] == disclosure.DISCOVERY
    # `revl_grammar` is not a CORE verb at all — it is reached through the
    # discovery topic, so the flag could only ride on it, never be a sibling
    assert "revl_grammar" not in disclosure.CORE
    assert "revl_grammar" in ADVERTISED
    assert "revl_grammar" in disclosure.TOPICS["author"][1]
    # one property added to the existing verb; the advertised surface is the
    # same size it was
    assert set(ADVERTISED["revl_grammar"]["inputSchema"]["properties"]) == {
        "format", "category", "prompt"}
    assert len(ADVERTISED) == len(server_mod.TOOLS)
