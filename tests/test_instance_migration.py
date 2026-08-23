"""Hot-swap with live instances — state migration, executed on cordis-py.

Roadmap item 10's remaining piece (docs/design-v2-instances.md, "Not in phase
1", question 6). Phase 1 froze `spawn`: a template `T` instantiated at runtime
as a child fiber, its state living in the host resources it acquires (its
`Map`). Phase 1 explicitly left *hot-swap of a template with live instances*
undefined. This proves the definition, by RUNNING it on the real runtime
through the same `Session.swap` an agent drives over the MCP bridge:

  * spawn a live instance of `T`, mutate its state to a known value;
  * hot-swap `T` -> `T'` (a compatible successor) and assert the instance is
    now running `T'` **with its state preserved** — not restarted cold;
  * hot-swap `T` -> `T''` (an *incompatible* successor that changes the
    instance's state shape) and assert the swap is **rejected** by the
    state-compat gate and rolled back with the original state intact — the
    state is never silently dropped (that would be residue).

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

from revl import compile_source  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402


# A Worker is a spawnable template holding a Map — its migratable state. The
# Supervisor spawns one and exposes an admin service that reaches into the
# worker's private provision through the spawn handle (`w.store`): seed writes,
# read reads, ver reports which Worker version's code is live. `version` is the
# one thing that changes between `T` and `T'`, so a live `ver()` proves the
# successor's *code* is running while the migrated data proves its *state*
# survived.
def _source(worker_body: str) -> str:
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}

service Admin {{
  fn seed(k: Str, v: Str)
  fn read(k: Str) -> Opt[Str]
  fn ver() -> Int
}}

component Worker provides store: Store {{
{worker_body}
}}

component Supervisor provides admin: Admin {{
  let w = effect spawn Worker undo w.dispose()
  provide admin {{
    fn seed(k, v) = w.store.put(k, v)
    fn read(k) = w.store.get(k)
    fn ver() = w.store.version()
  }}
}}
"""


# T — version 1, one Map.
WORKER_V1 = """  let m = effect Map.new() undo m.drop()
  provide store {
    fn get(k) = m.get(k)
    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }
    fn version() = 1
  }"""

# T' — version 2, still one Map: a *compatible* successor (same state shape,
# changed code). The migration must carry the Map's entries onto it.
WORKER_V2 = """  let m = effect Map.new() undo m.drop()
  provide store {
    fn get(k) = m.get(k)
    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }
    fn version() = 2
  }"""

# T'' — version 2, but now *two* Maps: an incompatible successor. Its instance
# state shape (a 2-resource vector) cannot receive the predecessor's 1-resource
# state, so the migration must be rejected, not silently dropped.
WORKER_BAD = """  let m = effect Map.new() undo m.drop()
  let m2 = effect Map.new() undo m2.drop()
  provide store {
    fn get(k) = m.get(k)
    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }
    fn version() = 2
  }"""


def _fresh_session(worker_body: str):
    src = _source(worker_body)
    session = Session()
    session.load(compile_source(src, "mig.rvl"), origin={"source": src})
    return session


def test_state_migrates_onto_a_compatible_successor():
    """T -> T': the live instance keeps its Map entries across the swap, and is
    demonstrably running the successor's code."""
    session = _fresh_session(WORKER_V1)
    try:
        # mutate the live instance's state to a known value
        session.call("admin", "seed", ["alice", "42"])
        assert session.call("admin", "read", ["alice"])["result"] == "42"
        assert session.call("admin", "ver", [])["result"] == 1  # running T

        # hot-swap the template with a live instance of it
        state = session.swap(compile_source(_source(WORKER_V2), "mig.rvl"),
                             origin={"source": _source(WORKER_V2)})

        # the swap reports the migration it performed
        assert state.get("migration") == {
            "policy": "generational",
            "templates": {"Worker": {"instances": 1, "migrated": True, "resources": 1}},
        }, state.get("migration")
        # running T' now ...
        assert session.call("admin", "ver", [])["result"] == 2
        # ... AND the instance's state survived the swap (not cold-restarted)
        assert session.call("admin", "read", ["alice"])["result"] == "42"
    finally:
        session.unload()


def test_incompatible_successor_is_rejected_and_rolled_back():
    """T -> T'': the successor changes the instance's state shape, so the
    state-compat gate refuses the swap and rolls back — the original instance,
    its code, and its state are all intact. State is never dropped."""
    session = _fresh_session(WORKER_V1)
    try:
        session.call("admin", "seed", ["bob", "99"])
        assert session.call("admin", "read", ["bob"])["result"] == "99"

        with pytest.raises(SessionError, match="cannot migrate|state-compat|residue"):
            session.swap(compile_source(_source(WORKER_BAD), "mig.rvl"),
                         origin={"source": _source(WORKER_BAD)})

        # rolled back: still T (version 1), state intact — nothing dropped
        assert session.call("admin", "ver", [])["result"] == 1
        assert session.call("admin", "read", ["bob"])["result"] == "99"
        # a subsequent *compatible* swap still works after the rejection
        session.swap(compile_source(_source(WORKER_V2), "mig.rvl"),
                     origin={"source": _source(WORKER_V2)})
        assert session.call("admin", "ver", [])["result"] == 2
        assert session.call("admin", "read", ["bob"])["result"] == "99"
    finally:
        session.unload()


def test_a_swap_with_no_live_instances_reports_no_migration():
    """Inertness: a swap of a composition that spawns nothing carries no
    migration key — the byte-identical pre-item-10 behaviour."""
    src = """
service Greeter { fn hi() -> Int }
component G provides greeter: Greeter {
  provide greeter { fn hi() = 1 }
}
"""
    src2 = src.replace("fn hi() = 1", "fn hi() = 2")
    session = Session()
    session.load(compile_source(src, "plain.rvl"), origin={"source": src})
    try:
        state = session.swap(compile_source(src2, "plain.rvl"),
                             origin={"source": src2})
        assert "migration" not in state
        assert session.call("greeter", "hi", [])["result"] == 2
    finally:
        session.unload()


# ---------------------------------------------------------------------------
# CHARACTERIZATION of a KNOWN, UNCLOSED hazard: same-typed positional collision.
# The state-compat gate checks same-LENGTH / same-TYPE-per-position; it does NOT
# check that a same-typed resource keeps its ROLE. A successor that REORDERS two
# same-typed resources (Maps `a` and `b`, acquired in the opposite order) PASSES
# the gate, and positional correlation migrates each one's state into the OTHER's
# slot — a SILENT WRONG-STATE migration. Fix = stable-key correlation + a declared
# `handoff` surface (roadmap item 53). This test PINS the current wrong behaviour;
# when correlation becomes key-based it will FAIL, which is the signal to close it.
# See docs/design-v2-instances.md, "reorder hazard".
# ---------------------------------------------------------------------------

_TWO_MAP_SRC = """
service TwoStore {
  fn getA(k: Str) -> Opt[Str]
  fn putA(k: Str, v: Str)
  fn getB(k: Str) -> Opt[Str]
  fn putB(k: Str, v: Str)
}
service Admin2 {
  fn seedA(v: Str)
  fn seedB(v: Str)
  fn readA() -> Opt[Str]
  fn readB() -> Opt[Str]
}
component Worker provides store: TwoStore {
%s
  provide store {
    fn getA(k) = a.get(k)
    fn putA(k, v) { effect a.insert(k, v) undo a.remove(k) }
    fn getB(k) = b.get(k)
    fn putB(k, v) { effect b.insert(k, v) undo b.remove(k) }
  }
}
component Supervisor provides admin: Admin2 {
  let w = effect spawn Worker undo w.dispose()
  provide admin {
    fn seedA(v) = w.store.putA("k", v)
    fn seedB(v) = w.store.putB("k", v)
    fn readA() = w.store.getA("k")
    fn readB() = w.store.getB("k")
  }
}
"""

_ORDER_AB = "  let a = effect Map.new() undo a.drop()\n  let b = effect Map.new() undo b.drop()"
_ORDER_BA = "  let b = effect Map.new() undo b.drop()\n  let a = effect Map.new() undo a.drop()"


def test_reorder_hazard_same_typed_resources_migrate_to_the_wrong_slot():
    """KNOWN hazard, pinned: two same-typed resources reordered in the successor
    pass the state-compat gate but cross their state under positional migration.
    Asserts the WRONG (crossed) result so a future stable-key fix trips this."""
    session = Session()
    src_ab = _TWO_MAP_SRC % _ORDER_AB
    session.load(compile_source(src_ab, "mig.rvl"), origin={"source": src_ab})
    try:
        session.call("admin", "seedA", ["A-STATE"])
        session.call("admin", "seedB", ["B-STATE"])
        assert session.call("admin", "readA", [])["result"] == "A-STATE"
        assert session.call("admin", "readB", [])["result"] == "B-STATE"

        src_ba = _TWO_MAP_SRC % _ORDER_BA
        session.swap(compile_source(src_ba, "mig.rvl"), origin={"source": src_ba})

        # THE HAZARD: gate passed ([Map, Map] == [Map, Map]) but positional
        # correlation crossed the state — each store now reads the OTHER's value.
        assert session.call("admin", "readA", [])["result"] == "B-STATE"
        assert session.call("admin", "readB", [])["result"] == "A-STATE"
    finally:
        session.unload()
