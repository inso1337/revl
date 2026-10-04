"""Issue #1700: agents write the decision, the server writes the frame.

Output tokens are the expensive ones, so the edit path takes terse text and
stores the canonical form, and a method or fn can be changed by its BODY only:

* a terse but parseable edit (a member replacement, an `{append}`, a body) is
  admitted and stored as `revl fmt` writes it, and the echo carries the
  canonical text when it differs from what was sent;
* `{symbol, body}` against a declared signature compiles: the header (name,
  parameters, return type) is the server's, from the declaration;
* the reference change's payload, measured before and after.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the edit verbs boot a composition and need the cordis-py runtime "
           "- install it with `sh backends/python/setup.sh`")

SOURCE = ("service Clock {\n"
          "  fn now() -> Int\n"
          "  fn later(n: Int) -> Int\n"
          "}\n"
          "\n"
          "fn double(x: Int) -> Int {\n"
          "  return x * 2\n"
          "}\n"
          "\n"
          "component FixedClock provides clock: Clock {\n"
          "  provide clock {\n"
          "    fn now() = 7\n"
          "    fn later(n) = double(n)\n"
          "  }\n"
          "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _held() -> str:
    return server_mod.SESSION.origin["source"]


def _edit(edit: dict) -> dict:
    return _call("revl_edit", {"edits": [edit]})


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def loaded():
    assert _call("revl_load", {"source": SOURCE})["ok"] is True


def _now() -> int:
    return _call("revl_call", {"key": "clock", "method": "now"})["result"]


def _later(n: int) -> int:
    return _call("revl_call", {"key": "clock", "method": "later",
                               "args": [n]})["result"]


# ------------------------------------------------ terse text, stored canonically


@needs_runtime
def test_a_terse_member_replacement_is_admitted_and_stored_canonically(loaded):
    result = _edit({"symbol": "FixedClock.clock.now", "replacement": "fn now()=4+5"})
    assert result["ok"] is True and result["swapped"] is True, result
    assert _now() == 9
    assert "    fn now() = 4 + 5\n" in _held()
    assert result["applied"][0]["canonical"] == "fn now() = 4 + 5"


@needs_runtime
def test_a_terse_append_is_stored_canonically(loaded):
    result = _call("revl_change", {"commit": True, "add": {
        "source": "fn triple(x:Int)->Int=x*3"}})
    assert result["committed"] is True, result
    assert "fn triple(x: Int) -> Int = x * 3\n" in _held()


# ------------------------------------------------ the body only


@needs_runtime
def test_a_body_only_edit_against_the_declared_signature_compiles(loaded):
    result = _edit({"symbol": "FixedClock.clock.now", "body": "40+2"})
    assert result["ok"] is True and result["swapped"] is True, result
    assert _now() == 42
    echo = result["applied"][0]
    assert echo["form"] == "body" and echo["header"] == "fn now()"
    assert "    fn now() = 40 + 2\n" in _held()


@needs_runtime
def test_a_statement_body_becomes_a_block(loaded):
    result = _edit({"symbol": "FixedClock.clock.later",
                    "body": "let k=double(n)\nreturn k+1"})
    assert result["swapped"] is True, result
    assert _later(3) == 7
    assert "    fn later(n) {\n      let k = double(n)\n      return k + 1\n    }\n" \
        in _held()


@needs_runtime
def test_a_top_level_fn_keeps_its_typed_header(loaded):
    result = _edit({"symbol": "double", "body": "x*10"})
    assert result["swapped"] is True, result
    assert _later(3) == 30
    assert "fn double(x: Int) -> Int = x * 10\n" in _held()


@needs_runtime
def test_a_body_through_revl_change_is_one_call(loaded):
    result = _call("revl_change", {"commit": True, "edit": {"edits": [
        {"symbol": "FixedClock.clock.now", "body": "1"}]}})
    assert result["committed"] is True, result
    assert _now() == 1


@needs_runtime
def test_a_body_for_something_that_is_not_a_fn_is_refused(loaded):
    result = _edit({"symbol": "FixedClock", "body": "1"})
    assert result["ok"] is False and result.get("swapped") is not True
    assert "replaces the body of a method or fn, and `FixedClock` is a component" \
        in result["diagnostics"][0]["message"]
    assert _now() == 7


@needs_runtime
def test_a_body_that_does_not_compile_changes_nothing(loaded):
    """The body reached the compile (the header was framed around it) and the
    compile refused it against the declared signature."""
    result = _edit({"symbol": "FixedClock.clock.now", "body": "\"seven\""})
    assert result["ok"] is False and result.get("swapped") is not True, result
    diagnostic = result["diagnostics"][0]
    assert diagnostic["category"] == "type-mismatch", diagnostic
    assert "expects `Int`, got `Str`" in diagnostic["message"]
    assert _now() == 7
    assert "    fn now() = 7\n" in _held()


# ------------------------------------------------ the measurement


def test_the_reference_change_costs_a_fraction_of_the_output():
    """The issue's measurement (static: the payload sizes, not a behaviour).
    One method body changed in examples/user_cache.rvl, written four ways.
    Sizes are the JSON arguments an agent emits, in bytes (no tokenizer is
    assumed; output tokens track bytes). Before #1700 the smallest form was the
    member replacement; the body-only form drops the frame as well."""
    src = (ROOT / "examples" / "user_cache.rvl").read_text(encoding="utf-8")
    old, new = "fn get(key) = store.get(key)", 'fn get(key) = store.get("u:" + key)'
    assert old in src
    changed = src.replace(old, new)
    component = changed[changed.index("component UserCache"):]

    def size(arguments: dict) -> int:
        return len(json.dumps(arguments, separators=(",", ":")))

    whole_file = size({"source": changed})
    whole_component = size({"commit": True, "replace": {
        "component": "UserCache", "source": component}})
    member = size({"edits": [{"symbol": "UserCache.cache.get", "replacement": new}]})
    body = size({"edits": [{"symbol": "UserCache.cache.get",
                            "body": 'store.get("u:"+key)'}]})
    assert body < member < whole_component < whole_file
    assert body * 5 < whole_component and body * 15 < whole_file
