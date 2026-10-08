"""Symbol-addressed reads and edits (issue #1714).

To change one component an agent used to read the whole file: about 4,858
tokens for `examples/app/notes.rvl` by the issue's proxy, ceil(chars / 4).
`revl_source` returns the addressed declaration, optionally with the
declarations it names and without comments, and `revl_edit` replaces a
declaration by symbol. These tests hold the issue's exits:

* `revl_source` for `NotesHttp` with deps and no comments is under 320 tokens
  of code by the proxy;
* a symbol edit admits and swaps, with the disk untouched;
* a change task completes with no whole-file read: read one symbol, replace
  it, call the result;

plus the addressing forms, the `touched` lists, and the comment-free rendering
leaving the program unchanged.
"""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.formatter import format_source  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
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


# ------------------------------------------------ reading by symbol


def test_notes_http_with_deps_and_no_comments_is_under_320_tokens(repo_root):
    result = _call("revl_source", {"files": [str(NOTES)], "symbol": "NotesHttp",
                                   "with": ["deps"], "comments": False})
    assert result["ok"] is True, result
    assert result["kind"] == "component" and result["symbol"] == "NotesHttp"
    code = result["text"] + "".join(d["text"] for d in result["deps"])
    whole = NOTES.read_text(encoding="utf-8")
    print(f"NotesHttp + deps, no comments: {_tokens(code)} tokens "
          f"(whole file {_tokens(whole)})")
    assert _tokens(code) < 320
    assert _tokens(whole) > 10 * _tokens(code)
    assert "//" not in code
    assert {d["symbol"] for d in result["deps"]} >= {"NoteStore", "NotesApi"}
    assert result["text"].startswith("component NotesHttp requires store: NoteStore")


def test_with_comments_the_text_is_verbatim_with_its_doc_comment(repo_root):
    result = _call("revl_source", {"files": [str(NOTES)], "symbol": "NotesHttp"})
    whole = NOTES.read_text(encoding="utf-8")
    assert result["text"] in whole
    assert result["text"].startswith("// The HTTP component wires the endpoints")
    assert "deps" not in result


def test_a_line_addresses_the_declaration_that_contains_it(repo_root):
    line = next(i for i, text in enumerate(
        NOTES.read_text(encoding="utf-8").split("\n"), 1)
        if "fn create_note(note) {" in text)
    result = _call("revl_source", {"files": [str(NOTES)],
                                   "symbol": f"{NOTES}:{line}"})
    assert result["ok"] is True, result
    assert result["symbol"] == "NotesHttp"


def test_an_unknown_symbol_is_a_clean_error(repo_root):
    result = _call("revl_source", {"files": [str(NOTES)], "symbol": "Nope"})
    assert result["ok"] is False
    assert "no top-level declaration matches" in result["diagnostics"][0]["message"]


def test_comment_free_rendering_compiles_to_the_same_program():
    source = ("// a doc comment\n"
              "service Cache { fn size() -> Int }  // trailing\n"
              "// above the component\n"
              "component C provides cache: Cache {\n"
              "  // inside\n"
              "  provide cache { fn size() = 0 }\n"
              "}\n")
    stripped = format_source(source, "c.rvl", comments=False)
    assert "//" not in stripped
    assert compile_source(stripped) == compile_source(source)


# ------------------------------------------------ editing by symbol


SERVICE = "service Tool { fn describe() -> Str }\n"
COMPONENT = ("// the operator's note about T\n"
             "component T provides tool: Tool {\n"
             '  provide tool { fn describe() = "one" }\n'
             "}\n")


@pytest.fixture
def composition(tmp_path, monkeypatch):
    paths = []
    for name, text in (("svc.rvl", SERVICE), ("main.rvl", COMPONENT)):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    try:
        yield paths
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old


@needs_runtime
def test_a_change_task_completes_with_no_whole_file_read(composition):
    """Load, read ONE symbol, replace it, call: no response carries a file."""
    _call("revl_load", {"files": composition})
    read = _call("revl_source", {"symbol": "T", "comments": False})
    assert read["ok"] is True and "service Tool" not in read["text"]
    new = read["text"].replace('"one"', '"two"')
    result = _call("revl_edit", {"edits": [{"symbol": "T", "replacement": new}]})
    assert result["ok"] is True and result["swapped"] is True, result
    assert result["applied"][0]["form"] == "symbol"
    # only one method's body changed, so that method is what is reported
    # (issue #1733), with the component it belongs to
    assert result["touched"] == [{"symbol": "T.tool.describe", "kind": "method",
                                  "parent": "T", "parentKind": "component",
                                  "buffer": composition[1], "change": "changed"}]
    assert _call("revl_call", {"key": "tool", "method": "describe"})["result"] == "two"
    # the disk is untouched, and the comment above T survives the replacement
    assert Path(composition[1]).read_text(encoding="utf-8") == COMPONENT
    held = _call("revl_source", {"symbol": "T"})["text"]
    assert held.startswith("// the operator's note about T") and '"two"' in held


@needs_runtime
def test_a_symbol_edit_that_breaks_a_guarantee_is_refused(composition):
    _call("revl_load", {"files": composition})
    refused = _call("revl_edit", {"edits": [{
        "symbol": "T", "replacement": "component T provides tool: Tool {\n"
                                      "  provide tool { fn describe() = 1 }\n}\n"}]})
    assert refused["ok"] is False and refused["swapped"] is False
    assert refused["diagnostics"][0]["code"] == "T1", refused
    assert _call("revl_call", {"key": "tool", "method": "describe"})["result"] == "one"


@needs_runtime
def test_a_swap_lists_the_symbols_it_touched(composition):
    _call("revl_load", {"source": SERVICE + COMPONENT})
    swapped = _call("revl_swap", {"source": SERVICE + COMPONENT.replace(
        "component T", "component T").replace('"one"', '"three"')})
    assert swapped["swapped"] is True, swapped
    assert swapped["touched"] == [{"symbol": "T.tool.describe", "kind": "method",
                                   "parent": "T", "parentKind": "component",
                                   "buffer": "source", "change": "changed"}]


def test_revl_source_is_advertised_read_only():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert tools["revl_source"]["annotations"]["readOnlyHint"] is True
    # `symbol` is optional since #2173: with nothing loaded and no `symbol`
    # the verb answers with the packaged stdlib's modules and symbols.
    assert tools["revl_source"]["inputSchema"]["required"] == []
    edit_items = tools["revl_edit"]["inputSchema"]["properties"]["edits"]["items"]
    assert "symbol" in edit_items["properties"]


def test_the_server_installs_its_hooks_on_the_shared_symbol_model():
    """The symbol model lives in `revl.symbols`, off the compile graph's
    `revl.mcp` (issue #1780); `revl.mcp.symbols` is the same module object with
    the server's canonical-form hook (issue #1700) and buffer resolver
    installed."""
    import revl.symbols as model
    import revl.mcp.symbols as served

    assert served is model
    assert model.CANONICALISE is served.canonical
    assert model.BUFFER_RESOLVER is not None
