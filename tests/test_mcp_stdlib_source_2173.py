"""`revl_source` can enumerate the packaged stdlib (issue #2173).

`stdlib/` ships in the wheel — 20 modules — but nothing on the MCP surface
could name one of them: `revl_source {}` refused with "`symbol` is required"
and `revl_source {symbol: 'concat'}` with "nothing is loaded", because the
stdlib is not a loaded buffer. The only declaration reader therefore required
a symbol name the agent had to already know, and the vocabulary — the modules
and their exported names, plus the base type surface — was unreachable. Over
45 measured authoring reps, 9 searched for `stdlib`, 28 for `syntax`, and 5 of
the 6 deep spirals were exactly that search.

These tests hold the issue's acceptance:

* with no `symbol` and nothing else asked for, `revl_source` answers with the
  stdlib's modules and their exported names, and says they are packaged;
* a stdlib symbol (`Str.concat`, `list_sort`) resolves against the packaged
  `stdlib/` even with no user buffer loaded;
* the index and the reader cannot disagree: every module the index lists is
  readable through the same handler;
* `revl_source` keeps its current behaviour and error for a missing symbol
  **when other arguments are given**, and stays in `CORE` — this is a branch
  in an existing handler, not a new tier entry, so the cap of 14 that
  #2073/#2042 protect is untouched.

The first test is the falsifier: it fails if the stdlib branch is disabled.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import _paths  # noqa: E402
from revl.mcp import disclosure  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")

#: The refusal `revl_source` has always given when a `symbol` is missing.
SYMBOL_REQUIRED = "`symbol` is required"
#: The refusal a session that holds nothing gives for a name it cannot reach.
NOTHING_LOADED = "nothing is loaded"
CONTENT_ERROR = "no top-level declaration matches"

GOOD = """service Greeter {
  fn greet(name: Str) -> Str
}

component OnlyOne provides greeter: Greeter {
  provide greeter {
    fn greet(name: Str) -> Str = name
  }
}
"""


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _message(result: dict) -> str:
    return " ".join(str(d.get("message", ""))
                    for d in result.get("diagnostics") or [])


@pytest.fixture
def nothing_loaded():
    """A session holding nothing — the state the issue measured in."""
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


@pytest.fixture
def repo_root():
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(ROOT),))
    try:
        yield
    finally:
        server_mod.AUTHORING = old
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()


def _modules() -> set:
    """The modules on disk, by the same root the enumeration reads."""
    return {path.stem for path in _paths.stdlib_root().glob("*.rvl")}


def test_no_symbol_answers_with_the_packaged_stdlib(nothing_loaded):
    """FALSIFIER. `revl_source {}` — the call the issue measured — answers
    with the stdlib's modules, their exported names, and that they are
    packaged. With the stdlib branch disabled this call refuses with
    "`symbol` is required" and this test is red."""
    result = _call("revl_source", {})

    assert result["ok"] is True, result
    assert result["kind"] == "stdlib", result
    assert result["packaged"] is True, result
    assert Path(result["root"]) == _paths.stdlib_root(), result
    modules = {m["module"]: m for m in result["modules"]}
    assert set(modules) == _modules(), sorted(set(modules) ^ _modules())
    assert modules["str"]["path"] == "stdlib/str.rvl", modules["str"]
    assert modules["str"]["packaged"] is True, modules["str"]
    assert modules["str"]["symbols"]["trim"] == "fn", modules["str"]["symbols"]
    assert modules["list"]["symbols"]["list_sort"] == "fn"
    assert modules["auth"]["symbols"]["Auth"] == "service"
    # The base type surface is built into the language and declared by no
    # file, so it is indexed under its own buffer.
    assert result["builtin"]["buffer"] == "builtin", result["builtin"]
    assert result["builtin"]["symbols"]["Str.concat"] == "fn concat(a0: Str) -> Str"


def test_the_index_says_what_each_export_is(nothing_loaded):
    """The names alone are not the vocabulary: a caller has to be able to tell
    a service from a function before it can use either."""
    result = _call("revl_source", {})

    kinds = {kind for module in result["modules"]
             for kind in module["symbols"].values()}
    assert {"fn", "type", "service", "component", "extern"} <= kinds, kinds
    assert all(module["symbols"] for module in result["modules"]), result


def test_the_index_and_the_reader_cannot_disagree(nothing_loaded):
    """Every module the index lists is readable through the same handler, by
    the name the index gives: a listed name that `revl_source` then refuses
    would send the caller straight back to the search this closes."""
    result = _call("revl_source", {})
    assert len(result["modules"]) == 20, len(result["modules"])

    for module in result["modules"]:
        name = next(iter(module["symbols"]))
        read = _call("revl_source", {"symbol": f"{module['module']}.rvl:{name}"})
        assert read["ok"] is True, (module["module"], name, read)
        assert read["symbol"] == name, read
        assert read["kind"] == module["symbols"][name], read


@pytest.mark.parametrize("symbol", ["list_sort", "str.rvl:trim", "Str.concat",
                                    "List.push", "Map.keys", "Int.div_trunc",
                                    "builtin:Str.length"])
def test_a_stdlib_symbol_resolves_with_nothing_loaded(nothing_loaded, symbol):
    """Acceptance 2: a stdlib symbol resolves against the packaged `stdlib/`
    with no user buffer loaded — a bare name, a `<module>.rvl:Name`, or a base
    type's `<Type>.<method>`."""
    result = _call("revl_source", {"symbol": symbol})

    assert result["ok"] is True, result
    assert result["text"].strip(), result
    assert result["buffer"].endswith(".rvl") or result["buffer"] == "builtin", \
        result["buffer"]


def test_an_unknown_name_is_refused_by_the_stdlib_not_by_the_session(nothing_loaded):
    """The old answer — "nothing is loaded: load a composition, or pass
    `files` or `source`" — leaves the caller where it started. The refusal
    names what the stdlib IS and how to see all of it."""
    result = _call("revl_source", {"symbol": "no_such_declaration"})

    assert result["ok"] is False, result
    assert CONTENT_ERROR in _message(result), result
    assert str(_paths.stdlib_root()) in _message(result), result
    assert "20 modules" in _message(result), result
    assert NOTHING_LOADED not in _message(result), result


def test_an_ambiguous_stdlib_name_is_still_ambiguous(nothing_loaded):
    """`Rendered` is declared by two stdlib modules; the index lists both, so
    the name stays ambiguous and the refusal says how to qualify it."""
    result = _call("revl_source", {"symbol": "Rendered"})

    assert result["ok"] is False, result
    assert "names more than one declaration" in _message(result), result
    assert "qualify it" in _message(result), result
    qualified = _call("revl_source", {"symbol": "template.rvl:Rendered"})
    assert qualified["ok"] is True, qualified


def test_a_module_that_does_not_exist_is_refused_by_buffer_name(nothing_loaded):
    """A mistyped module is a buffer that does not exist, not a declaration
    that does not exist — the two are different fixes."""
    result = _call("revl_source", {"symbol": "nosuch.rvl:trim"})

    assert result["ok"] is False, result
    assert "no server-side source buffer named" in _message(result), result


@pytest.mark.parametrize("arguments", [
    {"source": GOOD},
    {"modules": {"m": GOOD}},
    {"proposal": True},
    {"with": ["deps"]},
    {"comments": False},
])
def test_the_old_refusal_stands_when_other_arguments_are_given(nothing_loaded,
                                                               arguments):
    """Acceptance 3: a caller that names a source, a modifier or a proposal and
    no `symbol` gets the error it has always had, so existing callers are
    unaffected — the catalogue is not handed to a call that did not ask for
    it."""
    result = _call("revl_source", arguments)

    assert result["ok"] is False, result
    assert SYMBOL_REQUIRED in _message(result), result


def test_a_symbol_is_still_required_beside_a_named_source(repo_root, tmp_path):
    """The same rule with `files`: naming files is not a way to ask for the
    index."""
    good = tmp_path / "good.rvl"
    good.write_text(GOOD, encoding="utf-8")
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    try:
        result = _call("revl_source", {"files": [str(good)]})
        assert result["ok"] is False, result
        assert SYMBOL_REQUIRED in _message(result), result
    finally:
        server_mod.AUTHORING = old


def test_a_named_source_still_wins_over_the_stdlib(repo_root, tmp_path):
    """The stdlib branch is for a call that names no source. A `files` call
    keeps reading its own buffer, so a stdlib-only name is a content error in
    that file — never silently resolved against the stdlib."""
    good = tmp_path / "good.rvl"
    good.write_text(GOOD, encoding="utf-8")
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    try:
        read = _call("revl_source", {"symbol": "OnlyOne", "files": [str(good)]})
        assert read["ok"] is True, read
        assert read["kind"] == "component", read

        shadowed = _call("revl_source", {"symbol": "list_sort",
                                         "files": [str(good)]})
        assert shadowed["ok"] is False, shadowed
        assert CONTENT_ERROR in _message(shadowed), shadowed
        assert str(_paths.stdlib_root()) not in _message(shadowed), shadowed
    finally:
        server_mod.AUTHORING = old


def test_the_stdlib_branch_needs_a_session_that_holds_nothing(monkeypatch):
    """A loaded composition is read from its own working set: the stdlib is
    reached there the way it always was, through a `use`. The branch that
    resolves a `symbol` against the packaged stdlib is for a session that
    holds nothing."""
    monkeypatch.setattr(server_mod.SESSION, "_driver", object(), raising=False)

    assert server_mod.SESSION.loaded is True
    assert server_mod._stdlib_only({}) is False
    assert server_mod._stdlib_only({"files": ["a.rvl"]}) is False
    # ...while the index still answers, because a bare call asks for nothing
    # else and the stdlib is the only vocabulary there is.
    assert server_mod._bare_call({}) is True
    assert server_mod._bare_call({"symbol": None}) is True
    assert server_mod._bare_call({"files": ["a.rvl"]}) is False
    assert server_mod._bare_call({"proposal": True}) is False
    assert server_mod._bare_call({"comments": False}) is False


@needs_runtime
def test_a_loaded_composition_is_read_from_its_own_source(repo_root):
    """End to end, with a composition actually running: the session's own
    declarations resolve as they always did, and a bare call still answers
    with the stdlib index."""
    loaded = _call("revl_load", {"source": GOOD})
    assert loaded["ok"] is True, loaded

    own = _call("revl_source", {"symbol": "OnlyOne"})
    assert own["ok"] is True, own
    assert own["buffer"] == "source", own
    assert own["kind"] == "component", own

    index = _call("revl_source", {})
    assert index["ok"] is True, index
    assert index["kind"] == "stdlib", index


def test_revl_source_stays_in_the_core_tier(nothing_loaded):
    """Acceptance 3: `revl_source` is already in `CORE` and already means
    "read a declaration", so the stdlib index is a branch in an existing
    handler rather than a new tier entry — the cap of 14 that #2073/#2042
    protect is untouched."""
    assert "revl_source" in disclosure.CORE
    assert len(disclosure.CORE) == 14, disclosure.CORE
    assert disclosure.CORE[-1] == disclosure.DISCOVERY

    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert "revl_source" in tools, sorted(tools)
    assert tools["revl_source"]["annotations"]["readOnlyHint"] is True
    assert tools["revl_source"]["inputSchema"]["required"] == []
    assert "PACKAGED STDLIB" in tools["revl_source"]["description"]
