"""Composition persistence: snapshot an evolved composition, restore it.

The feature adds durability to the live MCP session (roadmap item 15). A
composition an agent grows at runtime through the admission gate lived only in
memory; snapshot/restore lets it survive a restart.

Two properties are under test. First, the round trip: a composition (including
one evolved by a swap) can be snapshotted to plain JSON and re-admitted into a
*fresh* session, with the same components admitted and the system answering
the same way. Second — the load-bearing one — that restore **replays
admission** rather than rehydrating: a snapshot whose component the current
(simulated newer) checker rejects fails the restore *loudly*, with the
diagnostic, and loads nothing. A save file cannot smuggle a now-rejected
component past a newer checker.

Only the round-trip half needs the cordis-py runtime (it boots real code). The
gate-replay half is pure frontend — a rejected component fails at *compile*,
before any runtime is touched — so it runs everywhere. The cordis gate is a
per-test marker (`@needs_runtime`), never a module-level `importorskip`, so the
runtime-free tests still get collected and counted where cordis is absent.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.errors import RevlError  # noqa: E402
from revl.mcp import persist  # noqa: E402
from revl.mcp.persist import RestoreError  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402

# a self-contained composition an agent might load
CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

CACHE_V2 = CACHE.replace("fn size() = 0", "fn size() = 42")

# a rejected variant: `size` must return Int, this returns a Str
CACHE_BROKEN = CACHE.replace("fn size() = 0", 'fn size() = "nope"')


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


# ---------------------------------------------------- the gate-replay invariant
#
# These need no runtime: a component the current checker rejects fails at
# COMPILE, inside restore's re-admission, before anything is loaded.

def test_restore_of_a_rejected_component_fails_loudly_and_loads_nothing():
    # a snapshot whose source the current checker refuses — as if it had been
    # taken under an older, more permissive checker
    snap = {"sources": {"source": CACHE_BROKEN},
            "manifest": {}, "meta": {"components": ["MemCache"]}}
    session = Session()
    with pytest.raises(RestoreError) as excinfo:
        session.restore(snap)
    # the diagnostic is surfaced, not swallowed
    assert excinfo.value.diagnostic is not None
    assert "size" in excinfo.value.diagnostic["message"]
    # and nothing slipped through
    assert session.loaded is False


def test_restore_tool_reports_the_rejection_as_a_result():
    payload = _call("revl_restore", {"snapshot": {
        "sources": {"source": CACHE_BROKEN},
        "manifest": {}, "meta": {"components": ["MemCache"]}}})
    assert payload["ok"] is False
    assert payload["restored"] is False
    assert payload["reAdmitted"] is False
    assert "size" in payload["diagnostics"][0]["message"]
    from revl.mcp import server as server_mod
    assert server_mod.SESSION.loaded is False


def test_a_valid_snapshot_is_refused_when_a_newer_checker_rejects_it(monkeypatch):
    """The heart of the invariant. A snapshot that WAS admittable (its sources
    compile fine today) is still refused once the checker changes to reject the
    component — because restore recompiles through the gate rather than trusting
    the stored manifest. Simulate the newer checker by making the compile
    entry point restore uses reject that source."""
    good_snapshot = {"sources": {"source": CACHE},
                     "manifest": {"loadOrder": ["MemCache"],
                                  "components": [{"name": "MemCache"}]},
                     "meta": {"components": ["MemCache"]}}

    # sanity: today's checker accepts it (pure frontend compile, no runtime)
    assert persist._recompile(good_snapshot["sources"]) is not None

    real = persist.compile_source

    def stricter(source, filename="<string>", **kwargs):
        if "component MemCache" in source:
            raise RevlError(filename, 1,
                            "`MemCache` uses a construct this checker no longer "
                            "admits (G4)")
        return real(source, filename, **kwargs)

    monkeypatch.setattr(persist, "compile_source", stricter)

    session = Session()
    with pytest.raises(RestoreError) as excinfo:
        session.restore(good_snapshot)
    assert "no longer admits" in excinfo.value.diagnostic["message"]
    assert session.loaded is False


def test_restore_refuses_when_a_composition_is_already_loaded():
    session = Session()
    session.origin = {"source": CACHE}  # pretend something is loaded
    session._driver = object()
    try:
        with pytest.raises(SessionError) as excinfo:
            session.restore({"sources": {"source": CACHE}, "meta": {}})
        assert "already loaded" in str(excinfo.value)
    finally:
        session._driver = None


def test_snapshot_of_an_empty_session_is_a_clean_error():
    payload = _call("revl_snapshot", {})
    assert payload["ok"] is False
    assert "nothing is loaded" in payload["diagnostics"][0]["message"]


def test_restore_tool_requires_a_snapshot_argument():
    payload = _call("revl_restore", {})
    assert payload["ok"] is False
    assert "`snapshot`" in payload["diagnostics"][0]["message"]


# ------------------------------------------------------------ the round trip
#
# Everything below boots real code into a cordis-py Context. Gate it per test:
# module-level `importorskip` would skip the whole file during collection and
# take the runtime-free tests above down with it.

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the round-trip tests need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`, then run this file under "
           "`backends/python/.venv/bin/pytest`",
)


@needs_runtime
def test_snapshot_shape_captures_sources_manifest_and_meta():
    loaded = _call("revl_load", {"source": CACHE})
    assert loaded["ok"] is True
    snap = _call("revl_snapshot", {})["snapshot"]

    assert snap["sources"]["source"] == CACHE
    assert snap["manifest"]["loadOrder"] == ["MemCache"]
    assert snap["meta"]["components"] == ["MemCache"]
    assert snap["meta"]["snapshotVersion"] == persist.SNAPSHOT_VERSION
    # plain JSON, nothing exotic
    assert json.loads(json.dumps(snap)) == snap


@needs_runtime
def test_snapshot_then_restore_into_a_fresh_session_admits_the_same_components():
    _call("revl_load", {"source": CACHE})
    snap = _call("revl_snapshot", {})["snapshot"]
    # tear the original down so the restore is genuinely into an empty session
    _call("revl_unload", {})

    restored = _call("revl_restore", {"snapshot": snap})
    assert restored["ok"] is True
    assert restored["restored"] is True
    assert restored["reAdmitted"] is True
    assert restored["loadOrder"] == ["MemCache"]
    assert [c["name"] for c in restored["components"]] == ["MemCache"]
    # the restored composition actually answers
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0


@needs_runtime
def test_round_trip_survives_a_restart_a_brand_new_session_object():
    # snapshot from one session, serialize to JSON (the "save"), then restore
    # into a completely separate Session — the process-restart shape
    origin = Session()
    origin.load(_compile(CACHE), origin={"source": CACHE})
    saved = json.dumps(origin.snapshot())
    origin.unload()

    reborn = Session()
    try:
        reborn.restore(json.loads(saved))
        assert reborn.loaded is True
        result = reborn.call("cache", "size")["result"]
        assert result == 0
        # the reborn session can be snapshotted again — a round trip stays one
        again = reborn.snapshot()
        assert again["sources"]["source"] == CACHE
    finally:
        if reborn.loaded:
            reborn.unload()


@needs_runtime
def test_an_evolved_composition_restores_the_swapped_generation_not_the_first():
    # load, then evolve by swapping in a new behaviour (size() = 42)
    _call("revl_load", {"source": CACHE})
    swapped = _call("revl_swap", {"source": CACHE_V2})
    assert swapped["swapped"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 42

    # the snapshot must capture the EVOLVED sources, not the ones first loaded
    snap = _call("revl_snapshot", {})["snapshot"]
    assert "fn size() = 42" in snap["sources"]["source"]
    _call("revl_unload", {})

    _call("revl_restore", {"snapshot": snap})
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 42


@needs_runtime
def test_restore_reenables_recording_when_the_snapshot_had_it_on():
    _call("revl_load", {"source": CACHE, "record": True})
    snap = _call("revl_snapshot", {})["snapshot"]
    assert snap["meta"]["record"] is True
    _call("revl_unload", {})

    restored = _call("revl_restore", {"snapshot": snap})
    assert restored["recording"] is True


@needs_runtime
def test_files_based_snapshot_is_self_contained_text(tmp_path):
    # a composition admitted from a file: the snapshot must carry the file's
    # TEXT, so it restores even after the file is gone
    path = tmp_path / "cache.rvl"
    path.write_text(CACHE)
    session = Session()
    session.load(_compile_files([str(path)]),
                 origin={"files": [str(path)]})
    snap = session.snapshot()
    session.unload()
    path.unlink()  # the source file is gone now

    assert snap["sources"]["files_content"][str(path)] == CACHE
    reborn = Session()
    try:
        reborn.restore(snap)
        assert reborn.call("cache", "size")["result"] == 0
    finally:
        if reborn.loaded:
            reborn.unload()


# -- helpers that need the frontend only ----------------------------------

def _compile(source: str) -> dict:
    from revl import compile_source
    return compile_source(source)


def _compile_files(paths: list) -> dict:
    from revl import compile_files
    return compile_files(paths)
