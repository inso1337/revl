"""Issue #2673: a declared successor must carry a re-declared live template.

The item-334 disclosure gate refuses an agent-authored successor that CHANGES a
template with live instances (`STATE_UNDISCLOSED`). It deliberately admits one
that re-declares the template with the same code (#2423 follow-up), on the
grounds that the running instance's state is re-seated on the operator's own
text. The `declared` migration policy captured no instance state, so that
admitted successor came up cold and the operator's state was silently dropped.

These tests pin the three sides of the choice:

* a declared successor with a live instance carries the state across (the
  capture runs under `declared` too),
* a declared successor with no live instance still lands cold and reports no
  migration (behaviour unchanged),
* the generational path still migrates, with its own report.

The declared path is driven through `Gate.propose`, the verb the issue names;
the generational path through `Session.swap`, its public entry.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap drives a live composition; needs cordis-py "
           "(the pin in backends/python/setup.sh)",
)


def _instances_source(version: int) -> str:
    """`Worker` is a spawn target (a template) holding a live `Map`; the
    unchanged `Supervisor` spawns it and exposes seed/read/ver through the
    spawn handle, so a live read proves the instance state survived the swap
    while `version` proves which code is running."""
    return (
        "service Store {\n"
        "  fn get(k: Str) -> Opt[Str]\n"
        "  fn put(k: Str, v: Str)\n"
        "  fn version() -> Int\n"
        "}\n"
        "service Admin {\n"
        "  fn seed(k: Str, v: Str)\n"
        "  fn read(k: Str) -> Opt[Str]\n"
        "  fn ver() -> Int\n"
        "}\n"
        "component Worker provides store: Store {\n"
        "  let m = effect Map.new() undo m.drop()\n"
        "  provide store {\n"
        "    fn get(k) = m.get(k)\n"
        "    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }\n"
        f"    fn version() = {version}\n"
        "  }\n"
        "}\n"
        "component Supervisor provides admin: Admin {\n"
        "  let w = effect spawn Worker undo w.dispose()\n"
        "  provide admin {\n"
        "    fn seed(k, v) = w.store.put(k, v)\n"
        "    fn read(k) = w.store.get(k)\n"
        "    fn ver() = w.store.version()\n"
        "  }\n"
        "}\n"
    )


def _echo_source() -> str:
    """A composition with no template at all. Switching to it DROPS the live
    template."""
    return (
        "service Ping { fn ping() -> Int }\n"
        "component Echo provides ping: Ping {\n"
        "  provide ping { fn ping() = 9 }\n"
        "}\n"
    )


def _instances_source_with_extra() -> str:
    """`_instances_source(1)` plus an unrelated component. `Worker` and
    `Supervisor` are byte for byte the same, so the successor is a real edit
    whose live template's code identity is unchanged."""
    return _instances_source(1) + _echo_source()


@pytest.fixture
def gate_factory():
    """Hand out live `Gate`s and close every one (a leaked gate soft-bricks
    every later test)."""
    from revl.gate import Gate

    gates = []

    def _make(**kwargs):
        gate = Gate(**kwargs)
        gates.append(gate)
        return gate

    try:
        yield _make
    finally:
        for gate in gates:
            gate.close()


@needs_cordis
def test_declared_successor_carries_the_unchanged_live_instance_state(
        gate_factory):
    """The reported bug. Gen N spawns `Worker` and seeds its `Map`; the
    successor re-declares `Worker` and `Supervisor` unchanged and adds an
    unrelated component, so the disclosure gate admits it. `propose` swaps with
    `migrate="declared"`, which used to capture nothing: the successor spawned a
    fresh `Worker` and `read("alice")` returned None. The declared policy must
    carry the state exactly as the generational path does, because the gate's
    carve-out rests on the state being re-seated on the operator's own text."""
    gate = gate_factory(record=True)
    gate.load(_instances_source(1))
    assert gate.call("admin", "ver", [])["result"] == 1
    gate.call("admin", "seed", ["alice", "42"])
    assert gate.call("admin", "read", ["alice"])["result"] == "42"

    result = gate.propose(_instances_source_with_extra(), granted=[])

    assert result.admitted and result.swapped, result.message
    assert "ping" in result.keys
    # the unrelated edit landed, so this is a successor and not a no-op
    assert gate.call("ping", "ping", [])["result"] == 9
    # the reported symptom: the seeded state is read back from the successor's
    # re-declared, unchanged template, not from a fresh cold instance
    assert gate.call("admin", "read", ["alice"])["result"] == "42", (
        "a declared successor that re-declares the live template unchanged "
        "must carry the instance state, not come up cold (issue #2673)")
    # the swap reports what it carried, under the policy that captured it
    assert result.migration is not None
    assert result.state["migration"]["policy"] == "declared"
    assert result.migration["templates"]["Worker"]["migrated"] is True
    assert result.migration["templates"]["Worker"]["instances"] == 1
    # the template is unchanged, so its code is still the operator's version 1
    assert gate.call("admin", "ver", [])["result"] == 1


@needs_cordis
def test_declared_successor_with_no_live_instance_still_lands_cold(
        gate_factory):
    """With the instance quiesced before the swap there is no state to carry:
    the same declared successor still lands, reports no migration, and starts
    its own instance empty. The capture added for #2673 must not invent state
    or turn the no-instance case into a refusal."""
    gate = gate_factory(record=True)
    gate.load(_instances_source(1))
    gate.call("admin", "seed", ["bob", "7"])

    for instance in list(
            gate._session._driver.runtime.live_instances("Worker")):
        pending = instance.dispose()
        if pending is not None:
            gate._session._run(pending)
    assert gate._session._driver.runtime.live_instances("Worker") == []

    result = gate.propose(_instances_source_with_extra(), granted=[])

    assert result.admitted and result.swapped, result.message
    assert result.migration is None
    assert gate.call("ping", "ping", [])["result"] == 9
    assert gate.call("admin", "ver", [])["result"] == 1
    # the seeded key did not ride onto the successor's fresh instance
    assert gate.call("admin", "read", ["bob"])["result"] is None


@needs_cordis
def test_declared_successor_that_drops_a_live_template_is_reverted(
        gate_factory):
    """The capture also covers a live template the successor removes. The
    disclosure gate does not judge it (it is not re-declared), so the
    state-compat check refuses the vanished instance after the load: the swap
    reports SWAP_REVERTED and gen N is back with its code and state. Quiescing
    the instance first is the way to remove a template, not silent data loss."""
    gate = gate_factory(record=True)
    gate.load(_instances_source(1))
    gate.call("admin", "seed", ["dave", "5"])
    assert gate.call("admin", "read", ["dave"])["result"] == "5"

    result = gate.propose(_echo_source(), granted=[])

    assert result.admitted and not result.swapped, result.message
    assert result.reverted is True
    assert result.code == "SWAP_REVERTED", result.message
    # gen N is serving again, its instance state intact
    assert gate.call("admin", "ver", [])["result"] == 1
    assert gate.call("admin", "read", ["dave"])["result"] == "5"


@needs_cordis
def test_generational_swap_still_migrates_the_live_instance_state():
    """The contrast that bounds the fix: an operator (trusted) swap with the
    generational policy still captures and re-seats the state, with a
    `generational` report. The declared capture must not change that path."""
    from revl import compile_source
    from revl.mcp.session import Session

    session = Session()
    try:
        session.load(compile_source(_instances_source(1), "gen1.rvl"),
                     record=True)
        session.call("admin", "seed", ["carol", "9"])
        assert session.call("admin", "read", ["carol"])["result"] == "9"

        state = session.swap(
            compile_source(_instances_source(2), "gen2.rvl"),
            migrate="generational", agent_authored=False)

        assert state["migration"] == {
            "policy": "generational",
            "templates": {"Worker": {"instances": 1, "migrated": True,
                                     "resources": 1}},
        }, state.get("migration")
        assert session.call("admin", "ver", [])["result"] == 2
        assert session.call("admin", "read", ["carol"])["result"] == "9"
    finally:
        session.unload()
