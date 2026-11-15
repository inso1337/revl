"""Issue #1703: every mutating MCP response carries its exact undo, and
`revl_step_back` with no arguments reverts the last change.

Exit tests, as the issue states them:

* for each mutating verb, executing the `undo` its response carried restores
  the prior generation byte for byte: the snapshot's `sources` and `manifest`
  (the inputs a re-admission is given) are equal before the call and after
  the undo, and an unload's undo puts back exactly what ran;
* `revl_step_back` with no arguments reverts the last change, and each further
  one the change before it.

A verb with no exact inverse answers `undo: null` with the reason
(`undo_record.IRREVERSIBLE`), and the classification test below fails on a
mutating verb that is in neither set, so a new verb cannot ship without one.

The composition tests drive a live session, so they need the cordis-py
runtime; the gate is a per-test marker, never a module-level skip, so the
classification test runs everywhere.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import operator as op  # noqa: E402
from revl.mcp import undo_record  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="an undo drives a live composition; install the runtime with "
           "`sh backends/python/setup.sh`")

CACHE = (
    "service Cache { fn get(key: Str) -> Opt[Str]\n"
    "                fn size() -> Int }\n"
    "component MemCache provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide cache { fn get(key) = store.get(key)\n"
    "                  fn size() = 0 }\n"
    "}\n"
)
SIZED = CACHE.replace("fn size() = 0", "fn size() = 2")
RESIZED = CACHE.replace("fn size() = 0", "fn size() = 3")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _state() -> str | None:
    """The byte-exact identity of what runs: the snapshot's sources and
    manifest (meta carries timestamps and the generation counter), or None
    when nothing is loaded."""
    from revl.mcp import server as server_mod

    if not server_mod.SESSION.loaded:
        return None
    snap = server_mod.SESSION.snapshot()
    return json.dumps({"sources": snap["sources"], "manifest": snap["manifest"]},
                      sort_keys=True)


def _undo(payload: dict) -> dict:
    undo = payload["undo"]
    assert undo is not None, payload.get("undoReason")
    return _call(undo["tool"], undo["arguments"])


@pytest.fixture(autouse=True)
def _fresh_server_session():
    """The MCP surface shares one module-level session and undo stack."""
    from revl.mcp import server as server_mod

    server_mod.UNDO_STACK.clear()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.UNDO_STACK.clear()


# ------------------------------------------------- the classification

# Verbs the operator gate holds as AUTHORITY although they mutate nothing:
# `revl_snapshot` hands back a document, `revl_approvals` (issue #2991) reads
# the approval tickets, their blast radius and history. Neither has an undo
# field at all. A new entry here is a decision, checked below against the
# advertised read-only annotation.
GATED_READ_ONLY = {"revl_snapshot", "revl_approvals"}


def test_every_mutating_verb_is_classified():
    """A mutating verb (one the operator gate holds) either has an exact
    inverse or names why it has none. The gated read-only verbs mutate
    nothing, so they answer with no undo field at all."""
    mutating = (set(op.TOOL_VERB) | set(op.COMPOSED_TOOL_VERB)) - GATED_READ_ONLY
    reversible = undo_record.GENERATION_VERBS | {"revl_lease"}
    assert not reversible & set(undo_record.IRREVERSIBLE)
    assert mutating - reversible - set(undo_record.IRREVERSIBLE) == set()
    assert all(reason.strip() for reason in undo_record.IRREVERSIBLE.values())


def test_the_gated_read_only_verbs_are_exactly_the_read_only_annotated_ones():
    """The exemption above is not a free pass: it is exactly the gated verbs
    the server advertises as read-only and that carry no irreversible-undo
    reason (`revl_commit` and `revl_fork` are annotated read-only but record a
    step with no inverse, so they stay in `IRREVERSIBLE`)."""
    from revl.mcp import server as server_mod

    gated = set(op.TOOL_VERB) | set(op.COMPOSED_TOOL_VERB)
    read_only = {tool["name"] for tool in server_mod.TOOLS
                 if (tool.get("annotations") or {}).get("readOnlyHint") is True}
    assert gated & (read_only - set(undo_record.IRREVERSIBLE)) == GATED_READ_ONLY
    reversible = undo_record.GENERATION_VERBS | {"revl_lease"}
    assert not GATED_READ_ONLY & (reversible | set(undo_record.IRREVERSIBLE))


def test_the_approval_query_surface_answers_with_no_undo_field():
    """`revl_approvals` reads and writes nothing (issue #2991): each action
    answers with no undo field, like `revl_snapshot`."""
    for action in ("list", "stats"):
        out = _call("revl_approvals", {"action": action})
        assert out["ok"] is True, out
        assert "undo" not in out and "undoReason" not in out


@needs_runtime
def test_step_back_with_nothing_to_revert_is_refused_with_depth_zero():
    out = _call("revl_step_back", {})
    assert out["ok"] is False and out["refused"] is True
    assert out["undoDepth"] == 0
    assert "nothing to step back" in out["diagnostics"][0]["message"]


@needs_runtime
def test_step_back_with_arguments_but_no_to_still_requires_it():
    out = _call("revl_step_back", {"component": "MemCache"})
    assert out["ok"] is False
    assert "`to` is required" in out["diagnostics"][0]["message"]


def test_without_the_runtime_step_back_is_the_runtime_gates_refusal(monkeypatch):
    """`revl_step_back` is a runtime verb (issue #1692's measured list): on a
    server that cannot import cordis nothing can have been loaded, so the
    runtime gate's refusal, which names the fix, answers before the argument
    checks and the undo stack are consulted."""
    from revl.mcp import server as server_mod

    monkeypatch.setattr(server_mod, "_RUNTIME_AVAILABLE", False)
    out = _call("revl_step_back", {})
    assert out["ok"] is False and out["refused"] is True
    assert out["unavailable"] == "cordis-py runtime"
    assert "undoDepth" not in out


# ------------------------------------------- each verb's undo is exact

@needs_runtime
def test_load_undoes_to_nothing_loaded():
    out = _call("revl_load", {"source": CACHE})
    assert out["undo"] == {"tool": "revl_unload", "arguments": {}}
    assert out["undoDepth"] == 1
    assert _undo(out)["ok"] is True
    assert _state() is None


@needs_runtime
def test_swap_undo_restores_the_prior_generation_byte_for_byte():
    _call("revl_load", {"source": CACHE})
    before = _state()
    out = _call("revl_swap", {"source": SIZED})
    assert out["ok"] is True and _state() != before
    assert out["undo"]["tool"] == "revl_undo"
    assert _undo(out)["ok"] is True
    assert _state() == before
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0


@needs_runtime
def test_edit_undo_restores_the_composition_and_the_working_buffer():
    _call("revl_load", {"source": CACHE})
    before = _state()
    out = _call("revl_edit", {"edits": [{"anchor": "fn size() = 0",
                                         "replacement": "fn size() = 5"}]})
    assert out["ok"] is True and _state() != before
    assert _undo(out)["ok"] is True
    assert _state() == before
    # the buffer an edit is computed against is back at the earlier source too
    again = _call("revl_edit", {"edits": [{"anchor": "fn size() = 0",
                                           "replacement": "fn size() = 6"}]})
    assert again["ok"] is True, again


@needs_runtime
def test_undo_and_rollback_are_undone_too():
    _call("revl_load", {"source": CACHE})
    _call("revl_swap", {"source": SIZED})
    sized = _state()
    undone = _call("revl_undo", {})
    assert undone["ok"] is True and _state() != sized
    assert _undo(undone)["ok"] is True
    assert _state() == sized
    _call("revl_swap", {"source": RESIZED})
    resized = _state()
    rolled = _call("revl_rollback", {})
    assert rolled["ok"] is True and _state() != resized
    assert _undo(rolled)["ok"] is True
    assert _state() == resized


@needs_runtime
def test_unload_undo_re_admits_exactly_what_ran():
    _call("revl_load", {"source": SIZED})
    before = _state()
    out = _call("revl_unload", {})
    assert out["ok"] is True and _state() is None
    assert out["undo"]["tool"] == "revl_restore"
    assert _undo(out)["ok"] is True
    assert _state() == before


@needs_runtime
def test_restore_undoes_to_nothing_loaded():
    _call("revl_load", {"source": CACHE})
    snap = _call("revl_snapshot", {})
    assert "undo" not in snap          # a snapshot mutates nothing
    _call("revl_unload", {})
    out = _call("revl_restore", {"snapshot": snap["snapshot"]})
    assert out["undo"] == {"tool": "revl_unload", "arguments": {}}
    assert _undo(out)["ok"] is True and _state() is None


@needs_runtime
def test_a_fresh_lease_claim_undoes_to_its_release():
    from revl.mcp import server as server_mod

    _call("revl_load", {"source": CACHE})
    out = _call("revl_lease", {"component": "MemCache"})
    assert out["undo"] == {"tool": "revl_lease",
                           "arguments": {"action": "release",
                                         "component": "MemCache"}}
    renewed = _call("revl_lease", {"action": "renew", "component": "MemCache"})
    assert renewed["undo"] is None and "renewal" in renewed["undoReason"]
    assert _undo(out)["ok"] is True
    assert server_mod.SESSION.leases.holder_of("MemCache") is None


@needs_runtime
def test_a_crossing_answers_undo_null_with_the_reason():
    _call("revl_load", {"source": CACHE})
    out = _call("revl_call", {"key": "cache", "method": "size"})
    assert out["ok"] is True
    assert out["undo"] is None
    assert out["undoReason"] == undo_record.IRREVERSIBLE["revl_call"]


@needs_runtime
def test_a_refused_call_carries_no_undo():
    _call("revl_load", {"source": CACHE})
    out = _call("revl_swap", {"source": CACHE.replace("fn size() = 0",
                                                      'fn size() = "x"')})
    assert out["ok"] is False
    assert "undo" not in out


# ------------------------------------- step_back with no arguments

@needs_runtime
def test_step_back_walks_back_one_change_at_a_time():
    _call("revl_load", {"source": CACHE})
    loaded = _state()
    _call("revl_swap", {"source": SIZED})
    sized = _state()
    _call("revl_swap", {"source": RESIZED})

    back = _call("revl_step_back", {})
    assert back["ok"] is True and back["steppedBack"] is True
    assert back["change"]["tool"] == "revl_swap"
    assert back["redo"] == {"tool": "revl_swap", "arguments": {"source": RESIZED}}
    assert back["undoDepth"] == 2
    assert _state() == sized

    assert _call("revl_step_back", {})["undoDepth"] == 1
    assert _state() == loaded
    assert _call("revl_step_back", {})["undoDepth"] == 0
    assert _state() is None
    assert _call("revl_step_back", {})["refused"] is True


@needs_runtime
def test_step_back_skips_irreversible_calls_and_reverts_the_last_change():
    _call("revl_load", {"source": CACHE})
    before = _state()
    _call("revl_swap", {"source": SIZED})
    _call("revl_call", {"key": "cache", "method": "size"})   # undo: null
    back = _call("revl_step_back", {})
    assert back["change"]["tool"] == "revl_swap"
    assert _state() == before
