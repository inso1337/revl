"""Nested symbols for revl_source and {symbol} edits (issue #1733).

To change one method an agent still read and resent its whole component: in
`examples/app/notes.rvl` the 373-token `NotesHttp` for a six-line method. A
dotted path addresses the member itself, `deps` are what that member names,
and `touched` reports the member, not the component. A member's span is used
only once it is proved to be exactly that member.
"""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp import symbols  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

NOTES = ROOT / "examples" / "app" / "notes.rvl"

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _notes() -> dict:
    text = NOTES.read_text(encoding="utf-8")
    return {"source": None, "files": [str(NOTES)],
            "files_content": {str(NOTES): text}, "modules": {}}


@pytest.fixture
def repo_root():
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(ROOT),))
    try:
        yield
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old


# ------------------------------------------------ reading


def test_one_method_reads_far_smaller_than_its_component(repo_root):
    method = _call("revl_source", {"files": [str(NOTES)], "comments": False,
                                   "symbol": "NotesHttp.notes_api.create_note"})
    whole = _call("revl_source", {"files": [str(NOTES)], "symbol": "NotesHttp"})
    assert method["ok"] is True, method
    assert method["kind"] == "method" and method["parent"] == "NotesHttp"
    assert method["text"].startswith("fn create_note(note) {")
    assert "fn list_notes" not in method["text"]
    assert _tokens(method["text"]) * 4 < _tokens(whole["text"])


def test_a_methods_deps_are_what_it_names_not_its_component():
    """`list_notes` calls `take` and the `store` require, so its deps are
    `take` and `NoteStore`; `create_note` names no function, so `take` is not
    in its deps although its component's are."""
    vs = _notes()
    listing = symbols.read(vs, "NotesHttp.list_notes", deps=True, comments=False)
    creating = symbols.read(vs, "NotesHttp.create_note", deps=True, comments=False)
    assert {d["symbol"] for d in listing["deps"]} == {"take", "NoteStore"}
    assert {d["symbol"] for d in creating["deps"]} == {"NoteStore"}


def test_the_short_form_and_a_service_operation_resolve():
    vs = _notes()
    assert symbols.read(vs, "NotesHttp.get_note")["symbol"] == \
        "NotesHttp.notes_api.get_note"
    operation = symbols.read(vs, "NoteStore.create")
    assert operation["kind"] == "operation"
    assert operation["text"].strip() == "emission[store] fn create(row: Note)"
    provision = symbols.read(vs, "NotesHttp.notes_api", comments=False)
    assert provision["kind"] == "provision"
    assert provision["text"].count("fn ") == 3


def test_a_member_that_shares_a_line_is_not_addressable():
    """`provide p { fn a() = 1 }` on one line: the method's line is also the
    provide block's, so no span is exactly the method. Refused, not guessed."""
    vs = {"source": "service P { fn a() -> Int }\n"
                    "component C provides p: P {\n"
                    "  provide p { fn a() = 1 }\n"
                    "}\n", "modules": {}}
    with pytest.raises(symbols.SymbolError, match="could not be isolated"):
        symbols.read(vs, "C.p.a")


def test_an_unknown_member_names_the_members_there_are():
    with pytest.raises(symbols.SymbolError, match="members: notes_api"):
        symbols.read(_notes(), "NotesHttp.nope")


# ------------------------------------------------ editing


def test_a_member_replacement_changes_only_that_member():
    vs = _notes()
    buffer, new_text, echo = symbols.replace(
        vs, "NotesHttp.create_note",
        "fn create_note(note) {\n  return Err(not_found_error(\"closed\"))\n}")
    assert echo["symbol"] == "NotesHttp.notes_api.create_note"
    after = {**vs, "files_content": {str(NOTES): new_text}}
    touched = symbols.touched(vs, after)
    assert touched == [{"symbol": "NotesHttp.notes_api.create_note",
                        "kind": "method", "parent": "NotesHttp",
                        "parentKind": "component", "buffer": str(NOTES),
                        "change": "changed"}]
    # the replacement is indented to the member's place
    assert '      return Err(not_found_error("closed"))' in new_text


def test_a_replacement_that_reaches_past_the_member_is_refused():
    """A member edit that also declares a second method changes more than
    that member: refused, never spliced."""
    with pytest.raises(symbols.SymbolError, match="changes more than that member"):
        symbols.replace(_notes(), "NotesHttp.create_note",
                        "fn create_note(note) { return Ok(note) }\n"
                        "fn extra() { return 1 }")


SERVICE = "service Tool { fn describe() -> Str\n               fn size() -> Int }\n"
COMPONENT = ("component T provides tool: Tool {\n"
             "  provide tool {\n"
             '    fn describe() = "one"\n'
             "    fn size() = 1\n"
             "  }\n"
             "}\n")


@needs_runtime
def test_revl_edit_by_member_swaps_and_reports_the_member(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    try:
        _call("revl_load", {"source": SERVICE + COMPONENT})
        result = _call("revl_edit", {"edits": [{"symbol": "T.tool.size",
                                                "replacement": "fn size() = 2"}]})
        assert result["ok"] is True and result["swapped"] is True, result
        assert [t["symbol"] for t in result["touched"]] == ["T.tool.size"]
        assert _call("revl_call", {"key": "tool", "method": "size"})["result"] == 2
        assert _call("revl_call", {"key": "tool", "method": "describe"})["result"] == "one"
        change = _call("revl_change", {"commit": True, "replace": {"component": "T.tool.describe",
                                                   "source": 'fn describe() = "two"'}})
        assert change["committed"] is True, change
        assert change["components"] == [{"component": "T", "change": "changed"}]
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
