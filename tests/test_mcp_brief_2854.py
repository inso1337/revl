"""`revl_brief`: the compact next-step answer (issue #2854).

The brief is a STRICT PROJECTION of state the session already holds: the
ambient footer every answer carries, the proposal store, the held draft and
the verdict of the last answer that judged work. These tests pin four things:
the projection is exactly the answer, field by field; an empty session is a
named answer rather than nulls; the size cap holds and drops names before the
next call; and a refusal and a check verdict both reach the brief.

None of them boots a composition. The boot is stubbed and the reads the brief
makes are the real ones, so the file runs on an interpreter without the
cordis-py runtime.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp import ambient  # noqa: E402
from revl.mcp import brief  # noqa: E402
from revl.mcp import remedy, repeat  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.leases import DEFAULT_HOLDER  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

#: A composition that compiles: one component, one provided key.
GOOD = """
service Greeter { fn greet() -> Str }
component C provides greeter: Greeter {
  provide greeter { fn greet() = "hi" }
}
"""

#: A composition that refuses with a code, a location and a repair: the reach
#: names no declared requirement.
BAD = """
service S { fn f(a: Int) -> Int }
component C provides s: S { provide s { fn f(a) = nope(a) } }
"""

#: A scaffold-shaped draft: it compiles with one open hole, so a check is green
#: and not admissible.
HOLED = """
service S { fn f(a: Int) -> Str }
component C provides s: S {
  provide s { fn f(a) { return hole } }
}
"""


class _Session:
    """A session holding a composition, with no runtime booted.

    `Session.loaded` is `_driver is not None` in the real class. Everything
    the brief reads (`loaded`, `ir`, `origin`, `_generation`, `draft`,
    `pending_draft`, the proposal store, the recorded verdict) is either given
    here or read from the real modules, so the projection under test is the
    production one."""

    def __init__(self, source=None, ir=None, generation=1, loaded=True):
        self.loaded = loaded
        self.ir = ir
        self.origin = ({"source": source, "modules": {}}
                       if source is not None else None)
        self.draft = None
        self.pending_draft = None
        self._generation = generation


def _loaded(source=GOOD, generation=2):
    return _Session(source=source, ir=compile_source(source),
                    generation=generation)


@pytest.fixture(autouse=True)
def held(monkeypatch):
    """Point the server at a fresh session and clear the two module-level
    memories a test could inherit, so each case starts where a fresh server
    does."""
    session = _Session(loaded=False)
    monkeypatch.setattr(server_mod, "SESSION", session)
    remedy.forget()
    repeat.forget()
    yield session
    remedy.forget()
    repeat.forget()


def _call(tool, arguments=None):
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool,
                                  "arguments": arguments or {}}})
    return response["result"]["structuredContent"]


def _brief():
    return _call("revl_brief")


def _compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str)


def _leaves(value):
    """Every non-container value in a nested document."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


# ---------------------------------------------------------------- empty


def test_an_empty_session_is_a_named_answer_not_nulls(held):
    answer = _brief()

    assert answer["ok"] is True
    assert answer["state"] == brief.EMPTY
    assert isinstance(answer["why"], str) and answer["why"]
    assert answer["next"]["tool"] == "revl_load"
    # not a single null in the brief: a caller reads the answer without
    # checking each value (the ambient footer is not part of the brief)
    block = {k: v for k, v in answer.items() if k != "sessionState"}
    assert None not in list(_leaves(block))
    # an empty answer carries no invented composition fields
    assert "generation" not in answer
    assert "components" not in answer
    assert "last" not in answer
    assert "pending" not in answer


def test_calling_the_brief_does_not_change_what_it_reads(held):
    """The brief's own answer is never the recorded verdict, so a second call
    is byte-identical to the first."""
    held.loaded = True
    _call("revl_check", {"source": BAD})

    assert _brief() == _brief()


def test_reading_the_brief_adds_no_state_of_its_own(held):
    """A projection that created state on every read would not be one. In
    particular the proposal store is read without its lazily-creating
    accessor, so an empty session stays empty."""
    assert not hasattr(held, "proposals")

    _brief()
    _brief()

    assert not hasattr(held, "proposals")
    assert brief.recorded(held) is None


def test_the_brief_does_not_write_to_the_held_draft(held):
    """The draft's hole count needs a compile, and the compile records the
    dependency buffers it read on the working set it is given. The brief hands
    it a copy, so the draft is byte-identical after two reads."""
    held.loaded = False
    held.pending_draft = {"vs": {"source": HOLED, "modules": {}},
                          "config": None, "record": False}
    before = copy.deepcopy(held.pending_draft["vs"])

    _brief()
    _brief()

    assert held.pending_draft["vs"] == before


# ---------------------------------------------------------------- populated


def test_a_populated_session_names_the_composition_and_generation(held):
    held.loaded = True
    held.origin = {"source": GOOD, "modules": {}}
    held.ir = compile_source(GOOD)
    held._generation = 3

    answer = _brief()

    assert answer["state"] == "loaded"
    assert answer["generation"] == 3
    assert answer["components"] == ["C"]
    assert answer["source"] == "inline source"
    assert answer["next"]["tool"] == "revl_check"


def test_a_refusal_is_reported_with_its_code_location_and_repair(held):
    held.loaded = True
    held.ir = compile_source(GOOD)
    refused = _call("revl_check", {"source": BAD})
    assert refused["ok"] is False
    diagnostic = refused["diagnostics"][0]

    answer = _brief()

    last = answer["last"]
    assert last["outcome"] == "refused"
    assert last["verb"] == "revl_check"
    assert last["code"] == diagnostic["code"] == "G1"
    assert last["location"] == f"{diagnostic['file']}:{diagnostic['line']}"
    assert last["repair"] == diagnostic["fix"]
    # the repair is in the answer, not only in the underlying diagnostic
    assert last["repair"] in _compact(answer)


def test_a_check_verdict_is_reported_when_it_is_the_last_judgement(held):
    held.loaded = True
    held.ir = compile_source(GOOD)
    checked = _call("revl_check", {"source": GOOD})
    assert checked["ok"] is True and checked["holes"] == []

    answer = _brief()

    assert answer["last"] == {"outcome": "ok", "verb": "revl_check",
                              "verdict": "checked"}
    # the candidate the caller sent is not the source the session holds, so
    # the commit step is named but not offered as ready
    assert answer["next"]["tool"] == "revl_swap"
    assert answer["next"]["ready"] is False


def test_a_green_check_of_the_held_source_can_be_swapped_as_ready(held):
    """A check of the held source (what `revl_swap {}` re-admits) is the one
    green verdict whose next call is ready to send, and it is marked `held` so
    the projection can tell the two apart."""
    held.loaded = True
    held.origin = {"source": BAD, "modules": {}}
    held.ir = compile_source(GOOD)
    held.draft = {"source": GOOD, "modules": {}}
    checked = _call("revl_check", {"session": True})
    assert checked["ok"] is True

    answer = _brief()

    assert answer["last"] == {"outcome": "ok", "verb": "revl_check",
                              "verdict": "checked", "held": True}
    assert answer["next"] == {"tool": "revl_swap", "arguments": {},
                              "ready": True}


def test_a_green_check_of_a_sent_candidate_does_not_offer_the_working_source(held):
    """The mirror of the case above: the working source is dirty, but the
    verdict judged a candidate the caller sent, so swapping the held source
    would apply something else. The brief names the step and says what it
    needs instead of offering the wrong call as ready."""
    held.loaded = True
    held.origin = {"source": BAD, "modules": {}}
    held.ir = compile_source(GOOD)
    held.draft = {"source": GOOD, "modules": {}}
    checked = _call("revl_check", {"source": GOOD + "\n// sent\n"})
    assert checked["ok"] is True

    answer = _brief()

    assert "held" not in answer["last"]
    assert answer["next"]["tool"] == "revl_swap"
    assert answer["next"]["ready"] is False
    assert answer["next"]["arguments"] == {}


def test_a_check_verdict_with_holes_carries_the_hole_count(held):
    held.loaded = False
    held.pending_draft = {"vs": {"source": HOLED, "modules": {}},
                          "config": None, "record": False}
    checked = _call("revl_check", {"session": True})
    assert checked["ok"] is True and len(checked["holes"]) == 1

    answer = _brief()

    assert answer["state"] == "draft"
    assert answer["last"]["verdict"] == "checked"
    assert answer["last"]["holes"] == 1
    # the draft's own hole count is part of the state, not a second compile
    # nobody can see
    assert answer["pending"] == {"kind": "draft", "holes": 1}
    # a holed draft cannot boot, so the next step is the edit, not a check
    assert answer["next"]["tool"] == "revl_edit"
    assert answer["next"]["ready"] is False


def test_a_draft_with_no_holes_is_booted_next(held):
    held.loaded = False
    held.pending_draft = {"vs": {"source": GOOD, "modules": {}},
                          "config": None, "record": False}

    answer = _brief()

    assert answer["state"] == "draft"
    assert answer["pending"] == {"kind": "draft", "holes": 0}
    assert answer["next"] == {"tool": "revl_load", "arguments": {},
                              "ready": True}


def test_a_green_check_that_left_holes_does_not_offer_a_ready_swap(held):
    """`ok: true` with holes is not admissible, so a ready `revl_swap {}`
    would be refused. The brief names the edit instead."""
    held.loaded = True
    held.origin = {"source": BAD, "modules": {}}
    held.ir = compile_source(GOOD)
    held.draft = {"source": HOLED, "modules": {}}
    checked = _call("revl_check", {"session": True})
    assert checked["ok"] is True and checked["holes"]

    answer = _brief()

    assert answer["last"]["held"] is True
    assert answer["last"]["holes"]
    assert answer["next"]["tool"] == "revl_edit"
    assert answer["next"]["ready"] is False


def test_a_second_verdict_replaces_the_first(held):
    """`last` is one answer, not a field soup: a green check after a refusal
    must not keep the refusal's code, and a refusal after a check must not
    keep the check's verdict."""
    held.loaded = True
    held.ir = compile_source(GOOD)
    _call("revl_check", {"source": BAD})
    assert _brief()["last"]["outcome"] == "refused"

    _call("revl_check", {"source": GOOD})
    last = _brief()["last"]
    assert last == {"outcome": "ok", "verb": "revl_check", "verdict": "checked"}
    assert "code" not in last and "repair" not in last

    _call("revl_check", {"source": BAD})
    last = _brief()["last"]
    assert last["outcome"] == "refused"
    assert "verdict" not in last and "held" not in last and "holes" not in last


def test_an_empty_session_after_a_verdict_still_offers_a_load(held):
    """Nothing is loaded, so no other next call can succeed: the verdict is
    reported, but the action is the load."""
    _call("revl_check", {"source": GOOD})

    answer = _brief()

    assert answer["state"] == brief.EMPTY
    assert answer["last"]["verdict"] == "checked"
    assert answer["next"]["tool"] == "revl_load"


def test_a_hatch_name_cannot_stretch_the_brief_past_the_cap(held):
    """The one caller-supplied name the brief records is a hatch's verb name.
    A name the server does not have is not recorded, so the answer stays
    bounded."""
    held.loaded = True
    held.ir = compile_source(GOOD)
    _call("revl_verbs", {"name": "x" * 5000, "args": {}})

    answer = _brief()

    assert answer["last"]["verb"] == "revl_verbs"
    assert brief.size({k: v for k, v in answer.items()
                       if k != "sessionState"}) <= brief.CAP


def test_a_pending_proposal_is_reported_and_committing_it_is_next(held):
    held.loaded = True
    held.ir = compile_source(GOOD)
    held._generation = 2
    held.proposals = {DEFAULT_HOLDER: {"vs": {"source": GOOD, "modules": {}},
                                       "generation": 2, "replacing": ["C"]}}

    answer = _brief()

    assert answer["state"] == "loaded"
    assert answer["pending"] == {"kind": "proposal", "generation": 2,
                                 "replacing": ["C"]}
    assert answer["next"] == {"tool": "revl_change",
                              "arguments": {"commit": True}, "ready": True}


# ------------------------------------------------------ the projection itself


def test_the_answer_is_the_projection_of_the_session_fields(held):
    """The drift guard, written as a hand projection rather than as
    `answer == brief.project(session)`: the expected block is built here from
    the same sources the brief reads, so a brief that dropped, renamed or
    stopped deriving a field reddens this test even if `project` changed with
    it."""
    held.loaded = True
    held.origin = {"source": GOOD, "modules": {}}
    held.ir = compile_source(GOOD)
    held._generation = 4
    refused = _call("revl_check", {"source": BAD})
    diagnostic = refused["diagnostics"][0]

    answer = {k: v for k, v in _brief().items() if k != "sessionState"}

    footer = ambient.footer(held)
    expected = {
        "ok": True,
        "state": "loaded",
        "generation": footer["generation"],
        "components": footer["components"],
        "source": "inline source",
        "last": {"outcome": "refused", "verb": "revl_check",
                 "code": diagnostic["code"],
                 "location": f"{diagnostic['file']}:{diagnostic['line']}",
                 "repair": diagnostic["fix"]},
        "next": {"tool": "revl_check", "arguments": {"session": True},
                 "ready": True},
    }

    assert footer["generation"] == held._generation == 4
    assert answer == expected
    # the recorded verdict is the one `last` projects, field for field
    assert answer["last"] == {k: v for k, v in brief.recorded(held).items()
                              if not k.startswith("_")}


def test_each_projected_field_follows_its_own_state(held):
    """Move one underlying field at a time: the brief moves with it and with
    nothing else, which is what "a projection, not new state" means."""
    held.loaded = True
    held.ir = compile_source(GOOD)
    held._generation = 5
    before = _brief()

    held._generation = 6
    after = _brief()

    def without_generation(answer):
        return {k: v for k, v in answer.items()
                if k not in ("generation", "sessionState")}

    assert after["generation"] == 6
    assert without_generation(after) == without_generation(before)


# ---------------------------------------------------------------- the cap


def test_the_cap_holds_and_names_go_before_the_next_call(held):
    """A composition whose names alone exceed the cap: the answer stays under
    `brief.CAP`, the names are dropped for a count, and `next` survives."""
    names = [f"Component{i:03d}" + "x" * 120 for i in range(12)]
    held.loaded = True
    held.ir = {"manifest": {"loadOrder": names,
                            "components": [{"name": n} for n in names]}}
    held._generation = 7
    held.origin = {"source": GOOD, "modules": {}}
    _call("revl_check", {"source": BAD})

    answer = _brief()

    assert brief.size({k: v for k, v in answer.items() if k != "sessionState"}) \
        <= brief.CAP
    assert "components" not in answer
    assert answer["componentCount"] == len(names)
    assert answer["next"]["tool"] in ("revl_check", "revl_swap")
    assert "components" in answer["trimmed"]


def test_a_next_call_too_large_to_carry_keeps_the_action(held, monkeypatch):
    """The largest thing a next call can carry is the source a reload would
    resend. It is dropped first, and the call itself, its `tool` and its
    `needs` stay."""
    huge = "service S { fn f() -> Int }\n" + "// pad\n" * 2000
    held.loaded = False
    held.origin = {"source": huge, "modules": {}}

    answer = _brief()

    assert answer["state"] == brief.EMPTY
    assert answer["next"]["tool"] == "revl_load"
    assert answer["next"]["arguments"] == {}
    assert answer["next"]["ready"] is False
    assert "next.arguments" in answer["trimmed"]
    assert brief.size({k: v for k, v in answer.items() if k != "sessionState"}) \
        <= brief.CAP


def test_the_cap_is_documented_and_bounded():
    assert isinstance(brief.CAP, int) and brief.CAP == 1200
    assert brief.CAP <= 2000, "a brief that costs a state read is not a brief"


def test_the_cap_is_enforced_by_construction():
    """Every trim spent and still over: the floor returns the state and the
    next call, clipped. Values no code path produces are used on purpose, so
    the bound does not rest on the enumeration being complete."""
    big = "x" * 5000
    cases = [
        {"state": "loaded", "generation": 9, "components": [big] * 10,
         "source": big,
         "last": {"outcome": "refused", "verb": big, "code": big,
                  "location": big, "repair": big},
         "pending": {"kind": "proposal", "replacing": [big] * 5},
         "next": {"tool": big, "arguments": {"source": big}, "ready": True}},
        {"state": brief.EMPTY, "why": big,
         "next": {"tool": big, "arguments": {}, "ready": False,
                  "needs": big}},
        {"state": big, "why": big, "trimmed": [big] * 20,
         "next": {"tool": big, "arguments": {"a": big}, "ready": True,
                  "needs": big}},
    ]

    for case in cases:
        answer = brief._bounded(case)
        assert brief.size(answer) <= brief.CAP, case
        # the action survives even when everything else does not
        assert answer["next"]["tool"]
        assert answer["state"]
        assert answer["trimmed"]
