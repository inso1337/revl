"""The agent's default reach over MCP (issue #1704).

An agent benchmark showed that revl's own surfaces (reuse by admission, the
typed scaffold, the exact blast radius, the nine guarantees) are rarely the
first thing an agent reaches for. Three changes make them the default, and
each is pinned here:

1. The `initialize` instructions name the loop in order with the exact verbs:
   revl_resolve -> revl_scaffold -> fill -> revl_query_withdraw -> revl_check
   -> revl_admit. Each tool on the loop says which step it is.
2. A `revl_edit`, `revl_swap` or `revl_change` response carries
   `blastRadius`: the revl_query_withdraw cascade for every component the
   change touches, read off the running composition.
3. `revl_check` carries `selfCheck`: every guarantee G1-G9 as pass, fail (with
   the code and the fix) or unchecked, in one call.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import authoring_loop  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

LOOP_VERBS = ["revl_resolve", "revl_scaffold", "revl_edit",
              "revl_query_withdraw", "revl_check", "revl_admit"]

#: Mem provides `store`; Web injects it and provides `front`; Client injects
#: `front`. Replacing Mem cascades through Web to Client.
CHAIN = (
    "service Store { fn get(k: Str) -> Opt[Str] }\n"
    "service Front { fn hit(k: Str) -> Opt[Str] }\n"
    "service Report { fn count() -> Int }\n"
    "component Mem provides store: Store {\n"
    "  let m = effect Map.new() undo m.drop()\n"
    "  provide store { fn get(k) = m.get(k) }\n"
    "}\n"
    "component Web requires store: Store provides front: Front {\n"
    "  provide front { fn hit(k) = store.get(k) }\n"
    "}\n"
    "component Client requires front: Front provides report: Report {\n"
    "  provide report { fn count() = 0 }\n"
    "}\n"
)

CLEAN = ("service S { fn f() -> Int }\n"
         "component C provides s: S { provide s { fn f() = 1 } }\n")

#: Two providers of one key: a G2 refusal.
TWO_PROVIDERS = ("service S { fn f() -> Int }\n"
                 "component A provides s: S { provide s { fn f() = 1 } }\n"
                 "component B provides s: S { provide s { fn f() = 2 } }\n")

HOLED = ("service S { fn f() -> Int }\n"
         'component C provides s: S { provide s { fn f() = hole[Int] "n" } }\n')


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ------------------------------------------------------------ 1. the loop text

def test_the_instructions_name_the_loop_in_order():
    text = handle({"jsonrpc": "2.0", "id": 1,
                   "method": "initialize"})["result"]["instructions"]
    steps = ["reuse", "scaffold", "fill", "preflight", "check", "commit"]
    positions = [text.index(f"{i}. {step} with {verb}")
                 for i, (step, verb) in enumerate(zip(steps, LOOP_VERBS), 1)]
    assert positions == sorted(positions), text
    assert "hole[T]" in text and "fillSpec" in text and "G1-G9" in text


def test_every_loop_tool_says_which_step_it_is():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    described = {t["name"]: t["description"] for t in listed["result"]["tools"]}
    for i, verb in enumerate(LOOP_VERBS, 1):
        assert described[verb].startswith(
            f"Authoring loop step {i} of {len(LOOP_VERBS)} "), verb


def test_the_loop_verbs_are_real_tools():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert set(LOOP_VERBS) <= names
    assert {verb for _step, verb, _what in authoring_loop.LOOP} == set(LOOP_VERBS)


# ---------------------------------------------------------- 3. the self-check

def test_a_clean_check_passes_all_nine_guarantees():
    result = _call("revl_check", {"source": CLEAN})
    check = result["selfCheck"]
    assert [g["code"] for g in check["guarantees"]] == [f"G{n}" for n in range(1, 10)]
    assert {g["status"] for g in check["guarantees"]} == {"pass"}
    assert check["summary"] == {"pass": 9, "fail": 0, "unchecked": 0}
    assert check["admissible"] is True
    assert all(g["guarantee"] for g in check["guarantees"])


def test_a_refusal_fails_its_guarantee_with_the_code_and_fix():
    result = _call("revl_check", {"source": TWO_PROVIDERS})
    assert result["ok"] is False
    check = result["selfCheck"]
    rows = {g["code"]: g for g in check["guarantees"]}
    assert len(rows) == 9
    assert rows["G2"]["status"] == "fail"
    assert rows["G2"]["fix"]
    assert rows["G2"]["failures"][0]["code"] == "G2"
    # the compile stopped at G2: the rest are not claimed to pass
    assert {g["status"] for c, g in rows.items() if c != "G2"} == {"unchecked"}
    assert check["admissible"] is False


def test_a_refusal_outside_g1_g9_is_listed_beside_the_rows():
    result = _call("revl_check", {"source": "service S { fn f() -> Int }\n"
                                            "component C provides s: S {\n"
                                            '  provide s { fn f() = "x" } }\n'})
    check = result["selfCheck"]
    assert check["otherFailures"], result["diagnostics"]
    assert check["otherFailures"][0]["code"] == result["diagnostics"][0]["code"]
    assert check["summary"]["pass"] == 0


def test_a_draft_with_holes_holds_every_guarantee_but_is_not_admissible():
    check = _call("revl_check", {"source": HOLED})["selfCheck"]
    assert check["summary"]["pass"] == 9
    assert check["admissible"] is False
    assert "hole" in check["note"]


# --------------------------------------------------- 2. the blast radius, pure

def test_only_the_changed_component_is_touched():
    before = compile_source(CHAIN, "chain.rvl")
    after = compile_source(CHAIN.replace("fn count() = 0", "fn count() = 1"),
                           "chain.rvl")
    assert authoring_loop.touched_components(before, after) == ["Client"]


def test_shifted_lines_alone_touch_nothing():
    before = compile_source(CHAIN, "chain.rvl")
    after = compile_source("\n\n" + CHAIN, "chain.rvl")
    assert authoring_loop.touched_components(before, after) == []


def test_the_radius_of_a_provider_is_its_withdrawal_cascade():
    before = compile_source(CHAIN, "chain.rvl")
    after = compile_source(CHAIN.replace(
        "let m = effect Map.new() undo m.drop()",
        "let m = effect Map.new() undo m.drop()\n"
        "  let n = effect Map.new() undo n.drop()"), "chain.rvl")
    radius = authoring_loop.blast_radius(before, after)
    assert radius["touched"] == ["Mem"]
    mem = radius["components"]["Mem"]
    assert [c["component"] for c in mem["cascade"]] == ["Web", "Client"]
    assert mem["breaks"] == 2 and radius["breaks"] == 2
    assert mem["withdrawalOrder"] == ["Client", "Web", "Mem"]


def test_an_added_component_has_no_cascade_yet():
    before = compile_source(CHAIN, "chain.rvl")
    after = compile_source(CHAIN + "service X { fn x() -> Int }\n"
                           "component Extra provides x: X { provide x { fn x() = 0 } }\n",
                           "chain.rvl")
    radius = authoring_loop.blast_radius(before, after)
    assert radius["touched"] == ["Extra"]
    assert radius["components"]["Extra"] == {"added": True, "cascade": [],
                                             "withdrawalOrder": [], "breaks": 0}


# ------------------------------------------------- 2. the blast radius, live

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="revl_edit patches a running composition and needs the cordis-py "
           "runtime",
)


@pytest.fixture(autouse=True)
def _fresh_session():
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


@needs_runtime
def test_an_edit_response_carries_the_cascade_of_what_it_touches():
    assert _call("revl_load", {"source": CHAIN})["ok"] is True
    edited = _call("revl_edit", {"edits": [{
        "anchor": "let m = effect Map.new() undo m.drop()",
        "replacement": "let m = effect Map.new() undo m.drop()\n"
                       "  let n = effect Map.new() undo n.drop()"}]})
    assert edited["swapped"] is True, edited
    radius = edited["blastRadius"]
    assert radius["touched"] == ["Mem"]
    assert [c["component"] for c in radius["components"]["Mem"]["cascade"]] == [
        "Web", "Client"]
    assert radius["breaks"] == 2


@needs_runtime
def test_a_leaf_edit_has_an_empty_cascade():
    assert _call("revl_load", {"source": CHAIN})["ok"] is True
    edited = _call("revl_edit", {"edits": [{"anchor": "fn count() = 0",
                                            "replacement": "fn count() = 1"}]})
    assert edited["swapped"] is True, edited
    assert edited["blastRadius"]["touched"] == ["Client"]
    assert edited["blastRadius"]["components"]["Client"]["cascade"] == []
    assert edited["blastRadius"]["breaks"] == 0


@needs_runtime
def test_a_draft_edit_with_a_hole_carries_the_cascade_too():
    assert _call("revl_load", {"source": CHAIN})["ok"] is True
    edited = _call("revl_edit", {"edits": [{
        "anchor": "fn hit(k) = store.get(k)",
        "replacement": 'fn hit(k) = hole[Opt[Str]] "lookup"'}]})
    assert edited["swapped"] is False and edited["holes"]
    radius = edited["blastRadius"]
    assert radius["touched"] == ["Web"]
    assert [c["component"] for c in radius["components"]["Web"]["cascade"]] == [
        "Client"]


#: Mem as CHAIN declares it, with a second effect: a change to Mem alone.
MEM_TWICE = (
    "component Mem provides store: Store {\n"
    "  let m = effect Map.new() undo m.drop()\n"
    "  let n = effect Map.new() undo n.drop()\n"
    "  provide store { fn get(k) = m.get(k) }\n"
    "}"
)


def _mem_cascade(radius: dict) -> list[str]:
    assert radius["touched"] == ["Mem"], radius
    assert radius["breaks"] == 2, radius
    return [c["component"] for c in radius["components"]["Mem"]["cascade"]]


@needs_runtime
def test_an_inline_swap_carries_the_cascade_of_what_it_replaces():
    assert _call("revl_load", {"source": CHAIN})["ok"] is True
    swapped = _call("revl_swap", {"source": CHAIN.replace(
        "  let m = effect Map.new() undo m.drop()\n",
        "  let m = effect Map.new() undo m.drop()\n"
        "  let n = effect Map.new() undo n.drop()\n")})
    assert swapped["swapped"] is True, swapped
    assert _mem_cascade(swapped["blastRadius"]) == ["Web", "Client"]


@needs_runtime
def test_a_swap_by_name_carries_the_radius_too():
    assert _call("revl_load", {"source": CHAIN})["ok"] is True
    swapped = _call("revl_swap", {})
    assert swapped["swapped"] is True and swapped["fromServerSide"] is True, swapped
    # the held source re-admitted unchanged touches no component
    assert swapped["blastRadius"]["touched"] == []
    assert swapped["blastRadius"]["breaks"] == 0


@needs_runtime
def test_a_change_proposal_and_its_commit_both_carry_the_cascade():
    assert _call("revl_load", {"source": CHAIN})["ok"] is True
    proposed = _call("revl_change", {"replace": {"component": "Mem",
                                                 "source": MEM_TWICE}})
    assert proposed["ok"] is True and proposed["committed"] is False, proposed
    assert _mem_cascade(proposed["blastRadius"]) == ["Web", "Client"]
    committed = _call("revl_change", {"commit": True})
    assert committed["committed"] is True, committed
    assert _mem_cascade(committed["blastRadius"]) == ["Web", "Client"]


@needs_runtime
def test_a_change_committed_in_one_call_carries_the_cascade():
    assert _call("revl_load", {"source": CHAIN})["ok"] is True
    committed = _call("revl_change", {"commit": True, "replace": {
        "component": "Mem", "source": MEM_TWICE}})
    assert committed["committed"] is True, committed
    assert _mem_cascade(committed["blastRadius"]) == ["Web", "Client"]
