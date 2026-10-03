"""A swap may keep a component the running generation was already running
PENDING (issue #1751).

`examples/app/notes.rvl` loads with `NotesConsole` PENDING: it requires the
ambient `webui` host service, which this host does not supply. The item-334
health gate refused any successor fiber left PENDING, and every root key that
did not resolve, so the composition could never be swapped, even to its own
unchanged source. These tests hold the fix and its edges: the same pending
component may stay pending, while a component that was ACTIVE and comes back
PENDING, and a new component that comes up PENDING, are still refused.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

NOTES = ROOT / "examples" / "app" / "notes.rvl"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")

# `Host` is an ambient service nothing here provides, so a component that
# requires it stays PENDING; `Clock` is provided and its users activate.
BASE = ("service Host { fn ping() -> Int }\n"
        "service Clock { fn now() -> Int }\n"
        "service Panel { fn show() -> Int }\n"
        "component FixedClock provides clock: Clock {\n"
        "  provide clock { fn now() = 7 }\n"
        "}\n")
WAITING = ("component HostPanel requires host: Host provides panel: Panel {\n"
           "  provide panel { fn show() = host.ping() }\n"
           "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _states(payload: dict) -> dict:
    return {c["name"]: c["state"] for c in payload.get("components") or []}


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(ROOT), str(tmp_path)))
    yield tmp_path
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.AUTHORING = old


def test_notes_can_be_swapped_to_itself():
    loaded = _call("revl_load", {"files": [str(NOTES)]})
    assert loaded["ok"] is True
    assert _states(loaded)["NotesConsole"] == "PENDING"
    swapped = _call("revl_swap", {"files": [str(NOTES)]})
    assert swapped["ok"] is True and swapped["swapped"] is True, swapped
    assert _states(swapped)["NotesConsole"] == "PENDING"
    assert _states(swapped)["NotesHttp"] == "ACTIVE"


def test_a_component_pending_before_may_stay_pending_while_others_change():
    loaded = _call("revl_load", {"source": BASE + WAITING})
    assert _states(loaded) == {"FixedClock": "ACTIVE", "HostPanel": "PENDING"}
    changed = _call("revl_swap", {"source": BASE.replace("now() = 7", "now() = 8")
                                  + WAITING})
    assert changed["ok"] is True and changed["swapped"] is True, changed
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 8


def test_an_active_component_that_comes_back_pending_is_still_refused():
    _call("revl_load", {"source": BASE})
    regressed = BASE.replace(
        "component FixedClock provides clock: Clock {\n"
        "  provide clock { fn now() = 7 }",
        "component FixedClock requires host: Host provides clock: Clock {\n"
        "  provide clock { fn now() = host.ping() }")
    refused = _call("revl_swap", {"source": regressed})
    assert refused["ok"] is False
    assert "FixedClock" in refused["diagnostics"][0]["message"]
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 7


def test_a_new_component_that_comes_up_pending_is_still_refused():
    _call("revl_load", {"source": BASE})
    refused = _call("revl_swap", {"source": BASE + WAITING})
    assert refused["ok"] is False
    assert "HostPanel" in refused["diagnostics"][0]["message"]
    assert "PENDING" in refused["diagnostics"][0]["message"]
