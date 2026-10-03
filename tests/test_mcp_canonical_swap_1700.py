"""Issue #1700, second slice: whole sources are stored canonical, and every
provide member gets its own line.

* `revl_check` and `revl_swap` compile what the agent SENT (so a
  diagnostic names a line it wrote) and store the canonical form, which the
  formatter's IR-equivalence gate proved compiles identically. They answer
  `canonicalSource: {changed, digest}`, with the text on `returnCanonical`.
  Text the formatter cannot read passes through as written.
* `revl fmt` puts each member of a `provide` block on its own line, so every
  method is addressable by symbol; on the few programs where that would move an
  IR line field, it keeps the line-preserving layout instead.
* `{symbol: "Comp.key.op", body}` writes a NEW method's frame from the service.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.formatter import format_admitted, format_source  # noqa: E402
from revl.mcp import canonical  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session verbs boot a composition and need the cordis-py runtime "
           "- install it with `sh backends/python/setup.sh`")

#: terse, one-line provide blocks: what an agent writes to save output
TERSE = ("service Clock { fn now() -> Int\n fn later(n: Int) -> Int }\n"
         "component FixedClock provides clock: Clock {\n"
         "  provide clock { fn now()=7 fn later(n)=n+1 }\n"
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
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old


# ------------------------------------------------ the formatter split


def test_a_one_line_provide_gets_one_member_per_line():
    out = format_source(TERSE)
    assert "  provide clock {\n    fn now() = 7\n    fn later(n) = n + 1\n  }\n" in out
    assert format_source(out) == out                      # idempotent
    text, gate = format_admitted(TERSE)
    assert text == out and gate.admitted


def test_the_split_falls_back_where_it_would_move_an_ir_line():
    """A `spawn` acquire carries its source line in the IR, so splitting a
    provide above it changes the IR; the admitted layout is then the
    line-preserving one, which the gate admits."""
    path = ROOT / "tests" / "fixtures" / "emit_ts_corpus" / "spawn.rvl"
    source = path.read_text(encoding="utf-8")
    split = format_source(source, str(path))
    assert split != format_source(source, str(path), split_members=False)
    text, gate = format_admitted(source, str(path))
    assert gate.admitted
    assert text == format_source(source, str(path), split_members=False)


EXAMPLES = sorted((ROOT / "examples").rglob("*.rvl"))


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.name for p in EXAMPLES])
def test_the_admitted_layout_differs_from_before_only_by_the_split(path):
    source = path.read_text(encoding="utf-8")
    try:
        before = format_source(source, str(path), split_members=False)
    except Exception:
        pytest.skip("not scannable")
    text, gate = format_admitted(source, str(path))
    assert gate.admitted, gate.reason
    assert format_admitted(text, str(path))[0] == text          # idempotent
    squash = str.maketrans("", "", " \n")
    assert text.translate(squash) == before.translate(squash)   # whitespace only
    if text != before:
        assert any(line.strip().startswith("provide ") and "fn " in line
                   for line in before.splitlines()), "changed without a one-line provide"


def test_the_split_reaches_the_examples_that_have_a_one_line_provide():
    split = []
    for path in EXAMPLES:
        source = path.read_text(encoding="utf-8")
        try:
            before = format_source(source, str(path), split_members=False)
        except Exception:
            continue
        if format_admitted(source, str(path))[0] != before:
            split.append(path.name)
    assert split, "no example was split: the member split is not reaching anything"


# ------------------------------------------------ check / load / swap


def test_check_answers_with_the_digest_of_the_canonical_text():
    result = _call("revl_check", {"source": TERSE})
    assert result["ok"] is True
    assert result["canonicalSource"] == {"changed": True,
                                         "digest": canonical.digest(format_source(TERSE))}


def test_check_returns_the_canonical_text_only_when_asked():
    result = _call("revl_check", {"source": TERSE, "returnCanonical": True})
    assert result["canonicalSource"]["source"] == format_source(TERSE)


def test_canonical_source_comes_back_unchanged():
    canon = format_source(TERSE)
    assert _call("revl_check", {"source": canon})["canonicalSource"]["changed"] is False


def test_a_refusal_names_the_line_the_agent_wrote():
    """Compiled as sent: the split would move the error from line 5 to line 7."""
    broken = TERSE + "component Bad provides clock: Clock {\n  provide clock { fn now() = \"x\" fn later(n) = n }\n}\n"
    result = _call("revl_check", {"source": broken})
    assert result["ok"] is False
    assert result["diagnostics"][0]["line"] == 6
    assert result["canonicalSource"]["changed"] is True


def test_unreadable_text_is_compiled_and_reported_as_written():
    weird = TERSE + "component § {}\n"
    result = _call("revl_check", {"source": weird})
    assert result["ok"] is False
    report = result["canonicalSource"]
    assert report["changed"] is False and report["kept"].startswith("not formatted")
    assert report["digest"] == canonical.digest(weird)


@needs_runtime
def test_after_a_terse_swap_every_method_is_addressable():
    """The one-line provide the agent sent is held split, so each of its
    methods can be edited by symbol."""
    assert _call("revl_load", {"source": format_source(TERSE)})["ok"] is True
    assert _call("revl_swap", {"source": TERSE})["swapped"] is True
    edited = _call("revl_edit", {"edits": [{"symbol": "FixedClock.clock.later",
                                            "body": "n*3"}]})
    assert edited["swapped"] is True, edited
    assert _call("revl_call", {"key": "clock", "method": "later",
                               "args": [2]})["result"] == 6


@needs_runtime
def test_a_booting_load_keeps_the_bytes_it_was_sent():
    """A control: a load that boots holds what was sent (a snapshot of it
    reproduces those bytes, tests/test_persistence.py); only a draft is held
    canonical."""
    result = _call("revl_load", {"source": TERSE})
    assert result["ok"] is True and "canonicalSource" not in result
    assert server_mod.SESSION.origin["source"] == TERSE


@needs_runtime
def test_a_terse_swap_is_stored_canonically():
    assert _call("revl_load", {"source": format_source(TERSE)})["ok"] is True
    swapped = _call("revl_swap", {"source": TERSE.replace("fn now()=7", "fn now()=8")})
    assert swapped["swapped"] is True, swapped
    assert swapped["canonicalSource"]["changed"] is True
    assert "    fn now() = 8\n" in server_mod.SESSION.origin["source"]
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 8


def test_the_three_verbs_advertise_return_canonical():
    tools = {t["name"]: t for t in server_mod._ADVERTISED}
    for name in ("revl_check", "revl_load", "revl_swap"):
        assert "returnCanonical" in tools[name]["inputSchema"]["properties"], name


# ------------------------------------------------ a new method from the service


NOW_ONLY = ("service Clock {\n  fn now() -> Int\n}\n\n"
            "component FixedClock provides clock: Clock {\n"
            "  provide clock {\n    fn now() = 7\n  }\n}\n")


@needs_runtime
def test_a_body_for_a_new_method_writes_its_frame_from_the_service():
    """The service gains an operation and the provider implements it, in one
    call: the second edit reads the signature the first one wrote."""
    assert _call("revl_load", {"source": NOW_ONLY})["ok"] is True
    result = _call("revl_edit", {"edits": [
        {"symbol": "Clock", "replacement":
            "service Clock {\n  fn now() -> Int\n  fn later(n: Int) -> Int\n}"},
        {"symbol": "FixedClock.clock.later", "body": "n+100"}]})
    assert result["swapped"] is True, result
    echo = result["applied"][1]
    assert echo["header"] == "fn later(n)" and echo["frame"] == "from the service"
    assert "    fn later(n) = n + 100\n" in server_mod.SESSION.origin["source"]
    assert _call("revl_call", {"key": "clock", "method": "later",
                               "args": [1]})["result"] == 101


@needs_runtime
def test_a_body_for_an_operation_the_service_lacks_is_refused():
    assert _call("revl_load", {"source": NOW_ONLY})["ok"] is True
    result = _call("revl_edit", {"edits": [{"symbol": "FixedClock.clock.nope",
                                            "body": "1"}]})
    assert result["ok"] is False
    assert "no operation `nope`" in result["diagnostics"][0]["message"]


@needs_runtime
def test_a_terse_hole_fill_is_stored_canonically():
    holed = ("service Clock {\n  fn now() -> Int\n}\n\n"
             "component FixedClock provides clock: Clock {\n"
             "  provide clock {\n    fn now() = hole[Int] \"the time\"\n  }\n}\n")
    opened = _call("revl_load", {"source": holed})
    assert opened.get("draft") is True, opened
    line = opened["holes"][0]["line"]
    filled = _call("revl_edit", {"edits": [{"hole": line, "expr": "3*4"}]})
    assert filled.get("booted") is True or filled.get("swapped") is True, filled
    assert filled["applied"][0]["canonical"] == "3 * 4"
    assert "    fn now() = 3 * 4\n" in server_mod.SESSION.origin["source"]
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 12


# ------------------------------------------------ a draft is held canonical

DRAFT = ("service Clock { fn now() -> Int\n fn later(n: Int) -> Int }\n"
         "component FixedClock provides clock: Clock {\n"
         "  provide clock {\n"
         "    fn now() = hole [Int]   \"the time\"\n"
         "    fn later(n)=n+1\n"
         "  }\n"
         "}\n")


def test_a_draft_is_held_canonical_with_its_hole_lines_unchanged():
    opened = _call("revl_load", {"source": DRAFT})
    assert opened.get("draft") is True, opened
    assert opened["canonicalSource"]["changed"] is True
    held = server_mod.SESSION.pending_draft["vs"]["source"]
    assert held == format_source(DRAFT)
    assert '    fn now() = hole[Int] "the time"\n' in held
    # the hole is on the line the load reported, in the held text
    line = opened["holes"][0]["line"]
    assert "hole[Int]" in held.split("\n")[line - 1]


@needs_runtime
def test_an_anchor_copied_from_the_sent_text_still_applies():
    opened = _call("revl_load", {"source": DRAFT})
    line = opened["holes"][0]["line"]
    # the agent quotes the line as it SENT it: `fn later(n)=n+1`
    edited = _call("revl_edit", {"edits": [
        {"anchor": "fn later(n)=n+1", "replacement": "fn later(n) = n + 2"},
        {"hole": line, "expr": "5"}]})
    assert edited.get("booted") is True, edited
    assert edited["applied"][0]["matched"] == "tokens"
    assert _call("revl_call", {"key": "clock", "method": "later", "args": [1]})["result"] == 3
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 5


def test_an_anchor_that_matches_no_tokens_is_still_refused():
    from revl.mcp import edit
    with pytest.raises(edit.EditError, match="not even with its whitespace ignored"):
        edit._apply_one("fn a() = 1\n", {"anchor": "fn b() = 1", "replacement": "x"})
