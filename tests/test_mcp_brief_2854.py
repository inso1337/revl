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
    assert answer["pending"] == {"kind": "draft"}
    assert answer["next"] == {"tool": "revl_check", "arguments": {"session": True},
                              "ready": True}


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
    """The drift guard: the verb's answer is exactly `brief.project` of the
    session, and each projected field equals the state it projects. A field
    that stopped being derived would fail here."""
    held.loaded = True
    held.origin = {"source": GOOD, "modules": {}}
    held.ir = compile_source(GOOD)
    held._generation = 4
    _call("revl_check", {"source": BAD})

    answer = _brief()

    assert answer == {"ok": True, **brief.project(held),
                      "sessionState": ambient.footer(held)}
    footer = ambient.footer(held)
    assert answer["state"] == "loaded"
    assert answer["generation"] == held._generation == footer["generation"]
    assert answer["components"] == footer["components"]
    assert answer["source"] == "inline source"
    assert answer["last"] == brief.recorded(held)

    # a field the state does not hold cannot appear in the answer
    assert set(answer) == {"ok", "state", "generation", "components", "source",
                           "last", "next", "sessionState"}


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
