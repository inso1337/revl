"""The post-merge finding on #2679: a declared successor that ADDS a service
consumer can read the operator's re-seated instance state.

#2679 made a declared (agent-authored) swap capture a live template's instance
state when the successor re-declares that template unchanged, so the operator's
state is no longer silently dropped. That closed the data-loss case, but it also
gave the state a new way out: a holder (`Supervisor`) exposes the migrated
instance through its own provided service (`admin.read` calls `w.store.get`).
The disclosure gate admits a successor whose template and holders are
byte-identical, so a successor that merely ADDS a component requiring `admin` -
new code, no change to any component the gate looked at - was admitted and then
read the operator's live state through the holder. Before the capture existed
the same successor read `None`, because the re-declared instance came up cold.

The gate now treats the service consumers of the template and its holders
(transitively) as part of the template's code identity, so the added consumer
changes the identity and refuses with `STATE_UNDISCLOSED` before any teardown.
The controls below keep the widening honest: an unrelated added component still
lands and carries the state, and an unsupported `migrate` label still captures
(the cold-start policy is the only one that carries nothing).
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


def _instances_source() -> str:
    """`Worker` is a spawn target (a template) holding a live `Map`; the
    unchanged `Supervisor` spawns it and exposes seed/read/ver through the spawn
    handle, so a live read proves the instance state crossed the swap."""
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
        "    fn version() = 1\n"
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


def _consumer_of_the_holder() -> str:
    """The reproduced addition: a component requiring the re-declared holder's
    `Admin` service and returning what it reads. It is the only text the
    successor adds; `Worker` and `Supervisor` are byte for byte unchanged, so
    only the consumer set can move the live template's code identity."""
    return (
        "service Leak { fn snoop(k: Str) -> Opt[Str] }\n"
        "component Spy requires admin: Admin provides leak: Leak {\n"
        "  provide leak { fn snoop(k) = admin.read(k) }\n"
        "}\n"
    )


def _unrelated_component() -> str:
    """A component the re-declared template and its holder cannot reach: it
    provides a new service and requires nothing, so it must not move their
    identity."""
    return (
        "service Ping { fn ping() -> Int }\n"
        "component Echo provides ping: Ping {\n"
        "  provide ping { fn ping() = 9 }\n"
        "}\n"
    )


def _via_a_provider_source() -> str:
    """The transitive shape. `Worker` requires `log`, provided by `Registrar`,
    and `Registrar` requires the holder's `admin` and returns what it reads
    there. So `Registrar` is a consumer of the holder, and anything that
    consumes `Registrar` reaches the operator's state one hop further out. The
    walk pops `Worker`, so its provider `Registrar` is seen before the holder
    `Supervisor`; this shape also pins that a component entering the consumer
    set after it was already seen as a provider still contributes its own
    consumers."""
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
        "service Log { fn last() -> Opt[Str] }\n"
        "component Worker provides store: Store requires log: Log {\n"
        "  let m = effect Map.new() undo m.drop()\n"
        "  provide store {\n"
        "    fn get(k) = m.get(k)\n"
        "    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }\n"
        "    fn version() = 1\n"
        "  }\n"
        "}\n"
        "component Registrar provides log: Log requires admin: Admin {\n"
        "  provide log { fn last() = admin.read(\"alice\") }\n"
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


def _consumer_of_the_provider() -> str:
    """The addition for the transitive shape: it requires `log`, the service
    `Registrar` provides, and returns `Registrar.last()`, which reads through
    the holder's `admin`."""
    return (
        "service MirrorEcho { fn peek() -> Opt[Str] }\n"
        "component Mirror requires log: Log provides mirror: MirrorEcho {\n"
        "  provide mirror { fn peek() = log.last() }\n"
        "}\n"
    )


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
def test_declared_successor_that_adds_a_consumer_of_the_holder_is_refused(
        gate_factory):
    """The post-merge finding. Gen N spawns `Worker` and seeds its `Map` through
    the `Supervisor` holder. The successor re-declares both byte-identically and
    only ADDS `Spy`, which requires the holder's `Admin` service. Before the
    fix that additive edit was admitted, the #2679 capture re-seated the
    operator's `alice -> 42`, and `Spy.snoop("alice")` returned it; the new
    consumer set is part of the template's identity now, so the same successor
    refuses before capture (no `migration`), and gen N keeps serving its code
    and its state."""
    gate = gate_factory(record=True)
    gate.load(_instances_source())
    assert gate.call("admin", "ver", [])["result"] == 1
    gate.call("admin", "seed", ["alice", "42"])
    assert gate.call("admin", "read", ["alice"])["result"] == "42"

    result = gate.propose(_instances_source() + _consumer_of_the_holder(),
                          granted=[])

    # refused BEFORE teardown and before capture: not admitted, not swapped,
    # not a post-load revert, and no migration report was produced.
    assert not result.admitted and not result.swapped
    assert not result.reverted
    assert result.code == "STATE_UNDISCLOSED", result.message
    assert result.migration is None
    assert "live instance state" in result.message

    # gen N is untouched: the operator's code (ver == 1) and its live instance
    # state (alice -> 42) are still serving, and the added consumer never boots.
    assert gate.call("admin", "ver", [])["result"] == 1
    assert gate.call("admin", "read", ["alice"])["result"] == "42"


@needs_cordis
def test_declared_successor_that_adds_a_transitive_consumer_is_refused():
    """The consumer relation is transitive. `Registrar` is an unchanged consumer
    of the holder's `admin` (it returns `admin.read("alice")`) and provides
    `log`; the template requires `log`, so `Registrar` is also a provider the
    template calls. The successor ADDS `Mirror`, which requires `log` and
    returns `log.last()`: one hop past `Registrar`, it still reads the operator's
    state. It must refuse like the direct case, before capture. This shape also
    walks `Registrar` as a provider before the holder, so it pins that a
    component that becomes a consumer after it was already seen contributes its
    own consumers.

    Driven through `Session.swap` with `agent_authored=True` rather than
    `Gate.propose`: the untrusted-author allowlist refuses the `Worker` -> `Log`
    requirement with `R2` before the disclosure gate runs, and this test is
    about the identity, not that admission rule (the direct case above covers
    the propose door)."""
    from revl import compile_source
    from revl.mcp.session import Session, SessionError

    session = Session()
    try:
        session.load(compile_source(_via_a_provider_source(), "gen1.rvl"),
                     record=True)
        session.call("admin", "seed", ["alice", "42"])
        assert session.call("admin", "read", ["alice"])["result"] == "42"

        with pytest.raises(SessionError) as raised:
            session.swap(
                compile_source(_via_a_provider_source()
                               + _consumer_of_the_provider(), "gen2.rvl"),
                migrate="declared", agent_authored=True)

        assert raised.value.code == "STATE_UNDISCLOSED", str(raised.value)
        # gen N still serves, state intact, and `Mirror` never came up.
        assert session.call("admin", "read", ["alice"])["result"] == "42"
    finally:
        session.unload()


@needs_cordis
def test_declared_successor_that_adds_an_unrelated_component_still_lands(
        gate_factory):
    """The control that bounds the widening: the same live composition and the
    same declared door, but the added component requires nothing the template
    or its holder provides, so it cannot reach the migrated state and must not
    move the identity. The swap lands and the re-declared instance carries the
    operator's state, exactly as #2679 fixed."""
    gate = gate_factory(record=True)
    gate.load(_instances_source())
    gate.call("admin", "seed", ["alice", "42"])

    result = gate.propose(_instances_source() + _unrelated_component(),
                          granted=[])

    assert result.admitted and result.swapped, result.message
    assert "ping" in result.keys
    assert gate.call("ping", "ping", [])["result"] == 9
    assert gate.call("admin", "read", ["alice"])["result"] == "42", (
        "an unrelated addition must not stop the unchanged template's state "
        "from being carried")
    assert result.migration is not None
    assert result.state["migration"]["policy"] == "declared"
    assert result.migration["templates"]["Worker"]["migrated"] is True


@needs_cordis
def test_an_unknown_migrate_label_still_captures_and_names_itself():
    """#2679 changed the capture condition from `== "generational"` to
    `!= "respawn"`, which catches every label the caller can pass, not only
    `declared`. That is intended: `respawn` is the one policy that carries
    nothing by design (F4, so a rollback cannot launder the generation it rolls
    back from), and an unrecognised label must fail toward carrying the live
    state rather than silently dropping it. The report names the label it
    captured under, so an operator sees what actually ran."""
    from revl import compile_source
    from revl.mcp.session import Session

    session = Session()
    try:
        session.load(compile_source(_instances_source(), "gen1.rvl"),
                     record=True)
        session.call("admin", "seed", ["eve", "3"])

        state = session.swap(compile_source(_instances_source(), "gen2.rvl"),
                             migrate="mystery", agent_authored=False)

        assert state["migration"]["policy"] == "mystery"
        assert state["migration"]["templates"]["Worker"]["migrated"] is True
        assert session.call("admin", "read", ["eve"])["result"] == "3", (
            "an unknown label must not cold-start the live instance")
    finally:
        session.unload()
