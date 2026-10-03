"""Terse agent output, canonicalised on the server (issue #1700).

An agent pays output tokens, decode-bound, for every space, indent and line of
structure it writes, and none of it is a decision. The authoring verbs
(`revl_check`, `revl_load`, `revl_swap`, `revl_edit` and its hole fills) accept
source in any layout the parser reads, store it in the canonical layout
`revl fmt` computes, under the same IR-equivalence gate, and answer with a
digest of the canonical text instead of the text. `revl_edit` gains a
body-only form, ``{"method": "<key>.<op>", "body": ...}``: the agent writes the
body and the server writes the frame, from the service's declared signature
when the method is new.

The pure half runs everywhere. The live half needs the cordis-py runtime, like
tests/test_mcp_edit.py.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

from revl.compiler import compile_source  # noqa: E402
from revl.formatter import format_source  # noqa: E402
from revl.mcp import canonical  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

# what an agent writes when it spends nothing on layout
TERSE = ("service Cache { fn get(key: Str) -> Opt[Str]\n fn size() -> Int\n"
         " fn count(n: Int, m: Int) -> Int }\n"
         "component MemCache provides cache: Cache {\n"
         "let store = effect Map.new() undo store.drop()\n"
         "provide cache { fn get(key) = store.get(key)\n"
         "fn size() = 0\n"
         "fn count(n, m) = n+m }\n}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ------------------------------------------------------------ canonicalise

def test_terse_source_is_stored_in_the_formatter_s_canonical_layout():
    canon = canonical.canonicalise(TERSE)
    assert canon.changed is True
    assert canon.text == format_source(TERSE)
    assert canon.text != TERSE
    # and nothing the compiler sees moved
    assert compile_source(canon.text, "a.rvl") == compile_source(TERSE, "a.rvl")


def test_canonical_source_comes_back_unchanged():
    canon = canonical.canonicalise(format_source(TERSE))
    assert canon.changed is False


def test_an_unformattable_source_is_kept_as_written():
    """Canonicalising is a convenience: a text the formatter cannot scan is
    kept, so the compile reports what it would have reported."""
    text = "component C { let x = 1 \x01 }"
    canon = canonical.canonicalise(text)
    assert canon.text == text and canon.changed is False
    assert canon.reason.startswith("not formatted")


def test_the_report_is_a_digest_unless_the_text_is_asked_for():
    canon = canonical.canonicalise(TERSE)
    assert set(canon.report()) == {"digest", "changed"}
    assert canon.report()["digest"] == canonical.digest(canon.text)
    assert canon.report(with_text=True)["source"] == canon.text


# ------------------------------------------------------------ body-only edits

def test_a_body_edit_replaces_only_the_body_of_an_existing_method():
    text, echo = canonical.method_body_edit(
        TERSE, {"method": "cache.size", "body": "42"})
    assert "fn size() = 42" in text
    assert echo == {"form": "method", "method": "cache.size", "frame": "kept",
                    "replaced": "= 0"}


def test_a_block_body_replaces_an_expression_body():
    text, _ = canonical.method_body_edit(
        TERSE, {"method": "cache.count", "body": "{ let s = n + m\nreturn s }"})
    assert "fn count(n, m) { let s = n + m" in text
    assert compile_source(text, "a.rvl")


def test_a_body_edit_against_a_declared_signature_compiles():
    """The exit test: the provide block has no `count` yet. The agent sends
    only the body; the server writes `fn count(n, m)` from the service's
    declared `count(n: Int, m: Int) -> Int`."""
    without = TERSE.replace("\nfn count(n, m) = n+m }", " }")
    assert "fn count(n, m)" not in without
    text, echo = canonical.method_body_edit(
        without, {"method": "cache.count", "body": "n * m"})
    assert echo["frame"] == "written" and echo["signature"] == "fn count(n, m)"
    assert "fn count(n, m) = n * m" in text
    assert compile_source(canonical.canonicalise(text).text, "a.rvl")


def test_a_body_edit_names_the_component_when_a_key_is_provided_twice():
    two = TERSE + ("component OtherCache provides cache: Cache {\n"
                   "provide cache { fn get(key) = None\nfn size() = 0\n"
                   "fn count(n, m) = 0 }\n}\n")
    with pytest.raises(canonical.BodyEditError, match="name the component"):
        canonical.method_body_edit(two, {"method": "cache.size", "body": "1"})
    text, _ = canonical.method_body_edit(
        two, {"method": "OtherCache.cache.size", "body": "1"})
    assert text.count("fn size() = 1") == 1


@pytest.mark.parametrize("edit, message", [
    ({"method": "nope.size", "body": "1"}, "no `provide nope"),
    ({"method": "cache.missing", "body": "1"}, "declares no operation `missing`"),
    ({"method": "size", "body": "1"}, "`<key>.<op>`"),
    ({"method": "cache.size", "body": "  "}, "non-empty `body`"),
])
def test_a_body_edit_that_cannot_be_placed_is_a_clean_error(edit, message):
    with pytest.raises(canonical.BodyEditError, match=message):
        canonical.method_body_edit(TERSE, edit)


# ------------------------------------------------------------ the measurement

def test_a_reference_change_costs_fewer_output_tokens_body_only():
    """The before and after of the issue's measurement, with the bench's own
    deterministic token proxy (bench/tokens.py `count_tokens`): changing one
    method's body by resending the canonical file (what `revl_swap` needed),
    by an anchor edit, and by the body-only form."""
    import json

    from tokens import count_tokens  # noqa: PLC0415

    canonical_file = format_source(TERSE)
    changed = canonical_file.replace("fn size() = 0", "fn size() = store.size()")
    full = count_tokens(json.dumps({"source": changed}))
    anchor = count_tokens(json.dumps({"edits": [
        {"anchor": "fn size() = 0", "replacement": "fn size() = store.size()"}]}))
    body = count_tokens(json.dumps({"edits": [
        {"method": "cache.size", "body": "store.size()"}]}))
    assert body < anchor < full
    # written down in docs/mcp-bridge.md; a drift here means the doc is stale
    assert (full, anchor, body) == MEASURED


MEASURED = (132, 34, 26)


# ------------------------------------------------------------ live (runtime)

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime")


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod

    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


def test_check_answers_with_the_digest_of_the_canonical_text():
    result = _call("revl_check", {"source": TERSE})
    assert result["ok"] is True
    assert result["canonical"] == {
        "digest": canonical.digest(format_source(TERSE)), "changed": True}


def test_check_returns_the_canonical_text_only_when_asked():
    result = _call("revl_check", {"source": TERSE, "returnCanonical": True})
    assert result["canonical"]["source"] == format_source(TERSE)


def test_a_refused_terse_candidate_reports_the_line_the_agent_wrote():
    """The formatter is line-preserving, so a diagnostic's line is a line of
    the text the agent sent, not of a layout it never saw."""
    bad = TERSE.replace("fn size() = 0", 'fn size() = "nope"')
    result = _call("revl_check", {"source": bad})
    assert result["ok"] is False
    line = result["diagnostics"][0]["line"]
    assert 'fn size() = "nope"' in bad.splitlines()[line - 1]


@needs_runtime
def test_a_terse_load_is_admitted_and_stored_canonically():
    """The exit test: a terse but parseable composition is admitted and the
    server holds it in canonical layout."""
    from revl.mcp import edit as edit_mod  # noqa: PLC0415
    from revl.mcp import server as server_mod  # noqa: PLC0415

    result = _call("revl_load", {"source": TERSE})
    assert result["ok"] is True and result["canonical"]["changed"] is True
    held = edit_mod.virtual_source(server_mod.SESSION)["source"]
    assert held == format_source(TERSE)
    assert _call("revl_call", {"key": "cache", "method": "count",
                               "args": [2, 3]})["result"] == 5


@needs_runtime
def test_a_body_only_edit_swaps_into_the_running_composition():
    _call("revl_load", {"source": TERSE})
    result = _call("revl_edit", {"edits": [
        {"method": "cache.count", "body": "{ let s = n * m\nreturn s }"}]})
    assert result["ok"] is True and result["swapped"] is True
    assert result["applied"][0]["frame"] == "kept"
    assert result["canonical"]["changed"] is True
    assert _call("revl_call", {"key": "cache", "method": "count",
                               "args": [2, 3]})["result"] == 6


@needs_runtime
def test_a_body_only_edit_writes_the_frame_of_a_new_method():
    _call("revl_load", {"source": TERSE.replace(
        "\nfn count(n, m) = n+m }", "\nfn count(n, m) = 0 }")})
    from revl.mcp import edit as edit_mod  # noqa: PLC0415
    from revl.mcp import server as server_mod  # noqa: PLC0415

    # drop the method from the held source, so the edit has to write it
    held = edit_mod.virtual_source(server_mod.SESSION)
    server_mod.SESSION.draft = {**held, "source": held["source"].replace(
        "\n    fn count(n, m) = 0", "")}
    result = _call("revl_edit", {"edits": [
        {"method": "cache.count", "body": "n - m"}]})
    assert result["ok"] is True and result["swapped"] is True, result
    assert result["applied"][0]["signature"] == "fn count(n, m)"
    assert _call("revl_call", {"key": "cache", "method": "count",
                               "args": [5, 3]})["result"] == 2


def test_the_authoring_verbs_advertise_return_canonical():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    for name in ("revl_check", "revl_load", "revl_swap", "revl_edit"):
        assert "returnCanonical" in tools[name]["inputSchema"]["properties"], name
    items = tools["revl_edit"]["inputSchema"]["properties"]["edits"]["items"]
    assert {"method", "body"} <= set(items["properties"])
