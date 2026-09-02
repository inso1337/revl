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

5. **The finding, and the check that closes it.** G3 is acyclic over
   components; placement quotients that graph by a process partition, and a
   quotient of a DAG can have a cycle. Two processes that require each other
   both wire every proxy before either serves, so neither comes up. Roadmap
   item 171 landed `placement.process_cycle_refusal` for exactly this shape:
   the crosswise placement is now a plan-time refusal naming the cycle, and
   the control - the same components placed TOGETHER - still admits, which is
   what proves the check is about the partition rather than the components.

6. **The wait-edge inventory** (design note §8.2, in the shape item 414
   established). Not a proof: a completeness checklist the type system cannot
   forget. Every wait primitive in the language carries a verdict saying what
   already covers it, every keyword and every host verb is classified against
   that table, and a new one that suspends cannot be added without a verdict.
   §5.2's finding was found by walking this table by hand; this is the
   mechanised version, and it is what will find the next one.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl import query  # noqa: E402
from revl.lexer import KEYWORDS  # noqa: E402
from revl.typecheck import _HOST_ARG_SIG  # noqa: E402

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
# process B holds P2 and C2. This is the partition item 171's gate refuses.
CROSSWISE_PARTITION = {"A": ["P1", "C1"], "B": ["P2", "C2"]}

# Six components in three independent chains, split three ways so the quotient
# is a 3-cycle: the refusal must walk the whole loop, not just name one edge.
CROSSWISE_THREE = """
service Ka { fn a() -> Int }
service Kb { fn b() -> Int }
service Kc { fn c() -> Int }
service Oa { fn x() -> Int }
service Ob { fn y() -> Int }
service Oc { fn z() -> Int }

component A1 provides ka: Ka { provide ka { fn a() = 1 } }
component B1 provides kb: Kb { provide kb { fn b() = 2 } }
component C1 provides kc: Kc { provide kc { fn c() = 3 } }

component A2 requires kb: Kb provides oa: Oa { provide oa { fn x() = 4 } }
component B2 requires kc: Kc provides ob: Ob { provide ob { fn y() = 5 } }
component C2 requires ka: Ka provides oc: Oc { provide oc { fn z() = 6 } }
"""


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
# 5. The one wait cycle that IS reachable, and the check that refuses it.
# --------------------------------------------------------------------------- #

def _placement_inputs(ir: dict, partition: dict) -> dict:
    """The four tables `run_placement` derives from an IR plus a partition.

    Mirrors `placement.run_placement` exactly (`placed`, then `merged(...)` for
    `provides`/`requires`, then `owner`), so the gate under test is handed the
    same shapes the conductor hands it.
    """
    components = {c["name"]: c for c in ir.get("components") or []}
    placed = {c: p for p, members in partition.items() for c in members}
    assert set(placed) == set(components), "every component must be placed"

    def merged(cnames, which):
        out: dict = {}
        for cname in cnames:
            out.update(components[cname].get(which) or {})
        return out

    provides = {p: merged(m, "provides") for p, m in partition.items()}
    requires = {p: merged(m, "requires") for p, m in partition.items()}
    owner = {key: p for p, keys in provides.items() for key in keys}
    return {"components": components, "placed": placed, "provides": provides,
            "requires": requires, "owner": owner}


def _refusal(ir: dict, partition: dict, placement_path: str = "p.toml") -> str | None:
    args = _placement_inputs(ir, partition)
    return _placement.process_cycle_refusal(
        args["requires"], args["provides"], args["owner"], args["placed"],
        args["components"], placement_path=placement_path)


def test_a_process_partition_can_close_a_cycle_the_component_dag_does_not():
    """G3 is acyclic over COMPONENTS. Placement quotients that graph by a
    partition, and a quotient of a DAG can have a cycle.

    Four components in two disjoint chains, split crosswise across two
    processes: A must connect to B for `k2` and B must connect to A for `k1`.
    Each runner wires every proxy (step 1) before it serves (step 3), so
    neither ever listens. `bridge._connect`'s docstring says the retry loop
    "makes start order irrelevant", which is true of a DAG of processes and
    false of this one.

    This is the design note's §5.2 finding. The check is a cycle detection on
    a graph with one node per process, which is the same linear DFS `_link`
    already runs one level down - not a marking-graph search.
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


def test_the_shipped_gate_derives_the_same_process_graph_as_the_runner():
    """`placement.process_graph` and the mirror above agree.

    The mirror is written from `_process_runner`'s own boot steps; the shipped
    function is written from the proxy-construction loop. They are two readings
    of the same relation, and the check is only worth anything if they match.
    """
    ir = compile_source(CROSSWISE, "crosswise.rvl")
    for partition in (CROSSWISE_PARTITION,
                      {"A": ["P1", "P2"], "B": ["C1", "C2"]},
                      {"A": ["P1", "P2", "C1", "C2"]},
                      {"A": ["P1"], "B": ["P2"], "C": ["C1"], "D": ["C2"]}):
        args = _placement_inputs(ir, partition)
        shipped = _placement.process_graph(args["requires"], args["provides"],
                                           args["owner"])
        assert shipped == _process_graph(ir, partition), partition


def test_the_crosswise_placement_is_refused_and_the_refusal_names_the_cycle():
    """Item 171. A refusal that says "there is a cycle" without saying WHICH
    processes is not actionable, so this pins the whole message shape: the
    cycle, one line per proxied key that closes a hop naming the requiring and
    providing components, the boot-order reason, and the fix.
    """
    ir = compile_source(CROSSWISE, "crosswise.rvl")
    message = _refusal(ir, CROSSWISE_PARTITION, "crosswise.toml")
    assert message is not None
    assert message.splitlines()[0] == "process cycle: A -> B -> A"
    assert "  A proxies `k2` from B  (required by C1, provided by P2)" in message
    assert "  B proxies `k1` from A  (required by C2, provided by P1)" in message
    assert "wires every proxy before it serves" in message
    assert "crosswise.toml" in message          # the PARTITION is named, not an author
    assert "co-locate" in message
    # and it names no author-facing blame: the components are cited as evidence
    # for the hops, never as the thing at fault.
    assert "G3 passed" in message


def test_the_control_the_same_components_placed_together_still_admits():
    """The control is the load-bearing half: it proves the refusal is about the
    partition and not about the components. Same source, same G3, same four
    components - placed so the quotient is a DAG, and the gate is silent.
    """
    ir = compile_source(CROSSWISE, "crosswise.rvl")
    assert _refusal(ir, {"A": ["P1", "P2"], "B": ["C1", "C2"]}) is None
    assert _refusal(ir, {"A": ["P1", "P2", "C1", "C2"]}) is None
    # even one process per component is fine: the component graph IS the
    # quotient there, and G3 already proved it acyclic.
    assert _refusal(ir, {"A": ["P1"], "B": ["P2"], "C": ["C1"], "D": ["C2"]}) is None


def test_a_longer_process_cycle_is_named_in_full():
    """Three processes, three hops. The refusal walks the whole cycle rather
    than naming the two ends of one edge."""
    ir = compile_source(CROSSWISE_THREE, "three.rvl")
    message = _refusal(ir, {"A": ["A1", "A2"], "B": ["B1", "B2"], "C": ["C1", "C2"]})
    assert message is not None
    head = message.splitlines()[0]
    assert head.startswith("process cycle: ")
    hops = head[len("process cycle: "):].split(" -> ")
    assert len(hops) == 4 and hops[0] == hops[-1]
    assert set(hops) == {"A", "B", "C"}
    for proc in ("A", "B", "C"):
        assert f"  {proc} proxies " in message


def test_a_remote_key_is_not_a_process_edge():
    """Scoping decision one: a `[remotes.<key>]` provider is a SEPARATE
    composition on its own placement (item 151), reached by address. This
    process graph cannot see its boot order and must not pretend to, so a
    remote key contributes no edge even when a local process happens to be
    named the same way."""
    ir = compile_source(CROSSWISE, "crosswise.rvl")
    args = _placement_inputs(ir, CROSSWISE_PARTITION)
    # pretend `k1` is served by a remote composition rather than process A
    assert _placement.process_graph(args["requires"], args["provides"],
                                    args["owner"], remote_keys={"k1"}) == {
        "A": {"B"}, "B": set()}
    assert _placement.process_cycle_refusal(
        args["requires"], args["provides"], args["owner"], args["placed"],
        args["components"], remote_keys={"k1"}) is None


def test_an_unprovided_key_is_not_a_process_edge():
    """A key no process provides has its own refusal ("provided by no
    process"); it must not be mistaken for an edge to nowhere here."""
    ir = compile_source(UNPROVIDED, "unprovided.rvl")
    args = _placement_inputs(ir, {"A": ["LedgerSvc"]})
    assert _placement.process_graph(args["requires"], args["provides"],
                                    args["owner"]) == {"A": set()}


def test_a_key_a_process_both_requires_and_provides_is_not_an_edge():
    """A locally-served key is not proxied at all, so it is not a wait: this
    is the same in-process short-circuit the proxy loop takes."""
    ir = compile_source(CROSSWISE, "crosswise.rvl")
    args = _placement_inputs(ir, {"A": ["P1", "C2"], "B": ["P2", "C1"]})
    # A provides k1 and requires k1 locally; B provides k2 and requires k2.
    assert _placement.process_graph(args["requires"], args["provides"],
                                    args["owner"]) == {"A": set(), "B": set()}


def test_the_refusal_fires_from_run_placement_before_anything_is_spawned(
        tmp_path, monkeypatch, capsys):
    """End to end, through the conductor the CLI calls. The point of putting
    this at plan time is that it lands as a diagnostic on `revl run
    --placement` rather than as two `ConnectionError`s five seconds into a
    boot, so it must fire before any TLS material is minted or any child
    process is started."""
    app = tmp_path / "crosswise.rvl"
    app.write_text(CROSSWISE, encoding="utf-8")
    plc = tmp_path / "crosswise.toml"
    plc.write_text('[processes.A]\ncomponents = ["P1", "C1"]\n\n'
                   '[processes.B]\ncomponents = ["P2", "C2"]\n', encoding="utf-8")

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement.subprocess, "Popen", _never_spawned)

    rc = _placement.run_placement([str(app)], str(plc), once=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: process cycle: A -> B -> A\n")
    assert "A proxies `k2` from B" in err
    assert str(plc) in err


def _never_spawned(*args, **kwargs):
    raise AssertionError("run_placement spawned a child past the cycle refusal")


def test_the_tiers_sugar_form_is_gated_too(tmp_path, monkeypatch, capsys):
    """A `[tiers]` manifest (item 363) partitions by BACKEND, which is the most
    likely way to draw the partition across the component DAG rather than along
    it - an author picks a tier per component and never sees the quotient.
    `expand_tiers` runs before this gate, so the synthesized `tier_<backend>`
    processes are checked exactly like a hand-written `[processes]` topology.
    """
    app = tmp_path / "crosswise.rvl"
    app.write_text(CROSSWISE, encoding="utf-8")
    plc = tmp_path / "crosswise.toml"
    plc.write_text('default_tier = "py"\n\n[tiers]\n'
                   'P1 = "py"\nC1 = "py"\nP2 = "node"\nC2 = "node"\n',
                   encoding="utf-8")

    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(_placement.subprocess, "Popen", _never_spawned)

    rc = _placement.run_placement([str(app)], str(plc), once=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: process cycle: ")
    assert "tier_py" in err and "tier_node" in err
    assert "proxies `k1`" in err and "proxies `k2`" in err


# --------------------------------------------------------------------------- #
# 6. The wait-edge inventory (design note §8.2), in the item-414 shape.
# --------------------------------------------------------------------------- #
#
# §5.2's finding was found by walking §5's table by hand, not by any analysis.
# This is that table, mechanised: an enumeration of every way a fiber can wait
# on something another component owns, each with a verdict naming what covers
# it, and two totality guards that fail when the language grows a wait
# primitive nobody has classified.

# The verdict vocabulary, verbatim from the design note. "OPEN" is the one that
# must never appear: a row with no verdict is a real hole, and the guard below
# refuses it.
VERDICTS = {
    "g3":       "the edge is a `requires` edge; a cycle is refused by `_link`",
    "scoped":   "the waited-for thing cannot leave one activation",
    "owned":    "the wait relation is a tree rooted at an owner (spawn)",
    "deadline": "bounded at runtime by a seam deadline (item 54)",
    "diverges": "a G3-invisible call edge that provably cannot suspend",
    "process":  "a process-graph edge; a cycle is refused by item 171's gate",
    "host":     "inside an opaque host body: residual R1, out of reach in principle",
    "OPEN":     "none of the above - a real hole",
}

# id -> (verdict, what waits on what, the evidence).
# `evidence` names a test in THIS file, or a `docs/`/`src/` location the design
# note argues from. The guard below checks that a named test actually exists,
# so a row cannot cite a test that was renamed away.
WAIT_EDGES: dict[str, tuple[str, str, str]] = {
    "activation_prereq": (
        "g3",
        "an activation waits for the component that provides a key it injects",
        "test_mutual_wait_is_already_a_g3_refusal_naming_the_cycle"),
    "emit_injected_key": (
        "g3",
        "`emit svc.method()` on an injected key blocks on that provider",
        "test_an_emit_call_cycle_is_a_requires_cycle"),
    "emit_routed_key": (
        "g3",
        "`emit` across a routed multi-realm key waits on each routed leg",
        "lower.py `_link` adds one edge per routed realm (design note §5)"),
    "emit_spawn_handle": (
        "owned",
        "`emit` through a spawn handle waits on the spawned instance",
        "test_a_spawned_instances_provisions_do_not_enter_the_link_table"),
    "cross_process_seam": (
        "process",
        "a seam call waits on the providing PROCESS, not just the component",
        "test_the_crosswise_placement_is_refused_and_the_refusal_names_the_cycle"),
    "seam_call_runtime": (
        "deadline",
        "a live-but-wedged provider holds its caller inside a seam call",
        "test_every_seam_carries_a_finite_deadline_by_default"),
    "arrow_across_service": (
        "diverges",
        "an arrow crossing a service boundary re-enters its owner (`C -> P -> "
        "C's closure -> P`), which G3 cannot see",
        "test_an_arrow_cannot_reach_an_async_operation"),
    "subscription_next": (
        "scoped",
        "`<sub>.next()` waits for the stream's producer",
        "test_a_stream_cannot_cross_a_component_boundary"),
    "stream_backpressure": (
        "scoped",
        "a `block`-policy provider suspends until its consumer drains",
        "test_a_stream_cannot_cross_a_component_boundary"),
    "divert_await": (
        "scoped",
        "`await <expr>` / `await Job.run(..)` suspends at a divert boundary",
        "test_there_is_no_future_or_task_value_to_await_across_a_boundary"),
    "approval_await": (
        "scoped",
        "`await approval[C] { .. }` reads as a wait but is a ledger lookup "
        "that fails closed - it never suspends",
        "lower.py `_lower_approval` (design note §5)"),
    "parallel_rejoin": (
        "scoped",
        "a parallel emission fan-out rejoins its branches",
        "parallel.py refuses a partition whose branches are not independent "
        "(item 259 §2.2)"),
    "pool_job": (
        "scoped",
        "a pooled connection or a `Job` could block on capacity",
        "test_the_host_frontier_has_no_blocking_primitive"),
    "teardown_lifo": (
        "g3",
        "teardown runs the reverse of a valid topological order, so one "
        "component's `undo` holds the next one's",
        "test_a_teardown_inverse_cannot_suspend"),
    "management_lease": (
        "deadline",
        "a management lease (item 61) holds the management plane",
        "mcp/leases.py: a lease is TTL-bound and the component keeps serving "
        "every call throughout (design note §5)"),
    "host_body": (
        "host",
        "a `@py { .. }` body may hold a lock, an unbounded read or a "
        "subprocess wait - one verbatim token to the compiler",
        "residual R1: the trust boundary G4, G8, item 414 and item 256 "
        "already rest on (design note §5.3)"),
}

# Totality guard one: every KEYWORD in the surface grammar is classified,
# either as introducing one of the wait edges above or as introducing none.
# Adding a keyword reds this until somebody decides which, which is the whole
# mechanism: a `lock`/`channel`/`join` keyword cannot arrive unnoticed.
KEYWORD_WAIT_EDGE: dict[str, str | None] = {
    # the wait-introducing surface
    "requires": "activation_prereq",
    "provides": "activation_prereq",
    "provide": "cross_process_seam",
    "emit": "emit_injected_key",
    "emission": "emit_injected_key",
    "isolate": "emit_routed_key",
    "realm": "emit_routed_key",
    "spawn": "emit_spawn_handle",
    "subscribe": "subscription_next",
    "await": "divert_await",
    "async": "divert_await",
    "effect": "divert_await",
    "undo": "teardown_lifo",
    "compensate": "teardown_lifo",
    "extern": "host_body",
    "acquire": "host_body",
    # everything else introduces no suspension. Grouped by why:
    #   declaration/structure  service component config type use pub test
    #   pure computation       fn let var return if else while for of match
    #                          assert true false null as in hole
    #   loop control           break continue
    #   effect classification  pure verified commutative idempotent
    #   non-suspending effects intercept handoff with every after fail
    "service": None, "component": None, "config": None, "type": None,
    "use": None, "pub": None, "test": None,
    "fn": None, "let": None, "var": None, "return": None, "if": None,
    "else": None, "while": None, "for": None, "of": None, "match": None,
    "assert": None, "true": None, "false": None, "null": None, "as": None,
    "in": None, "hole": None,
    "break": None, "continue": None,
    "pure": None, "verified": None, "commutative": None, "idempotent": None,
    # `intercept`/`handoff`/`with` rewrite or hand over a provision without
    # suspending; `every`/`after` acquire a revertible schedule (the timer
    # fires later, it does not hold the activation); `fail` is an L-Raise.
    "intercept": None, "handoff": None, "with": None,
    "every": None, "after": None, "fail": None,
}

# Totality guard two: every HOST VERB on the frontier is classified the same
# way. `_HOST_ARG_SIG` is the complete host surface an admitted program can
# name, so a blocking verb (`Pool.acquire`, `Chan.recv`) cannot be added
# without a verdict either.
HOST_VERB_WAIT_EDGE: dict[str, str | None] = {
    "Map.new": None, "Map.drop": None, "Map.insert": None,
    "Map.insert_if_absent": None, "Map.remove": None, "Map.get": None,
    "Pool.open": "pool_job", "Pool.close": "pool_job",
    "Pool.query": "pool_job", "Pool.execute": "pool_job",
    "Job.run": "divert_await",
    "Stream.source": "stream_backpressure", "Stream.close": None,
    "Subscription.next": "subscription_next", "Subscription.close": None,
}

# Names a synchronisation primitive would have to wear. The design note's row
# "any mutex, semaphore, channel or queue" says no such surface exists; these
# two guards are what keep that true rather than remembered.
SYNC_PRIMITIVE_NAMES = frozenset({
    "lock", "unlock", "mutex", "semaphore", "channel", "chan", "queue",
    "join", "yield", "sleep", "wait", "notify", "signal", "barrier",
    "recv", "acquire_lock", "release",
})

# The host frontier bans one more name than the grammar does: `acquire`. As a
# revl KEYWORD it classifies an extern (`extern acquire fn open() -> H`), which
# is a bracket, not a wait. As a HOST VERB (`Pool.acquire`) it would be a
# borrow-until-available - the blocking checkout the design note's `pool_job`
# row rests on NOT existing ("a pool connection is borrowed for one call, and
# exhaustion RAISES rather than blocking").
BLOCKING_HOST_VERB_NAMES = SYNC_PRIMITIVE_NAMES | {"acquire"}


def test_every_wait_edge_carries_a_legal_verdict_and_none_is_open():
    """The table's own shape. An "OPEN" row is a real hole, so it fails here
    rather than sitting in a doc nobody rereads."""
    assert WAIT_EDGES, "the inventory must not be empty"
    for wid, (verdict, waits_on, evidence) in WAIT_EDGES.items():
        assert verdict in VERDICTS, (wid, verdict)
        assert verdict != "OPEN", (
            f"wait edge {wid!r} has no verdict: {waits_on}. An OPEN row is a "
            "liveness hole, not a TODO - close it or refuse the shape.")
        assert waits_on and evidence, wid


def test_every_verdict_in_the_vocabulary_is_used_by_some_row():
    """Except "OPEN", which is used by none - that is the invariant above."""
    used = {verdict for verdict, _, _ in WAIT_EDGES.values()}
    assert used == set(VERDICTS) - {"OPEN"}


def test_every_cited_test_exists():
    """A row may cite a test in this file instead of restating its evidence.
    That citation is only worth anything if the test is still here."""
    here = set(globals())
    for wid, (_, _, evidence) in WAIT_EDGES.items():
        if evidence.startswith("test_"):
            assert evidence in here, (wid, evidence)


def test_every_keyword_is_classified_against_the_inventory():
    """The guard the design note asks for: a new wait primitive cannot be
    added without a verdict.

    `KEYWORDS` is the lexer's own enumeration of the surface, so this cannot
    drift from the language. A keyword added to `revl.lexer` reds this test
    until somebody says whether it suspends, and if it does, which row covers
    it.
    """
    unclassified = KEYWORDS - set(KEYWORD_WAIT_EDGE)
    assert not unclassified, (
        f"new surface keyword(s) {sorted(unclassified)} with no wait verdict. "
        "Decide: does this construct let one component wait on something "
        "another owns? If so add a WAIT_EDGES row; if not, map it to None.")
    stale = set(KEYWORD_WAIT_EDGE) - KEYWORDS
    assert not stale, f"classified keyword(s) no longer in the lexer: {sorted(stale)}"
    for kw, wid in KEYWORD_WAIT_EDGE.items():
        assert wid is None or wid in WAIT_EDGES, (kw, wid)


def test_every_host_verb_is_classified_against_the_inventory():
    """The same guard over the host frontier. `_HOST_ARG_SIG` is the complete
    set of host verbs an admitted program can name (design note §5), so a
    blocking one cannot arrive unclassified."""
    unclassified = set(_HOST_ARG_SIG) - set(HOST_VERB_WAIT_EDGE)
    assert not unclassified, (
        f"new host verb(s) {sorted(unclassified)} with no wait verdict")
    stale = set(HOST_VERB_WAIT_EDGE) - set(_HOST_ARG_SIG)
    assert not stale, f"classified host verb(s) no longer on the frontier: {sorted(stale)}"
    for verb, wid in HOST_VERB_WAIT_EDGE.items():
        assert wid is None or wid in WAIT_EDGES, (verb, wid)


def test_the_host_frontier_has_no_blocking_primitive():
    """The `pool_job` row's evidence, and the "no mutex, semaphore, channel or
    queue" row of §5 made mechanical.

    `_HOST_ARG_SIG` is the complete host frontier; a pool connection is
    borrowed for one call and exhaustion RAISES rather than blocking, so there
    is no `acquire`/`release` pair and nothing to hold. A verb wearing one of
    the synchronisation names would be a genuinely new wait primitive with a
    genuinely new cycle question, and it reds here.
    """
    for verb in _HOST_ARG_SIG:
        method = verb.split(".")[-1]
        assert method not in BLOCKING_HOST_VERB_NAMES, (
            f"host verb `{verb}` is a synchronisation primitive - it can block "
            "a fiber on a resource another component holds, which is a wait "
            "edge with no row in WAIT_EDGES")


def test_the_surface_grammar_has_no_synchronisation_keyword():
    """The same claim one level up: nothing in the language spells a lock."""
    assert not (KEYWORDS & SYNC_PRIMITIVE_NAMES)


# --- the executable evidence the rows cite -------------------------------- #

EMIT_CALL_CYCLE = """
service Ledger { emission fn balance(k: Str) -> Int }
service Audit  { emission fn record(k: Str) -> Int }

component LedgerSvc requires audit: Audit provides ledger: Ledger {
  provide ledger {
    fn balance(k) {
      emit audit.record(k)
    }
  }
}

component AuditSvc requires ledger: Ledger provides audit: Audit {
  provide audit {
    fn record(k) {
      emit ledger.balance(k)
    }
  }
}
"""

SPAWNABLE = """
service Store { fn put(v: Str) -> Str }

component Shard provides store: Store {
  provide store { fn put(v) = v }
}

component Router provides store: Store {
  let s = effect spawn Shard undo s.dispose()
  provide store { fn put(v) = s.store.put(v) }
}
"""


def test_an_emit_call_cycle_is_a_requires_cycle():
    """`emit_injected_key`'s row. A call on an injected key follows a
    `requires` edge, so a call cycle IS a `requires` cycle and G3 names it -
    there is no separate call graph to walk."""
    with pytest.raises(RevlError) as exc:
        compile_source(EMIT_CALL_CYCLE, "callcycle.rvl")
    message = str(exc.value)
    assert "dependency cycle" in message and "(G3)" in message


def test_a_spawned_instances_provisions_do_not_enter_the_link_table():
    """`emit_spawn_handle`'s row. A spawned instance's provisions go into a
    fresh local realm nobody else can name, so no other component's `inject`
    can reach them: the wait relation over spawned instances is a tree rooted
    at the spawning activation, and a tree has no cycle.

    Mechanically: `Router` spawns `Shard`, both are DECLARED components, and
    the linked manifest carries only `Router`. `Shard`'s `store` provision is
    nowhere in the link table, so no other component's `inject` can name it -
    which is also why `Router` can provide `store` itself without a G2
    conflict. The spawn handle is `Router`'s own binding, not an injected key,
    so `Router` waits on nobody.
    """
    ir = compile_source(SPAWNABLE, "spawn.rvl")
    assert {c["name"] for c in ir["components"]} == {"Shard", "Router"}
    entries = ir["manifest"]["components"]
    assert [e["name"] for e in entries] == ["Router"]
    assert ir["manifest"]["loadOrder"] == ["Router"]
    router = entries[0]
    assert not (router.get("inject") or [])


def test_every_seam_carries_a_finite_deadline_by_default():
    """`seam_call_runtime`'s row. The waits that can wedge a RUNNING system are
    below the composition IR; item 54's answer is that every seam call carries
    a deadline, and placement always stamps a finite default "because a placed
    composition is exactly where an unbounded cross-process wait is
    unacceptable"."""
    assert _placement.DEFAULT_SEAM_DEADLINE is not None
    assert float(_placement.DEFAULT_SEAM_DEADLINE) > 0


def test_an_arrow_cannot_reach_an_async_operation():
    """`arrow_across_service`'s row. The re-entrant edge an arrow opens
    (`C -> P -> C's closure -> P`) is invisible to G3, and it does not need to
    be visible: an arrow reaching an async operation is refused (A1), so the
    worst case is divergence, not deadlock."""
    source = (ROOT / "examples" / "rejections" / "a1_async_arrow_sync_type.rvl")
    with pytest.raises(RevlError) as exc:
        compile_source(source.read_text(encoding="utf-8"), source.name)
    assert "(A1)" in str(exc.value)


def test_a_teardown_inverse_cannot_suspend():
    """`teardown_lifo`'s row. Teardown is sequential over the reverse of a
    valid topological order, and a suspension in an `undo` is statically
    refused - so the LIFO cannot wait on anything G3 did not order."""
    source = (ROOT / "examples" / "rejections" / "a1_async_undo_suspends.rvl")
    with pytest.raises(RevlError) as exc:
        compile_source(source.read_text(encoding="utf-8"), source.name)
    message = str(exc.value)
    assert "teardown is synchronous on every tier" in message


def test_there_is_no_future_or_task_value_to_await_across_a_boundary():
    """`divert_await`'s row. `Async[T]` is position-restricted and is not a
    value type: nothing one component awaits can be completed by another,
    because there is no future, promise or task value to hand over."""
    source = (ROOT / "examples" / "rejections" / "a1_await_in_method.rvl")
    with pytest.raises(RevlError) as exc:
        compile_source(source.read_text(encoding="utf-8"), source.name)
    assert "(A1)" in str(exc.value)
