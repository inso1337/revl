"""Issue #2346: `revl_undo` after a handoff swap must not drop a component's
state while its dossier reports that nothing was dropped.

An undo re-admits the target generation's sources and starts them cold
(`migrate="respawn"`): a rollback never carries the generation it leaves into
the one it returns to, so the forward swap's `handoff` is deliberately not run
in reverse (docs/state-handoff.md, docs/generation-history.md). What the undo
owes the caller is the truth about that: every component whose live state the
cold start discards is named in `dossier.stateDropped.components`, with a
`note`, and the forward swap's `undo` advice says the same before the caller
follows it.

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


def _book(version: int, *, handoff: bool = True) -> str:
    """The `MeterBook` component alone, the text a `replace` takes."""
    line = "  handoff meter: Map[Str, Str]\n" if handoff else ""
    return (
        "component MeterBook provides meter: Meter {\n"
        f"{line}"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide meter {\n"
        "    fn log(label) { effect store.insert(label, label) "
        "undo store.remove(label) }\n"
        "    fn get(label) = store.get(label)\n"
        f"    fn version() = {version}\n"
        "  }\n"
        "}\n")


def _meter(version: int, *, handoff: bool = True) -> str:
    return SERVICE + _book(version, handoff=handoff)


STATELESS = (
    "service Clock { fn now() -> Int }\n"
    "component Fixed provides clock: Clock {\n"
    "  provide clock { fn now() = 1 }\n"
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


def _swapped_with_state(*, handoff: bool = True) -> dict:
    """The issue's repro up to the undo: load, record four labels, commit a
    `replace` swap whose successor carries them through its `handoff`."""
    loaded = _call("revl_load", {"source": _meter(1, handoff=handoff)})
    assert loaded["ok"] is True, loaded
    for label in ("l1", "l2", "l3", "l4"):
        assert _meter_call("log", label)["ok"] is True
    changed = _call("revl_change", {
        "commit": True,
        "replace": {"component": "MeterBook",
                    "source": _book(2, handoff=handoff)}})
    assert changed["ok"] is True and changed.get("committed") is True, changed
    assert _meter_call("version")["result"] == 2
    return changed


@needs_runtime
def test_undo_after_a_handoff_swap_reports_the_state_it_drops():
    changed = _swapped_with_state()
    assert changed["handoff"]["meter"]["migrated"] is True
    assert _meter_call("get", "l1")["result"] == "l1"   # carried forward
    assert changed["undo"] == {"tool": "revl_undo", "arguments": {"to": 1}}

    undone = _call(changed["undo"]["tool"], changed["undo"]["arguments"])
    assert undone["ok"] is True and undone["undone"] is True, undone
    assert _meter_call("version")["result"] == 1
    # the undo starts generation 1 cold: the labels are gone ...
    assert _meter_call("get", "l1")["result"] is None
    # ... and the dossier says so, rather than `components: []`
    dropped = undone["dossier"]["stateDropped"]
    assert dropped["components"] == ["MeterBook"], dropped
    assert "MeterBook" in dropped["note"] and "cold" in dropped["note"]
    assert "handoff" not in undone


@needs_runtime
def test_the_swaps_undo_advice_names_the_state_it_will_not_restore():
    """The forward swap's response says, next to its `undo`, that following
    it re-admits sources only: the running state is not carried back."""
    changed = _swapped_with_state()
    note = changed.get("undoNote")
    assert note and "MeterBook" in note and "revl_change" in note, changed
    assert "would be dropped" in note


@needs_runtime
def test_undo_without_a_handoff_still_names_the_dropped_state():
    """No `handoff` on either side: the forward swap started cold too, so the
    undo has nothing live to drop; a component holding state at undo time is
    still named."""
    _swapped_with_state(handoff=False)
    assert _meter_call("get", "l1")["result"] is None   # forward was cold
    assert _meter_call("log", "fresh")["ok"] is True
    undone = _call("revl_undo", {})
    assert undone["ok"] is True, undone
    assert undone["dossier"]["stateDropped"]["components"] == ["MeterBook"]


@needs_runtime
def test_undo_with_no_live_state_reports_nothing_dropped():
    """A component whose store is empty, or a stateless one, loses nothing:
    the list stays empty and no note is added."""
    assert _call("revl_load", {"source": _meter(1)})["ok"] is True
    changed = _call("revl_change", {
        "commit": True, "replace": {"component": "MeterBook",
                                    "source": _book(2)}})
    assert changed["ok"] is True, changed
    assert "undoNote" not in changed
    undone = _call("revl_undo", {})
    assert undone["ok"] is True, undone
    dropped = undone["dossier"]["stateDropped"]
    assert dropped["components"] == [] and "note" not in dropped, dropped

    _call("revl_unload", {})
    assert _call("revl_load", {"source": STATELESS})["ok"] is True
    assert _call("revl_swap", {"source": STATELESS.replace("= 1", "= 2")}
                 )["ok"] is True
    undone = _call("revl_undo", {})
    assert undone["ok"] is True, undone
    assert undone["dossier"]["stateDropped"]["components"] == []


SPAWNED = (
    "service Store { fn put(k: Str, v: Str)\n"
    "                fn get(k: Str) -> Opt[Str]\n"
    "                fn version() -> Int }\n"
    "service Admin { fn seed(k: Str, v: Str)\n"
    "                fn read(k: Str) -> Opt[Str] }\n"
    "component Worker provides store: Store {\n"
    "  let m = effect Map.new() undo m.drop()\n"
    "  provide store {\n"
    "    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }\n"
    "    fn get(k) = m.get(k)\n"
    "    fn version() = 1\n"
    "  }\n"
    "}\n"
    "component Supervisor provides admin: Admin {\n"
    "  let w = effect spawn Worker undo w.dispose()\n"
    "  provide admin {\n"
    "    fn seed(k, v) = w.store.put(k, v)\n"
    "    fn read(k) = w.store.get(k)\n"
    "  }\n"
    "}\n")


@needs_runtime
def test_undo_names_a_template_whose_live_instance_loses_its_state():
    """A swap migrates a live instance's store (item 10); the undo respawns
    the instance cold, so its template is named. The supervisor holds only
    the spawn handle, which carries no state, so it is not."""
    assert _call("revl_load", {"source": SPAWNED})["ok"] is True
    _call("revl_call", {"key": "admin", "method": "seed", "args": ["a", "1"]})
    # swapped on the session itself: `revl_swap` of a composition with a
    # template trips an unrelated blast-radius fault before it reaches the swap
    from revl import compile_source
    from revl.mcp import server as server_mod

    successor = SPAWNED.replace("fn version() = 1", "fn version() = 2")
    session = server_mod.SESSION
    session.swap(compile_source(successor, "spawned.rvl", manifest=session.ir,
                                replacing=("Worker",)),
                 origin={"source": successor}, agent_authored=False)
    read = _call("revl_call", {"key": "admin", "method": "read", "args": ["a"]})
    assert read["result"] == "1"           # migrated forward
    undone = _call("revl_undo", {})
    assert undone["ok"] is True, undone
    assert undone["dossier"]["stateDropped"]["components"] == ["Worker"]
    read = _call("revl_call", {"key": "admin", "method": "read", "args": ["a"]})
    assert read["result"] is None          # the undo started it cold


@needs_runtime
def test_an_unreadable_live_state_says_so_and_the_undo_still_runs(monkeypatch):
    """Reading live state is a report: if it raises, the undo still runs,
    lists no component it cannot see, and says why in the note."""
    from revl.mcp.session import Session

    _swapped_with_state()

    def broken(self):
        raise RuntimeError("frame registry unavailable")

    monkeypatch.setattr(Session, "live_state_components", broken)
    undone = _call("revl_undo", {})
    assert undone["ok"] is True and undone["undone"] is True, undone
    dropped = undone["dossier"]["stateDropped"]
    assert dropped["components"] == []
    assert "could not be read" in dropped["note"]
    assert "frame registry unavailable" in dropped["note"]
    assert _meter_call("version")["result"] == 1
    # the undo's own `undo` advice survives the same failure
    assert undone["undo"]["tool"] == "revl_undo" and "undoNote" not in undone


@needs_runtime
def test_a_refused_undo_keeps_the_state_and_claims_no_drop(monkeypatch):
    """A refused undo changed nothing: the dossier still plans which
    components would lose state, but carries no note saying it was dropped,
    and the state is still there."""
    from revl.errors import RevlError
    from revl.mcp import persist

    _swapped_with_state()

    def rejects(*_args, **_kwargs):
        raise RevlError("meter.rvl", 1, "this checker no longer admits it")

    monkeypatch.setattr(persist, "_recompile", rejects)
    refused = _call("revl_undo", {})
    assert refused["ok"] is False and refused["refused"] is True, refused
    dropped = refused["dossier"]["stateDropped"]
    assert dropped["components"] == ["MeterBook"]
    assert "note" not in dropped, dropped
    assert _meter_call("get", "l1")["result"] == "l1"
