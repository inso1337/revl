"""Generation history and operator undo (roadmap item 65).

`revl_rollback` was depth-1: a single `previous` slot, gone the moment a second
change landed. This deepens it into what item 15's persistence makes cheap —
every admitted change (load, swap, apply) appends a *generation snapshot*, and
`revl undo` returns to N−1, `revl undo --to <gen>` to any still-retained
generation.

The load-bearing properties under test:

* **the history is deep** — a sequence of swaps leaves every generation
  retained, not just the last one;
* **an undo is itself a gated change** — the target's sources are re-admitted
  through the *same* compile+admission gate a live swap runs; a target the
  current checker rejects is refused (a *result*, with the diagnostic) and the
  running composition is untouched — an undo never bypasses the gate;
* **the dossier is honest in reverse** — it names what unloads, what state
  drops, and the interim boundary crossings that no undo can un-emit
  (compensation is not inversion, paper §6.1).

The dossier/crossing math and the gate-refusal are frontend where they can be,
but an undo runs *against a live composition*, so most of this needs the
cordis-py runtime. The cordis gate is a per-test marker (`@needs_runtime`),
never a module-level `importorskip`, so the runtime-free checks still get
collected and counted where cordis is absent.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp import persist  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

# a small composition that crosses the boundary (emits), so an undo has an
# authority surface to enumerate. Db provides an `emission` service PgCache
# emits through; the crossing token is `emit:PgCache:db.execute`.
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
component Db provides db: Database { provide db { fn execute(sql) = 0 } }
service Cache { emission[db] fn put(key: Str, value: Str) -> Int }
component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) = emit db.execute(`INSERT`) }
}
"""

# generation 2 adds a Front component that emits `cache.put` — a NEW boundary
# crossing (`emit:Front:cache.put`) the base generation does not have.
WITH_FRONT = BASE + """
component Front requires cache: Cache { emit cache.put("k", "v") }
"""

# generation 3: change PgCache's emitted payload only — same crossing surface.
RETUNED = WITH_FRONT.replace("`INSERT`", "`UPSERT`")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_server_session():
    """The MCP surface shares one module-level session; leave it empty."""
    from revl.mcp import server as server_mod

    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


# --------------------------------------------------------- frontend-only checks

def test_the_undo_verb_is_registered_and_gated():
    """The verb exists, takes an optional `to`, and is marked destructive."""
    from revl.mcp import server as server_mod

    tool = next(t for t in server_mod.TOOLS if t["name"] == "revl_undo")
    assert "to" in tool["inputSchema"]["properties"]
    assert tool["annotations"]["destructiveHint"] is True
    # the description names the invariant, so an agent reading the schema knows
    assert "gate" in tool["description"].lower()


def test_undo_on_an_empty_session_is_a_clean_error():
    payload = _call("revl_undo", {})
    assert payload["ok"] is False
    assert "nothing is loaded" in payload["diagnostics"][0]["message"]


# --------------------------------------------------------------- the live path
#
# Everything below boots real code into a cordis-py Context. Gate it per test.

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the undo path drives a live composition — install the runtime with "
           "`sh backends/python/setup.sh`")


def _cross(ir) -> list[str]:
    from revl.audit_diff import audit_report, crossings

    return sorted(crossings(audit_report(ir)))


def _built_history() -> Session:
    """A session with three retained generations: base, +Front, retuned."""
    session = Session()
    session.load(compile_source(BASE, "a.rvl"), origin={"source": BASE})
    session.swap(compile_source(WITH_FRONT, "a.rvl"), origin={"source": WITH_FRONT})
    session.swap(compile_source(RETUNED, "a.rvl"), origin={"source": RETUNED})
    return session


@needs_runtime
def test_every_admitted_change_appends_a_generation():
    session = _built_history()
    try:
        # not depth-1: all three generations are retained, in order
        assert [e["generation"] for e in session._history] == [1, 2, 3]
        assert session._generation == 3
        state = session.state()
        assert state["history"] == [1, 2, 3]
        assert state["canUndo"] is True
        # each entry carries a re-admittable snapshot (item 15's persist bundle)
        assert all(e["snapshot"] is not None for e in session._history)
    finally:
        session.unload()


@needs_runtime
def test_undo_returns_to_n_minus_one_through_the_gate():
    session = _built_history()
    try:
        result = session.undo()
        assert result["undone"] is True
        assert result["toGeneration"] == 2  # N−1
        # the undo IS a new generation (git-revert of a change is a change)
        assert result["generation"] == 4
        assert [e["generation"] for e in session._history] == [1, 2, 3, 4]
        # and the live composition now serves the N−1 shape (Front is back)
        assert "Front" in [c["name"] for c in session.state()["components"]]
    finally:
        session.unload()


@needs_runtime
def test_undo_to_reaches_a_specific_retained_generation():
    session = _built_history()
    try:
        result = session.undo(to=1)
        assert result["undone"] is True
        assert result["toGeneration"] == 1
        # generation 1 had no Front — the undo unloaded it
        names = [c["name"] for c in session.state()["components"]]
        assert "Front" not in names
        assert result["dossier"]["unloads"] == ["Front"]
    finally:
        session.unload()


@needs_runtime
def test_undo_to_an_unretained_generation_is_refused():
    session = _built_history()
    try:
        payload = _tool_or_session_error(session, to=99)
        assert "not in the retained history" in payload
    finally:
        session.unload()


def _tool_or_session_error(session, to):
    from revl.mcp.session import SessionError

    try:
        session.undo(to=to)
    except SessionError as error:
        return str(error)
    return ""


@needs_runtime
def test_the_dossier_names_unemittable_interim_crossings():
    """Undoing to generation 1 gives up `emit:Front:cache.put` going forward —
    but that crossing was reachable while generations 2/3 were live, and undoing
    the code cannot un-emit it. The dossier enumerates it honestly."""
    session = _built_history()
    try:
        result = session.undo(to=1)
        crossings = result["dossier"]["unemittableCrossings"]
        # the interim generations (2, 3) are enumerated with their surfaces
        assert [g["generation"] for g in crossings["interim"]] == [2, 3]
        # Front's crossing is authority relinquished, yet already exercised
        assert "emit:Front:cache.put" in crossings["givenUp"]
        # PgCache's crossing survives into the target — the target still reaches it
        assert "emit:PgCache:db.execute" in crossings["persisting"]
        # every interim crossing is in the un-emittable union
        assert set(crossings["givenUp"]) | set(crossings["persisting"]) \
            == set(crossings["crossings"])
        assert "compensation is not inversion" in crossings["note"]
    finally:
        session.unload()


@needs_runtime
def test_an_undo_is_itself_gated_a_rejected_target_is_refused(monkeypatch):
    """The heart of item 65: an undo re-admits the target's sources through the
    SAME gate a swap runs. Simulate a stricter checker that now rejects the
    base generation's source; the undo to it must be refused — a *result*, with
    the diagnostic — and the running composition left untouched, never a
    bypass."""
    session = _built_history()
    try:
        real = persist.compile_source

        def stricter(source, filename="<string>", **kwargs):
            if "component PgCache" in source and "Front" not in source:
                raise RevlError(filename, 1,
                                "`PgCache` uses a construct this checker no "
                                "longer admits (G4)")
            return real(source, filename, **kwargs)

        monkeypatch.setattr(persist, "compile_source", stricter)

        before = session._generation
        result = session.undo(to=1)
        assert result["undone"] is False
        assert result["refused"] is True
        assert "no longer admits" in result["reason"]
        assert result["diagnostics"][0]["message"]
        # the dossier is still computed, so the operator sees what was refused
        assert result["dossier"]["toGeneration"] == 1
        # untouched: same generation running, Front still present (gen-3 shape)
        assert session._generation == before
        assert "Front" in [c["name"] for c in session.state()["components"]]
    finally:
        session.unload()


@needs_runtime
def test_mcp_undo_verb_round_trips_and_reports_refusals():
    _call("revl_load", {"source": BASE})
    _call("revl_swap", {"source": WITH_FRONT})
    # a normal undo: ok, with a dossier
    payload = _call("revl_undo", {})
    assert payload["ok"] is True
    assert payload["undone"] is True
    assert payload["toGeneration"] == 1
    assert "unemittableCrossings" in payload["dossier"]


@needs_runtime
def test_state_below_two_generations_cannot_undo():
    session = Session()
    session.load(compile_source(BASE, "a.rvl"), origin={"source": BASE})
    try:
        assert session.state()["canUndo"] is False
        from revl.mcp.session import SessionError
        with pytest.raises(SessionError) as excinfo:
            session.undo()
        assert "no earlier generation" in str(excinfo.value)
    finally:
        session.unload()


@needs_runtime
def test_undo_to_a_generation_aged_out_of_the_bounded_history(monkeypatch):
    """Retention is bounded: the oldest generations age out, and an undo to one
    that is gone is refused honestly rather than reaching for it."""
    monkeypatch.setattr("revl.mcp.session.HISTORY_LIMIT", 2)
    session = Session()
    session.load(compile_source(BASE, "a.rvl"), origin={"source": BASE})
    session.swap(compile_source(WITH_FRONT, "a.rvl"), origin={"source": WITH_FRONT})
    session.swap(compile_source(RETUNED, "a.rvl"), origin={"source": RETUNED})
    try:
        # only the last two generations survive the bound
        assert [e["generation"] for e in session._history] == [2, 3]
        assert _tool_or_session_error(session, to=1) != ""
        assert "aged out" in _tool_or_session_error(session, to=1)
    finally:
        session.unload()


@needs_runtime
def test_cli_undo_replays_a_history_document_and_reverts(tmp_path, capsys):
    """The additive `revl undo` CLI: replay a generation-history document into a
    fresh session, then revert through the gate. Exercises the whole path from
    the command line."""
    from revl.__main__ import main

    session = _built_history()
    doc = session.history_document()
    session.unload()
    path = tmp_path / "history.json"
    path.write_text(json.dumps(doc))

    rc = main(["undo", str(path), "--to", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "undone: generation" in out
    # the un-emittable crossing is named in the CLI report
    assert "emit:Front:cache.put" in out
    assert "compensation is not inversion" in out
    assert "torn down — no residue: True" in out
