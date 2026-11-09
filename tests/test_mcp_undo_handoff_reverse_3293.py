"""Issue #3293: an undo carries live state back across a compatible `handoff`.

A forward swap threads a provider's state onto its successor when the
successor's declared `handoff` accepts the running provider's (item 53). An
undo used to re-admit the restored generation cold even when both generations
declared the same hand-off, dropping exactly the state the undo exists to
keep. Now the undo runs the same capture/restore in the restore direction: the
running generation exports, the restored generation accepts. A shape that does
not accept the running state in that direction still starts cold, and the
dossier names it (issue #2346).

These drive a live composition, so they need the cordis-py runtime; the gate
is a per-test marker.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="an undo drives a live composition; install the runtime with "
           "`sh backends/python/setup.sh`")


SERVICE = (
    "service Meter { fn log(label: Str)\n"
    "                fn get(label: Str) -> Opt[Str]\n"
    "                fn version() -> Int }\n")


def _book(version: int, handoff: str | None = "Map[Str, Str]", *,
          name: str = "MeterBook") -> str:
    """The `MeterBook` component alone, the text a `replace` takes. `handoff`
    is the whole declared state shape, or None for no hand-off."""
    line = f"  handoff meter: {handoff}\n" if handoff else ""
    return (
        f"component {name} provides meter: Meter {{\n"
        f"{line}"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide meter {\n"
        "    fn log(label) { effect store.insert(label, label) "
        "undo store.remove(label) }\n"
        "    fn get(label) = store.get(label)\n"
        f"    fn version() = {version}\n"
        "  }\n"
        "}\n")


def _meter(version: int, handoff: str | None = "Map[Str, Str]") -> str:
    return SERVICE + _book(version, handoff)


SEEDED = (
    "service Audit { fn touched() -> Int }\n"
    "component SeededAudit provides audit: Audit {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  effect store.insert(\"boot\", \"1\") undo store.remove(\"boot\")\n"
    "  provide audit { fn touched() = 1 }\n"
    "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _meter_call(method: str, *args) -> dict:
    return _call("revl_call", {"key": "meter", "method": method,
                               "args": list(args)})


@pytest.fixture(autouse=True)
def _fresh_server_session():
    from revl.mcp import server as server_mod

    server_mod.UNDO_STACK.clear()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.UNDO_STACK.clear()


def _swapped_with_state() -> dict:
    """Load generation 1, record four labels, commit a `replace` swap whose
    successor carries them through the same declared `handoff`."""
    loaded = _call("revl_load", {"source": _meter(1)})
    assert loaded["ok"] is True, loaded
    for label in ("l1", "l2", "l3", "l4"):
        assert _meter_call("log", label)["ok"] is True
    changed = _call("revl_change", {
        "commit": True,
        "replace": {"component": "MeterBook", "source": _book(2)}})
    assert changed["ok"] is True and changed.get("committed") is True, changed
    assert _meter_call("version")["result"] == 2
    return changed


@needs_runtime
def test_undo_carries_the_live_state_back_across_a_compatible_handoff():
    changed = _swapped_with_state()
    assert changed["handoff"]["meter"]["migrated"] is True
    # the forward swap's advice has nothing to warn about: the undo carries it
    assert "undoNote" not in changed, changed
    assert changed["undo"] == {"tool": "revl_undo", "arguments": {"to": 1}}

    undone = _call(changed["undo"]["tool"], changed["undo"]["arguments"])
    assert undone["ok"] is True and undone["undone"] is True, undone
    # the restored generation runs with the labels the undo exists to keep
    assert _meter_call("version")["result"] == 1
    assert _meter_call("get", "l1")["result"] == "l1"
    assert _meter_call("get", "l4")["result"] == "l4"
    # the dossier reports the carry, not a drop
    dropped = undone["dossier"]["stateDropped"]
    assert dropped["components"] == [], dropped
    assert dropped["carried"] == ["MeterBook"], dropped
    assert "note" not in dropped, dropped
    assert undone["handoff"]["meter"]["migrated"] is True
    assert undone["handoff"]["meter"]["resources"] == 1


@needs_runtime
def test_step_back_carries_the_state_back_too():
    """`revl_step_back` runs the recorded `revl_undo`, so it inherits the
    restore-direction carry."""
    _swapped_with_state()
    stepped = _call("revl_step_back", {})
    assert stepped["ok"] is True and stepped["steppedBack"] is True, stepped
    assert _meter_call("version")["result"] == 1
    assert _meter_call("get", "l1")["result"] == "l1"


@needs_runtime
def test_undo_stays_cold_when_the_restored_shape_cannot_accept_in_reverse():
    """The forward swap widened `Str` to `Opt[Str]` and was admitted; the undo
    runs the same §5 relation the other way (`Str` must accept `Opt[Str]`) and
    it does not, so the provider starts cold and the dossier says so."""
    loaded = _call("revl_load", {"source": _meter(1, "Map[Str, Str]")})
    assert loaded["ok"] is True, loaded
    for label in ("l1", "l2"):
        assert _meter_call("log", label)["ok"] is True
    changed = _call("revl_change", {
        "commit": True,
        "replace": {"component": "MeterBook",
                    "source": _book(2, "Map[Str, Opt[Str]]")}})
    assert changed["ok"] is True and changed.get("committed") is True, changed
    assert changed["handoff"]["meter"]["migrated"] is True
    assert _meter_call("get", "l1")["result"] == "l1"
    # the forward swap already warns that this undo cannot carry it back
    note = changed.get("undoNote")
    assert note and "MeterBook" in note and "would be dropped" in note, changed

    undone = _call("revl_undo", {})
    assert undone["ok"] is True and undone["undone"] is True, undone
    assert _meter_call("version")["result"] == 1
    assert _meter_call("get", "l1")["result"] is None   # cold, as before
    dropped = undone["dossier"]["stateDropped"]
    assert dropped["components"] == ["MeterBook"], dropped
    assert "carried" not in dropped, dropped
    assert "MeterBook" in dropped["note"] and "cold" in dropped["note"]
    # the undo's own hand-off report names the reason, not a silent drop
    assert undone["handoff"]["meter"]["migrated"] is False
    assert undone["handoff"]["meter"]["reason"] == "incompatible"


@needs_runtime
def test_undo_carries_state_back_to_a_renamed_provider():
    """Providers correlate by the key they provide, not their component name:
    a swap that renames the provider (`replacing`) still carries its state
    back, and the running component the target withdraws is not reported as
    dropped because its state crosses."""
    loaded = _call("revl_load", {"source": _meter(1)})
    assert loaded["ok"] is True, loaded
    for label in ("l1", "l2"):
        assert _meter_call("log", label)["ok"] is True
    swapped = _call("revl_swap", {
        "source": _meter(2).replace("component MeterBook ",
                                    "component MeterBook2 "),
        "replacing": ["MeterBook"]})
    assert swapped["ok"] is True, swapped
    assert swapped["handoff"]["meter"] == {
        "component": "MeterBook2", "migrated": True, "resources": 1}
    assert _meter_call("get", "l1")["result"] == "l1"

    undone = _call("revl_undo", {})
    assert undone["ok"] is True and undone["undone"] is True, undone
    assert _meter_call("version")["result"] == 1
    assert _meter_call("get", "l1")["result"] == "l1"
    dropped = undone["dossier"]["stateDropped"]
    assert dropped["components"] == [], dropped
    assert dropped["carried"] == ["MeterBook2"], dropped
    assert undone["handoff"]["meter"]["migrated"] is True


@needs_runtime
def test_the_forward_undo_advice_names_only_the_state_the_undo_drops():
    """Two providers hold live state after the swap; only one declares a
    compatible hand-off on both sides. The forward advice names the one whose
    state the undo will really drop."""
    source = _meter(1) + SEEDED
    assert _call("revl_load", {"source": source})["ok"] is True
    assert _meter_call("log", "l1")["ok"] is True
    changed = _call("revl_change", {
        "commit": True,
        "replace": {"component": "MeterBook", "source": _book(2)}})
    assert changed["ok"] is True, changed
    note = changed.get("undoNote")
    assert note and "SeededAudit" in note, changed
    assert "the live state held by SeededAudit would be dropped" in note, note
    assert "this undo carries MeterBook back" in note, note

    undone = _call("revl_undo", {})
    assert undone["ok"] is True, undone
    assert _meter_call("get", "l1")["result"] == "l1"    # carried back
    dropped = undone["dossier"]["stateDropped"]
    assert dropped["components"] == ["SeededAudit"], dropped
    assert dropped["carried"] == ["MeterBook"], dropped
    assert "SeededAudit" in dropped["note"], dropped
    assert "carries MeterBook back" in dropped["note"], dropped
