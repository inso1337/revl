"""Issue #1694: an identical repeated refusal changes shape.

The server remembers the last refused call by verb and a digest of its
arguments. The same call refused again right after it comes back changed: it
names the attempt, restates the reason beside what the session holds now,
keeps the `next` call (#1691), and from attempt `BOUND` on leads with a
distinct `REPEATED_REFUSAL` diagnostic. A changed argument, a different verb or
a success resets the count; a success is never rewritten.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import remedy, repeat  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

GOOD = "component Ok {\n}\n"
BAD = "component Broken {\n"
CALL = ("revl_call", {"key": "cache", "method": "size", "args": []})


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _body(payload: dict) -> dict:
    """The payload without the session footer, which is the same on both."""
    return {k: v for k, v in payload.items() if k != "sessionState"}


@pytest.fixture(autouse=True)
def _clean():
    # a server that can boot a composition, so `revl_call` reaches the
    # nothing-loaded refusal rather than the #1692 runtime gate (the operator
    # step case below sets the runtime unavailable itself)
    server_mod.set_runtime_available(True)
    repeat.forget()
    remedy.forget()
    yield
    server_mod.set_runtime_available(None)
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    repeat.forget()
    remedy.forget()


def test_two_identical_refusals_have_different_bodies():
    first = _call(*CALL)
    second = _call(*CALL)
    assert first["ok"] is False and second["ok"] is False
    assert _body(first) != _body(second)
    assert "repeat" not in first
    assert second["repeat"] == {"attempt": 2, "bound": repeat.BOUND}
    message = second["diagnostics"][0]["message"]
    assert message.startswith("attempt 2 of this same call, refused again")
    # the violated precondition, restated beside the state that violates it
    assert "nothing is loaded — call revl_load first" in message
    assert message.endswith("The session now: nothing is loaded")
    # the next call (#1691) still rides along
    assert second["next"] == first["next"]
    assert second["next"]["tool"] == "revl_load"


def test_every_further_repeat_is_new_and_the_bound_has_its_own_code():
    bodies = [_body(_call(*CALL)) for _ in range(repeat.BOUND + 1)]
    assert len({repr(b) for b in bodies}) == len(bodies)
    for attempt, body in enumerate(bodies, start=1):
        codes = [d["code"] for d in body["diagnostics"]]
        if attempt < repeat.BOUND:
            assert repeat.CODE not in codes
        else:
            assert codes[0] == repeat.CODE
            assert f"refused {attempt} times in a row" in body["diagnostics"][0]["message"]
            assert "send `next` instead" in body["diagnostics"][0]["message"]


def test_a_changed_argument_resets_the_count():
    _call(*CALL)
    other = _call("revl_call", {"key": "cache", "method": "get", "args": ["k"]})
    assert "repeat" not in other
    again = _call(*CALL)
    assert "repeat" not in again, "the last refusal was a different call"
    assert _call(*CALL)["repeat"]["attempt"] == 2


def test_a_different_verb_resets_the_count():
    _call(*CALL)
    assert _call(*CALL)["repeat"]["attempt"] == 2
    _call("revl_unload", {})
    assert "repeat" not in _call(*CALL)


def test_a_success_resets_the_count_and_is_never_rewritten():
    refused = _call("revl_check", {"source": BAD})
    assert refused["ok"] is False
    assert _call("revl_check", {"source": BAD})["repeat"]["attempt"] == 2
    ok_first = _call("revl_check", {"source": GOOD})
    ok_second = _call("revl_check", {"source": GOOD})
    assert ok_first["ok"] is True and "repeat" not in ok_second
    assert _body(ok_first) == _body(ok_second)
    assert "repeat" not in _call("revl_check", {"source": BAD})


def test_argument_order_does_not_make_a_different_call():
    _call("revl_call", {"key": "cache", "method": "size", "args": []})
    second = _call("revl_call", {"args": [], "method": "size", "key": "cache"})
    assert second["repeat"]["attempt"] == 2


def test_a_refusal_with_no_diagnostics_gets_one(monkeypatch):
    monkeypatch.setitem(server_mod._HANDLERS, "revl_state",
                        lambda _arguments: {"ok": False, "note": "no"})
    _call("revl_state", {})
    second = _call("revl_state", {})
    assert second["note"] == "no"
    assert second["diagnostics"][0]["message"].startswith(
        "attempt 2 of this same call, refused again for the same reason: "
        "the call was refused")
    assert "send `next`" not in repr(second)


def test_the_state_is_rendered_from_the_footer():
    assert repeat.render({"loaded": False}) == "nothing is loaded"
    loaded = {"loaded": True, "generation": 3, "components": ["A", "B"],
              "dirty": True, "draft": False}
    assert repeat.render(loaded) == (
        "generation 3 is running (A, B), and the working source differs "
        "from what is running")
    assert repeat.render({**loaded, "draft": True}).endswith(
        "with a draft edit that has open holes pending")


def test_past_the_bound_an_operator_step_is_not_offered_as_a_call():
    """A runtime-gate refusal (#1692) has an operator step for `next`, which
    the caller cannot send; the bound's message says an operator must act."""
    server_mod.set_runtime_available(False)
    try:
        bodies = [_call("revl_load", {"source": GOOD}) for _ in range(repeat.BOUND)]
    finally:
        server_mod.set_runtime_available(None)
    stop = bodies[-1]["diagnostics"][0]
    assert stop["code"] == repeat.CODE
    assert "an operator has to act first" in stop["message"]
    assert "send `next`" not in stop["message"]
