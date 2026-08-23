"""revl_edit — deltas, not documents (roadmap item 50).

The token-surface audit (`bench/results/token-surface-audit.md`, finding #1)
measured that `revl_swap` re-sends the whole composition source on every
hot-swap. `revl_edit` is the delta form: an agent sends a small structured
patch against the source the server already holds, and the server applies it,
recompiles, and re-admits — through the *same* gate a full swap runs.

Two halves are under test. The patch mechanics (`_apply_edits`, `_hole_span`)
are pure and run everywhere. The load-bearing half — a patch re-admits without
the client resending the file, a patch that breaks admission is refused with
its diagnostic (the gate is not bypassed), a hole-fill pairs with a fillSpec,
and full-source swap still works — needs the cordis-py runtime, so those carry
the `@needs_runtime` marker rather than a module-level `importorskip`.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import edit  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

CACHE = (
    "service Cache { fn get(key: Str) -> Opt[Str]\n"
    "                fn size() -> Int }\n"
    "component MemCache provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide cache { fn get(key) = store.get(key)\n"
    "                  fn size() = 0 }\n"
    "}\n"
)


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ------------------------------------------------------ pure patch mechanics

def test_anchor_edit_replaces_a_literal_snippet():
    text, applied = edit._apply_edits("fn size() = 0",
                                      [{"anchor": "= 0", "replacement": "= 42"}])
    assert text == "fn size() = 42"
    assert applied[0] == {"form": "anchor", "anchor": "= 0",
                          "replacement": "= 42", "sites": 1}


def test_anchor_count_bounds_the_sites():
    text, applied = edit._apply_edits("a a a",
                                      [{"anchor": "a", "replacement": "b", "count": 2}])
    assert text == "b b a"
    assert applied[0]["sites"] == 2


def test_anchor_that_does_not_occur_is_a_clean_error():
    with pytest.raises(edit.EditError):
        edit._apply_edits("abc", [{"anchor": "zzz", "replacement": "q"}])


def test_range_edit_replaces_a_character_span():
    text, applied = edit._apply_edits("fn size() = 0",
                                      [{"range": [12, 13], "replacement": "42"}])
    assert text == "fn size() = 42"
    assert applied[0]["form"] == "range" and applied[0]["replaced"] == "0"


def test_range_of_one_offset_is_an_insertion():
    text, _ = edit._apply_edits("ab", [{"range": [1], "replacement": "X"}])
    assert text == "aXb"


def test_range_out_of_bounds_is_refused():
    with pytest.raises(edit.EditError):
        edit._apply_edits("ab", [{"range": [0, 99], "replacement": "X"}])


def test_hole_span_covers_the_whole_token():
    # bare, annotated (with nested brackets), and message forms
    for line, expect in [
        ('let x = hole  // note', "hole"),
        ('fn f() = hole[Map[Str, Int]] "why"', 'hole[Map[Str, Int]] "why"'),
        ('y = hole "m"', 'hole "m"'),
    ]:
        start, end = edit._hole_span(line, 1)
        assert line[start:end] == expect


def test_hole_fill_edit_replaces_only_the_hole():
    text, applied = edit._apply_edits('fn size() = hole[Int] "count"',
                                      [{"hole": 1, "expr": "7"}])
    assert text == "fn size() = 7"
    assert applied[0] == {"form": "hole", "line": 1,
                          "replaced": 'hole[Int] "count"', "expr": "7"}


def test_a_line_with_no_hole_is_a_clean_error():
    with pytest.raises(edit.EditError):
        edit._apply_edits("fn size() = 0", [{"hole": 1, "expr": "7"}])


def test_an_edit_without_a_recognised_form_is_refused():
    with pytest.raises(edit.EditError):
        edit._apply_edits("x", [{"nonsense": 1}])


# ------------------------------------------------------ live gate (runtime)

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`, then run this file under "
           "`backends/python/.venv/bin/pytest`",
)


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod

    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


@needs_runtime
def test_edit_patches_server_side_source_and_re_admits_without_resending():
    """The audit's #1 target: change one line without re-serializing the file.
    The agent sends only the delta; the server holds the source."""
    _call("revl_load", {"source": CACHE})
    result = _call("revl_edit",
                   {"edits": [{"anchor": "fn size() = 0",
                               "replacement": "fn size() = 42"}]})
    assert result["ok"] is True
    assert result["admitted"] is True and result["swapped"] is True
    # no `source` was ever passed to revl_edit — the patch names the change
    assert result["applied"][0]["form"] == "anchor"
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 42


@needs_runtime
def test_an_edit_that_breaks_admission_is_refused_with_the_diagnostic():
    """The gate is not bypassed by editing rather than swapping: a patch that
    violates a guarantee is refused, and the running system keeps serving."""
    _call("revl_load", {"source": CACHE})
    result = _call("revl_edit",
                   {"edits": [{"anchor": "fn size() = 0",
                               "replacement": 'fn size() = "nope"'}]})
    assert result["ok"] is False
    assert result["edited"] is False and result["swapped"] is False
    assert result["diagnostics"][0]["code"] == "T1"
    # the running composition never changed
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0


@needs_runtime
def test_a_hole_fill_edit_pairs_with_a_fill_spec():
    """An edit that introduces a hole compiles but does not swap — it comes back
    as an obligation carrying a fillSpec (expected type, line). A second edit
    addresses that line by the fillSpec's own coordinate and fills it, which
    then admits and swaps. Deltas accumulate server-side across both calls."""
    _call("revl_load", {"source": CACHE})
    scaffold = _call("revl_edit",
                     {"edits": [{"anchor": "fn size() = 0",
                                 "replacement": 'fn size() = hole[Int] "count"'}]})
    assert scaffold["ok"] is True
    assert scaffold["swapped"] is False  # a hole may not enter a running system
    assert len(scaffold["holes"]) == 1
    spec = scaffold["holes"][0]
    assert spec["fillSpec"]["expected"] == "Int"
    # the running composition is still the pre-edit one
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0

    # fill the hole by the line the fillSpec reported — no source resent
    filled = _call("revl_edit", {"edits": [{"hole": spec["line"], "expr": "7"}]})
    assert filled["ok"] is True and filled["swapped"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 7


@needs_runtime
def test_a_failed_admission_does_not_advance_the_server_side_source():
    """A refused patch leaves the working buffer at its last good state, so the
    next edit is computed against known-good source, not a broken draft."""
    _call("revl_load", {"source": CACHE})
    _call("revl_edit", {"edits": [{"anchor": "fn size() = 0",
                                   "replacement": 'fn size() = "nope"'}]})
    # the original anchor still matches, because the buffer never changed
    ok = _call("revl_edit", {"edits": [{"anchor": "fn size() = 0",
                                        "replacement": "fn size() = 5"}]})
    assert ok["swapped"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 5


@needs_runtime
def test_swap_by_name_re_admits_the_server_side_source():
    """revl_swap with no inline source re-admits what the server already holds —
    the name-referenced form the audit's #1 finding asks for."""
    _call("revl_load", {"source": CACHE})
    result = _call("revl_swap", {})
    assert result["ok"] is True and result["swapped"] is True
    assert result["fromServerSide"] is True


@needs_runtime
def test_full_source_swap_still_works():
    """Back-compat: the whole-file swap path is unchanged and still accepted."""
    _call("revl_load", {"source": CACHE})
    result = _call("revl_swap",
                   {"source": CACHE.replace("fn size() = 0", "fn size() = 99")})
    assert result["admitted"] is True and result["swapped"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 99


@needs_runtime
def test_editing_before_loading_is_a_clean_error():
    result = _call("revl_edit", {"edits": [{"anchor": "x", "replacement": "y"}]})
    assert result["ok"] is False
    assert "nothing is loaded" in result["diagnostics"][0]["message"]


@needs_runtime
def test_edit_is_advertised_with_the_delta_patch_schema():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert "revl_edit" in tools
    props = tools["revl_edit"]["inputSchema"]["properties"]
    assert "edits" in props and tools["revl_edit"]["inputSchema"]["required"] == ["edits"]
