"""Issue #1700, the last two clauses.

* Completion: the MCP authoring verbs accept newline-separated match arms and
  an `if` without parentheses, by inserting what the parser lacks (inside the
  line, so diagnostics keep their line numbers), only when that makes the text
  parse, and never when the insertion is ambiguous. The language does not
  change: `revl compile` still refuses both forms.
* The server writes a component: `revl_change {add: {component, provide,
  methods}}` takes the provided service, the method frames and the `requires`
  clause from the composition, and refuses whatever it would have to guess.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.complete import complete  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session verbs boot a composition and need the cordis-py runtime "
           "- install it with `sh backends/python/setup.sh`")

ARMS = ("type T = A | B | C(Int)\n"
        "service S { fn f(t: T) -> Int }\n"
        "component Q provides s: S {\n"
        "  provide s {\n"
        "    fn f(t) = match t {\n"
        "      A => 1\n"
        "      B => 2\n"
        "      C(n) => n + 1\n"
        "    }\n"
        "  }\n"
        "}\n")

IF = ("fn g(x: Int) -> Int {\n"
      "  if x > 1 { return 1 } else if x < 0 { return 2 }\n"
      "  return 0\n"
      "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    server_mod.set_runtime_available(True)
    yield
    server_mod.set_runtime_available(None)
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old
    _call("revl_change", {"discard": True})


# ------------------------------------------------ completion


@pytest.mark.parametrize("terse", [ARMS, IF], ids=["match arms", "if"])
def test_the_language_is_unchanged(terse):
    with pytest.raises(RevlError):
        compile_source(terse)


def test_newline_separated_match_arms_are_completed():
    result = _call("revl_check", {"source": ARMS})
    assert result["ok"] is True, result
    report = result["canonicalSource"]
    assert report["changed"] is True
    assert report["completed"] == [{"line": 6, "inserted": ","},
                                   {"line": 7, "inserted": ","}]


def test_an_if_without_parentheses_is_completed():
    result = _call("revl_check", {"source": IF})
    assert result["ok"] is True, result
    inserted = [c["inserted"] for c in result["canonicalSource"]["completed"]]
    assert inserted == ["(", ")", "(", ")"]
    text, _ = complete(IF)
    assert "if (x > 1) { return 1 } else if (x < 0) { return 2 }" in text


def test_an_ambiguous_condition_is_left_as_written():
    """The first `{` after this `if` opens a record literal, not the block:
    wrapping up to it does not parse, so nothing is inserted and the agent's
    own parse error comes back."""
    record = ("type R = { a: Int }\n"
              "fn g(r: R) -> Int {\n  if r == { a: 1 } { return 1 }\n  return 0\n}\n")
    assert complete(record) == (record, [])
    result = _call("revl_check", {"source": record})
    assert result["ok"] is False
    assert "completed" not in result["canonicalSource"]


def test_a_continued_expression_is_not_split_into_arms():
    """A line without `=>` at the arm's depth continues the arm before it."""
    continued = ARMS.replace("      C(n) => n + 1\n", "      C(n) => n +\n        1\n")
    text, inserted = complete(continued)
    assert [c["line"] for c in inserted] == [6, 7]
    assert "C(n) => n +\n        1\n" in text


def test_text_that_parses_is_never_completed():
    canonical = compile_source(complete(ARMS)[0]) and complete(ARMS)[0]
    assert complete(canonical) == (canonical, [])


def test_a_completed_source_keeps_its_line_numbers():
    """Every insertion is inside a line, so a diagnostic on the terse source
    names the same line as on the hand-completed one."""
    broken = ARMS.replace("      B => 2\n", '      B => "two"\n')
    terse = _call("revl_check", {"source": broken})
    by_hand = _call("revl_check", {"source": complete(broken)[0]})
    assert terse["ok"] is False and terse["canonicalSource"]["completed"]
    assert terse["diagnostics"][0]["line"] == by_hand["diagnostics"][0]["line"]
    assert terse["diagnostics"][0]["message"] == by_hand["diagnostics"][0]["message"]


@needs_runtime
def test_a_terse_swap_is_completed_and_stored_canonical():
    assert _call("revl_load", {"source": complete(ARMS)[0]})["ok"] is True
    changed = ARMS.replace("      A => 1\n", "      A => 10\n")
    swapped = _call("revl_swap", {"source": changed})
    assert swapped["swapped"] is True, swapped
    assert swapped["canonicalSource"]["completed"]
    held = server_mod.SESSION.origin["source"]
    assert "      A => 10,\n      B => 2,\n" in held


@needs_runtime
def test_a_body_edit_with_newline_separated_arms_is_completed():
    assert _call("revl_load", {"source": complete(ARMS)[0]})["ok"] is True
    edited = _call("revl_edit", {"edits": [{"symbol": "Q.s.f", "body":
                                            "match t {\n  A => 7\n  B => 8\n  C(n) => n\n}"}]})
    assert edited["swapped"] is True, edited
    forms = [e["form"] for e in edited["applied"]]
    assert forms == ["body", "completed"]
    assert "A => 7," in server_mod.SESSION.origin["source"]


# ------------------------------------------------ the server writes a component


BASE = ("service Store {\n  fn get_count() -> Int\n}\n"
        "service Clock {\n  fn now() -> Int\n  fn later(n: Int) -> Int\n}\n\n"
        "component Mem provides store: Store {\n"
        "  provide store {\n    fn get_count() = 41\n  }\n}\n")

TICKER = {"component": "Ticker", "provide": {"key": "clock", "service": "Clock"},
          "methods": {"now": "store.get_count() + 1", "later": "n * 2"}}


@needs_runtime
def test_a_component_from_its_provide_and_bodies_compiles_with_requires_inferred():
    assert _call("revl_load", {"source": BASE})["ok"] is True
    result = _call("revl_change", {"commit": True, "add": TICKER})
    assert result["committed"] is True, result
    assert result["components"] == [{"component": "Ticker", "change": "added"}]
    held = server_mod.SESSION.origin["source"]
    assert "component Ticker requires store: Store provides clock: Clock {" in held
    assert "    fn later(n) = n * 2\n" in held
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 42
    assert _call("revl_call", {"key": "clock", "method": "later", "args": [4]})["result"] == 8


def test_the_provided_service_is_inferred_from_the_key():
    from revl.mcp import change
    source = change.component_source({"source": BASE, "modules": {}}, {
        "component": "Mem2", "provide": "store", "methods": {"get_count": "7"}})
    assert source.startswith("component Mem2 provides store: Store {\n")
    compile_source(BASE + "\n" + source.replace("Mem2 provides store", "Mem2 provides st2")
                   .replace("provide store", "provide st2"))


def test_a_component_that_is_not_a_name_names_add_component():
    """Issue #2219: the old hint read as the shape of `component`, so agents
    nested `component` inside `component` and retried byte-identically. The
    refusal must name the failing key and the type it needs."""
    from revl.mcp import change
    from revl.mcp.change import ChangeError
    with pytest.raises(ChangeError) as excinfo:
        change.component_source({"source": BASE, "modules": {}}, {
            "component": {"component": "Ticker", "provide": "clock",
                          "methods": {"now": "1", "later": "2"}},
            "provide": "clock", "methods": {"now": "1", "later": "2"}})
    message = str(excinfo.value)
    assert "`add.component`" in message and "string" in message, message
    assert "siblings" in message, message


def test_methods_as_a_list_names_add_methods_and_wants_an_object():
    """Issue #2219: a `methods` list must be refused naming `add.methods` and
    the object shape, not the whole `add` shape."""
    from revl.mcp import change
    from revl.mcp.change import ChangeError
    with pytest.raises(ChangeError) as excinfo:
        change.component_source({"source": BASE, "modules": {}}, {
            "component": "Ticker", "provide": "clock",
            "methods": ["fn now() = 1", "fn later(n) = n * 2"]})
    message = str(excinfo.value)
    assert "`add.methods`" in message and "object" in message, message
    assert "list" in message, message


@pytest.mark.parametrize("change,needle", [
    ({"methods": {"now": "ghost.get() + 1", "later": "n"}},
     "`ghost` is not a key of this composition"),
    ({"methods": {"now": "1"}}, "no body for later"),
    ({"methods": {"now": "1", "later": "n", "soon": "2"}}, "no operation soon"),
    ({"methods": {"now": "config.step", "later": "n"}}, "config is not inferred"),
    ({"provide": "nowhere"}, "the composition does not know `nowhere` yet"),
])
@needs_runtime
def test_what_the_server_would_have_to_guess_is_refused(change, needle):
    assert _call("revl_load", {"source": BASE})["ok"] is True
    result = _call("revl_change", {"add": {**TICKER, **change}})
    assert result["ok"] is False
    assert needle in result["diagnostics"][0]["message"], result["diagnostics"]


@needs_runtime
def test_config_is_taken_when_given():
    assert _call("revl_load", {"source": BASE})["ok"] is True
    result = _call("revl_change", {"commit": True, "add": {
        **TICKER, "config": "{ step: Int = 3 }",
        "methods": {"now": "config.step", "later": "n"}}})
    assert result["committed"] is True, result
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 3


def test_the_header_form_costs_less_output_than_the_whole_source():
    """The issue's measurement: the same component as `{add: {source}}` and as
    `{add: {component, provide, methods}}`, in JSON argument bytes."""
    whole = ("component Ticker requires store: Store provides clock: Clock {\n"
             "  provide clock {\n"
             "    fn now() = store.get_count() + 1\n"
             "    fn later(n) = n * 2\n"
             "  }\n"
             "}\n")

    def size(arguments: dict) -> int:
        return len(json.dumps(arguments, separators=(",", ":")))

    source_form = size({"add": {"source": whole}})
    header_form = size({"add": {"component": "Ticker", "provide": "clock",
                                "methods": TICKER["methods"]}})
    assert header_form < source_form
