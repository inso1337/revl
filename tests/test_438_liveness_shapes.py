"""Roadmap item 438 - the falsification harness for the Petri-net proposal.

Item 438 proposes deriving a Petri net from the composition IR and running a
bounded reachability search over its marking graph to find dead states. This
file is the evidence base for `docs/design/438-petri-reachability.md`, which
recommends building one linear check instead of that engine. Every claim the
design note makes about the net revl actually produces is asserted here
mechanically, so the recommendation can be re-checked by running the tests
rather than by rereading the argument.

Five groups:

1. **The two shapes item 438 names, constructed.** A mutual wait is already a
   G3 refusal naming the cycle, which is the refusal the item asks for. A
   starved `merge` fan-in has no spelling on `main` at all: a stream is an
   activation-local host resource and cannot cross a component boundary, so
   the "sources consumed elsewhere" premise cannot be written.

2. **The derived net's structure.** Read off a real linked manifest: one
   producer per place (G2), read arcs only, so the net is 1-safe, monotone and
   conflict-free.

3. **The blowup that buys nothing.** An exhaustive marking-graph search over a
   four-component diamond enumerates every order ideal of the DAG and finds
   exactly one maximal marking and zero dead states, which the linear-time
   topological sort in `_link` already established.

4. **The one liveness fact the net does surface**, an unprovided place, and the
   two shipped surfaces that already report it.

5. **The finding.** G3 is acyclic over components; placement quotients that
   graph by a process partition, and a quotient of a DAG can have a cycle. Two
   processes that require each other both wire every proxy before either
   serves, so neither comes up. Nothing checks the process graph.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import query  # noqa: E402

SHARED_REALM = ""


# --------------------------------------------------------------------------- #
# The net derivation, exactly as the design note defines it (§2).
# --------------------------------------------------------------------------- #

def _realm_of(entry: dict, key: str) -> str:
    """Mirror `lower._link._realm`: the component's own `isolate` map."""
    return (entry.get("isolate") or {}).get(key, SHARED_REALM)


def _net(ir: dict) -> dict:
    """Derive the item-438 net from a linked manifest.

    place        `(key, realm)`, G2's own unit and `provider_of`'s key
    transition   one component activation
    produce arc  `t -> p` for every key `t` provides
    read arc     `p ~ t` for every key `t` injects, a TEST arc: enablement
                 checks the token and firing leaves it in place, because a
                 provision is resolved once and held, never spent
    """
    entries = ir["manifest"]["components"]
    places: set = set()
    produced_by: dict = {}
    produces: dict = {}
    reads: dict = {}
    for entry in entries:
        name = entry["name"]
        produces[name] = set()
        for key in entry.get("provides") or []:
            place = (key, _realm_of(entry, key))
            places.add(place)
            produced_by.setdefault(place, []).append(name)
            produces[name].add(place)
    for entry in entries:
        name = entry["name"]
        reads[name] = set()
        for key in entry.get("inject") or []:
            place = (key, _realm_of(entry, key))
            places.add(place)
            reads[name].add(place)
    return {
        "places": places,
        "transitions": [e["name"] for e in entries],
        "produced_by": produced_by,
        "produces": produces,
        "reads": reads,
        "load_order": ir["manifest"]["loadOrder"],
    }


def _enabled(net: dict, marking: frozenset, fired: frozenset) -> set:
    return {t for t in net["transitions"]
            if t not in fired and net["reads"][t] <= marking}


def _reachable(net: dict) -> dict:
    """Exhaustive marking-graph search. Returns state -> successor states.

    A state is `(marking, fired)`. This is the bounded BFS item 438 proposes,
    with the bound removed so the enumeration is complete.
    """
    start = (frozenset(), frozenset())
    graph: dict = {}
    frontier = [start]
    while frontier:
        state = frontier.pop()
        if state in graph:
            continue
        marking, fired = state
        succ = set()
        for t in _enabled(net, marking, fired):
            nxt = (marking | net["produces"][t], fired | {t})
            succ.add(nxt)
            frontier.append(nxt)
        graph[state] = succ
    return graph


# --------------------------------------------------------------------------- #
# Sources.
# --------------------------------------------------------------------------- #

MUTUAL_WAIT = """
service Ledger { fn balance() -> Int }
service Audit  { fn record() -> Int }

component LedgerSvc requires audit: Audit provides ledger: Ledger {
  provide ledger { fn balance() = 1 }
}

component AuditSvc requires ledger: Ledger provides audit: Audit {
  provide audit { fn record() = 2 }
}
"""

STARVED_MERGE = """
service Feed { fn tick() -> Int }

component Fanin provides feed: Feed {
  let a = effect Stream.source() undo a.close()
  let b = effect Stream.source() undo b.close()
  let first = subscribe a undo first.close()
  let both  = subscribe merge(a, b) undo both.close()

  provide feed { fn tick() = 1 }
}
"""

# The same fan-in with the sources honestly its own: this one admits, which is
# what makes the refusal above a real check and not a parse accident.
HONEST_MERGE = """
service Feed { fn tick() -> Int }

component Fanin provides feed: Feed {
  let a = effect Stream.source() undo a.close()
  let b = effect Stream.source() undo b.close()
  let both = subscribe merge(a, b) undo both.close()

  provide feed { fn tick() = 1 }
}
"""

# A stream handed across a component boundary, in every spelling the surface
# offers. None of them reaches the subscriber.
CROSS_COMPONENT_STREAM = """
service Src {{ fn open() -> Stream[Int] }}

component Producer provides src: Src {{
  let a = effect Stream.source() undo a.close()
  provide src {{ fn open() = a }}
}}

component Consumer requires src: Src {{
  {binding}
  let sub = subscribe s undo sub.close()
}}
"""

CROSS_COMPONENT_BINDINGS = [
    "let s = emit src.open()",
    "let s = emit src.open() compensate s.close()",
    "let s = effect src.open() undo s.close()",
]

# A diamond: two independent providers, a consumer of both, and a fourth
# component behind the consumer. Width 2, depth 3.
DIAMOND = """
service Db    { fn read() -> Int }
service Cache { fn get() -> Int }
service Api   { fn serve() -> Int }
service Log   { fn note() -> Int }

component DbSvc provides db: Db {
  provide db { fn read() = 1 }
}

component CacheSvc provides cache: Cache {
  provide cache { fn get() = 2 }
}

component ApiSvc requires db: Db, cache: Cache provides api: Api {
  provide api { fn serve() = 3 }
}

component LogSvc requires api: Api provides log: Log {
  provide log { fn note() = 4 }
}
"""

UNPROVIDED = """
service Ledger  { fn balance() -> Int }
service Missing { fn thing() -> Int }

component LedgerSvc requires m: Missing provides ledger: Ledger {
  provide ledger { fn balance() = 1 }
}
"""

# Four components in two independent chains. The component graph is a pair of
# disjoint edges, so G3 is satisfied by any placement. Splitting it across two
# processes CROSSWISE closes a cycle in the quotient.
CROSSWISE = """
service K1 { fn one() -> Int }
service K2 { fn two() -> Int }
service Out1 { fn a() -> Int }
service Out2 { fn b() -> Int }

component P1 provides k1: K1 {
  provide k1 { fn one() = 1 }
}

component P2 provides k2: K2 {
  provide k2 { fn two() = 2 }
}

component C1 requires k2: K2 provides out1: Out1 {
  provide out1 { fn a() = 3 }
}

component C2 requires k1: K1 provides out2: Out2 {
  provide out2 { fn b() = 4 }
}
"""

# The placement a conductor would be handed: process A holds P1 and C1,
# process B holds P2 and C2. Nothing in `placement.py` checks this partition.
CROSSWISE_PARTITION = {"A": ["P1", "C1"], "B": ["P2", "C2"]}


def _process_graph(ir: dict, partition: dict) -> dict:
    """The quotient of the component DAG by a placement partition.

    Mirrors how `placement.py` derives a process spec: a process proxies every
    key it requires and does not itself provide (`placement.py:2216-2221`), and
    the proxy's target is the process that owns the key. The runner connects
    every proxy BEFORE it serves anything (`_process_runner.py` steps 1 and 3),
    so a process edge is a boot-time wait edge.
    """
    home = {c: p for p, members in partition.items() for c in members}
    entries = {e["name"]: e for e in ir["manifest"]["components"]}
    owner = {}
    for name, entry in entries.items():
        for key in entry.get("provides") or []:
            owner[key] = home[name]
    edges: dict = {p: set() for p in partition}
    for name, entry in entries.items():
        for key in entry.get("inject") or []:
            target = owner.get(key)
            if target is not None and target != home[name]:
                edges[home[name]].add(target)
    return edges


def _has_cycle(edges: dict) -> bool:
    state: dict = {}

    def visit(node) -> bool:
        state[node] = 1
        for succ in edges.get(node, ()):
            if state.get(succ) == 1:
                return True
            if state.get(succ, 0) == 0 and visit(succ):
                return True
        state[node] = 2
        return False

    return any(state.get(n, 0) == 0 and visit(n) for n in edges)


# --------------------------------------------------------------------------- #
# 1. The two shapes item 438 names.
# --------------------------------------------------------------------------- #

def test_mutual_wait_is_already_a_g3_refusal_naming_the_cycle():
    """Shape one. Two consumers each holding a provision the other waits on IS
    a `requires` cycle, and `_link`'s DFS already refuses it by name.

    This is exactly the deliverable item 438 asks a reachability search to
    produce: "a REFUSAL naming the deadlocked cycle rather than a crash at
    3am". It ships, it is linear time, and it carries a why-trace.
    """
    with pytest.raises(RevlError) as exc:
        compile_source(MUTUAL_WAIT, "mutual.rvl")
    message = str(exc.value)
    assert "dependency cycle" in message
    assert "(G3)" in message
    assert "LedgerSvc" in message and "AuditSvc" in message


def test_a_starved_merge_fan_in_is_already_a_rule_31_refusal():
    """Shape two, in the only scope where it can be written: one activation.

    `_admit_stream_operand` sees every operand of the fan-in and every earlier
    subscription in the same `Env`, so a source consumed twice is caught
    pointwise. The interleaving item 438 worries about needs the second
    consumer to be somewhere the checker cannot see, which is the next test.
    """
    with pytest.raises(RevlError) as exc:
        compile_source(STARVED_MERGE, "starve.rvl")
    message = str(exc.value)
    assert "already subscribed" in message
    assert "rule 3.1" in message


def test_the_same_fan_in_with_its_own_sources_admits():
    """The refusal above bites on the starvation, not on `merge` itself."""
    ir = compile_source(HONEST_MERGE, "honest.rvl")
    assert [c["name"] for c in ir["manifest"]["components"]] == ["Fanin"]


@pytest.mark.parametrize("binding", CROSS_COMPONENT_BINDINGS)
def test_a_stream_cannot_cross_a_component_boundary(binding):
    """The premise of shape two has no spelling on `main`.

    "sources consumed elsewhere" requires a stream to be reachable from two
    components. A stream is only ever an `env.host_locals` entry created by
    `effect Stream.source()`, so every operand of every `merge` in a program is
    visible to the one `Env` that admits it. `_admit_stream_operand`'s own hint
    says so: "A required `Stream[T]` capability is a later slice."

    When that slice lands, rule 3.1 becomes a claim about a composition rather
    than about a scope, and it can no longer be checked pointwise. That is
    trigger T1 in the design note.
    """
    with pytest.raises(RevlError) as exc:
        compile_source(CROSS_COMPONENT_STREAM.format(binding=binding),
                       "cross.rvl")
    message = str(exc.value)
    assert ("has no use for" in message           # G4 refuses the plain bind
            or "does not name one" in message)    # the operand is not a stream


# --------------------------------------------------------------------------- #
# 2. The derived net's structure.
# --------------------------------------------------------------------------- #

def test_g2_gives_every_place_exactly_one_producing_transition():
    """One producer per `(key, realm)` is G2 restated in net terms. It is what
    makes the net 1-safe without an S-invariant computation: a place's token
    can be produced by one transition, and that transition fires once.
    """
    net = _net(compile_source(DIAMOND, "diamond.rvl"))
    assert net["produced_by"]
    for place, producers in net["produced_by"].items():
        assert len(producers) == 1, (place, producers)


def test_the_incidence_matrix_makes_the_s_invariant_space_trivial():
    """`C = Post - Pre`. A read arc is a self-loop, so it cancels and every
    remaining entry is a `+1` at a place and its single producer.

    A non-negative S-invariant `y` therefore satisfies, for each transition
    `t`, `sum(y_p for p in post(t)) == 0`, which forces `y_p == 0` at every
    PRODUCED place. The invariant space is spanned by the unproduced places
    alone, and an unproduced place is one nothing in the composition provides.
    So Martinez-Silva signed elimination over a revl composition returns the
    set of unresolved injections, which `query.Composition.unresolved_
    injections` already answers with a dict lookup.
    """
    net = _net(compile_source(DIAMOND, "diamond.rvl"))
    incidence = {}
    for t in net["transitions"]:
        for p in net["produces"][t]:
            incidence[(p, t)] = incidence.get((p, t), 0) + 1
        for p in net["reads"][t]:          # +1 post, -1 pre: cancels
            incidence[(p, t)] = incidence.get((p, t), 0)
    assert all(v in (0, 1) for v in incidence.values())
    produced = set(net["produced_by"])
    assert {p for (p, _), v in incidence.items() if v == 1} == produced
    # nothing is unproduced in the diamond, so the invariant space is {0}
    assert net["places"] - produced == set()


def test_the_net_is_monotone_and_conflict_free():
    """No transition ever removes a token, so an enabled transition stays
    enabled until it fires. That is persistence, and it is why there is no
    interleaving to search: every maximal firing sequence ends in the same
    marking.
    """
    net = _net(compile_source(DIAMOND, "diamond.rvl"))
    for state, succs in _reachable(net).items():
        marking, fired = state
        here = _enabled(net, marking, fired)
        for nxt in succs:
            nxt_marking, nxt_fired = nxt
            assert marking <= nxt_marking                 # monotone
            assert here - nxt_fired <= _enabled(net, nxt_marking, nxt_fired)


def test_load_order_is_a_witness_firing_sequence():
    """`_link`'s Kahn sort already hands back a sequence that fires every
    transition. Replaying it needs no search: each transition is enabled when
    it is reached.
    """
    net = _net(compile_source(DIAMOND, "diamond.rvl"))
    marking, fired = frozenset(), frozenset()
    for name in net["load_order"]:
        assert net["reads"][name] <= marking, name
        marking |= net["produces"][name]
        fired |= {name}
    assert set(fired) == set(net["transitions"])
    assert marking == set(net["produced_by"])


# --------------------------------------------------------------------------- #
# 3. The blowup that buys nothing.
# --------------------------------------------------------------------------- #

def test_the_marking_graph_search_finds_nothing_the_sort_did_not():
    """The exhaustive search over the diamond's marking graph.

    It visits one state per order ideal of the dependency DAG, finds ZERO dead
    states, and finds exactly ONE terminal marking, which is the one
    `loadOrder` reaches in a single linear pass. The search is strictly more
    expensive and strictly less informative: it has no cycle to name, because
    a net with a cycle never gets built - G3 refused it first.
    """
    net = _net(compile_source(DIAMOND, "diamond.rvl"))
    graph = _reachable(net)
    terminals = [s for s, succ in graph.items() if not succ]
    assert len(terminals) == 1
    marking, fired = terminals[0]
    assert set(fired) == set(net["transitions"])          # no dead state
    assert marking == set(net["produced_by"])             # the unique maximum
    # more states than components, for a composition of four
    assert len(graph) > len(net["transitions"])
    # and every maximal sequence agrees, which is what persistence buys
    for order in itertools.permutations(net["transitions"]):
        m, f = frozenset(), frozenset()
        for t in order:
            if net["reads"][t] <= m:
                m |= net["produces"][t]
                f |= {t}
        if set(f) == set(net["transitions"]):
            assert m == marking


# --------------------------------------------------------------------------- #
# 4. The one liveness fact the net surfaces, and who already reports it.
# --------------------------------------------------------------------------- #

def test_an_unprovided_place_admits_and_leaves_a_permanently_dead_transition():
    """A required key nothing provides is NOT a refusal: an open composition
    completed by ambient components at admission time is legitimate, which is
    why this cannot fail closed. In the net it is a place with no producing
    transition, so its consumer is dead in every reachable marking.

    This is the whole of what the net formulation adds, and it is a dict
    lookup, not a search.
    """
    ir = compile_source(UNPROVIDED, "unprovided.rvl")
    assert ir["manifest"]["loadOrder"] == ["LedgerSvc"]
    net = _net(ir)
    unproduced = net["places"] - set(net["produced_by"])
    assert unproduced == {("m", SHARED_REALM)}
    graph = _reachable(net)
    terminals = [s for s, succ in graph.items() if not succ]
    assert len(terminals) == 1
    assert set(terminals[0][1]) == set()          # nothing ever fires


def test_the_dead_transition_is_already_reported_by_two_shipped_surfaces():
    """`revl query reaches` names the unresolved key and downgrades its own
    claim because of it, which is the item-418 posture item 438's open question
    2 asks for. `revl query withdraw` is the reachability query that does
    matter, and it is a linear walk of the same DAG.
    """
    ir = compile_source(UNPROVIDED, "unprovided.rvl")
    reach = query.reach(ir, "LedgerSvc")
    assert any("`m`" in a for a in reach["assumptions"])
    assert any("INCOMPLETE" in a for a in reach["assumptions"])

    diamond = compile_source(DIAMOND, "diamond.rvl")
    cascade = query.withdrawal(diamond, "DbSvc")
    rendered = str(cascade)
    assert "ApiSvc" in rendered and "LogSvc" in rendered


# --------------------------------------------------------------------------- #
# 5. The one wait cycle that IS reachable, and is not covered.
# --------------------------------------------------------------------------- #

def test_a_process_partition_can_close_a_cycle_the_component_dag_does_not():
    """G3 is acyclic over COMPONENTS. Placement quotients that graph by a
    partition, and a quotient of a DAG can have a cycle.

    Four components in two disjoint chains, split crosswise across two
    processes: A must connect to B for `k2` and B must connect to A for `k1`.
    Each runner wires every proxy (step 1) before it serves (step 3), so
    neither ever listens. `bridge._connect`'s docstring says the retry loop
    "makes start order irrelevant", which is true of a DAG of processes and
    false of this one.

    This is the design note's §5.2 finding and the one thing worth building.
    The check is a cycle detection on a graph with one node per process, which
    is the same linear DFS `_link` already runs one level down - not a
    marking-graph search.
    """
    ir = compile_source(CROSSWISE, "crosswise.rvl")          # G3 is satisfied
    assert set(ir["manifest"]["loadOrder"]) == {"P1", "P2", "C1", "C2"}
    assert not _has_cycle({e["name"]: set() for e in ir["manifest"]["components"]})

    edges = _process_graph(ir, CROSSWISE_PARTITION)
    assert edges == {"A": {"B"}, "B": {"A"}}
    assert _has_cycle(edges)


def test_the_same_components_placed_together_are_acyclic():
    """The partition is the whole cause: the components did not change."""
    ir = compile_source(CROSSWISE, "crosswise.rvl")
    edges = _process_graph(ir, {"A": ["P1", "P2"], "B": ["C1", "C2"]})
    assert edges == {"A": set(), "B": {"A"}}
    assert not _has_cycle(edges)
