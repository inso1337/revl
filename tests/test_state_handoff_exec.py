"""Verified state hand-off on hot-swap, executed on cordis-py (roadmap item 53).

The static half (grammar, lowering, the admission gate) is in
`test_state_handoff.py`. This is the *dynamic* half — it proves the exit
criterion by RUNNING it on the real runtime through the same `Session.swap` an
agent drives over the MCP bridge:

  * a stateful provider (a cache holding entries) is hot-swapped to a
    successor that declares a **compatible** `handoff`; its entries **survive**
    the swap — a warm start, not a cold restart — while a live `version()`
    proves the successor's *code* is running;
  * a swap whose successor accepts an **incompatible** hand-off shape is
    **refused at admission** with the running composition and its state
    untouched — state is never silently dropped (that would be residue);
  * a successor that declares **no** hand-off opts out: it starts cold, no
    refusal (a valid, if lossy, author choice);
  * a stateless swap carries **no** `handoff` report at all — the pre-item-53
    behaviour is byte-identical.

Set up the runtime with `sh backends/python/setup.sh`; without cordis-py these
skip (never reported as passing).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backends" / "python"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

pytest.importorskip(
    "cordis", reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")

from revl import RevlError, compile_source  # noqa: E402
from revl.mcp.session import Session  # noqa: E402


def _cache(state_type: str = "Map[Str, Str]", *, version: int = 1,
           name: str = "Cache", handoff: bool = True) -> str:
    line = f"  handoff cache: {state_type}\n" if handoff else ""
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
component {name} provides cache: Store {{
{line}  let m = effect Map.new() undo m.drop()
  provide cache {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
"""


def _loaded(src: str) -> Session:
    session = Session()
    session.load(compile_source(src, "cache.rvl"), origin={"source": src})
    return session


def test_state_crosses_to_a_compatible_successor():
    """The exit test: a cache's entries survive a hot-swap to a compatible
    successor (warm start), and the successor's code is demonstrably live."""
    session = _loaded(_cache(version=1))
    try:
        session.call("cache", "put", ["alice", "42"])
        session.call("cache", "put", ["bob", "7"])
        assert session.call("cache", "get", ["alice"])["result"] == "42"
        assert session.call("cache", "version", [])["result"] == 1  # old code

        candidate = compile_source(_cache(version=2), "cache.rvl",
                                   manifest=session.ir, replacing=("Cache",))
        state = session.swap(candidate, origin={"source": _cache(version=2)})

        # the swap reports what state it carried across
        assert state.get("handoff") == {
            "cache": {"component": "Cache", "migrated": True, "resources": 1}
        }, state.get("handoff")
        # the successor's code is live ...
        assert session.call("cache", "version", [])["result"] == 2
        # ... AND the entries survived (warm, not cold-restarted)
        assert session.call("cache", "get", ["alice"])["result"] == "42"
        assert session.call("cache", "get", ["bob"])["result"] == "7"
    finally:
        session.unload()


def test_incompatible_handoff_is_refused_at_admission_state_intact():
    """A successor that accepts an incompatible hand-off shape is refused by
    the admission gate; the running cache and its entries are untouched."""
    session = _loaded(_cache("Map[Str, Str]", version=1))
    try:
        session.call("cache", "put", ["bob", "99"])

        # admission refuses the incompatible successor — the swap never runs
        with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
            compile_source(_cache("Map[Str, Int]", version=2), "cache.rvl",
                           manifest=session.ir, replacing=("Cache",))

        # still the old provider, entries intact — nothing dropped
        assert session.call("cache", "version", [])["result"] == 1
        assert session.call("cache", "get", ["bob"])["result"] == "99"

        # a subsequent *compatible* swap still works and starts warm
        ok = compile_source(_cache(version=2), "cache.rvl",
                            manifest=session.ir, replacing=("Cache",))
        session.swap(ok, origin={"source": _cache(version=2)})
        assert session.call("cache", "version", [])["result"] == 2
        assert session.call("cache", "get", ["bob"])["result"] == "99"
    finally:
        session.unload()


def test_successor_without_handoff_starts_cold():
    """A successor that declares no `handoff` opts out of inheriting the state:
    no refusal, but the entries do not cross (a cold start)."""
    session = _loaded(_cache(version=1))
    try:
        session.call("cache", "put", ["alice", "42"])
        src2 = _cache(version=2, handoff=False)
        candidate = compile_source(src2, "cache.rvl",
                                   manifest=session.ir, replacing=("Cache",))
        state = session.swap(candidate, origin={"source": src2})
        assert "handoff" not in state           # nothing threaded
        assert session.call("cache", "version", [])["result"] == 2
        assert session.call("cache", "get", ["alice"])["result"] is None  # cold
    finally:
        session.unload()


def test_stateless_swap_reports_no_handoff():
    """Inertness: a swap of a composition whose provider declares no hand-off
    carries no `handoff` key — byte-identical to the pre-item-53 behaviour."""
    src = """
service Greeter { fn hi() -> Int }
component G provides greeter: Greeter {
  provide greeter { fn hi() = 1 }
}
"""
    src2 = src.replace("fn hi() = 1", "fn hi() = 2")
    session = _loaded(src)
    try:
        candidate = compile_source(src2, "g.rvl",
                                   manifest=session.ir, replacing=("G",))
        state = session.swap(candidate, origin={"source": src2})
        assert "handoff" not in state
        assert session.call("greeter", "hi", [])["result"] == 2
    finally:
        session.unload()
