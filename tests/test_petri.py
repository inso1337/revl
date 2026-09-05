"""The Petri engine's math, tested on bare nets with no revl in sight (roadmap
item 438). Enablement, firing, bounded reachability with its two caps, dead
markings and dead transitions, and P-semiflows by Martinez-Silva on nets whose
invariants are known by hand."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.petri import (  # noqa: E402
    Net, Transition, canon, covered_places, p_semiflows, reachable,
    structurally_bounded,
)


# ------------------------------------------------------------------ enablement


def test_enablement_reads_do_not_consume():
    """A read arc gates enablement but leaves the token in place, so two readers
    of one token are both enabled and firing one does not disable the other."""
    reader = Transition("r", read={"p": 1}, produce={"done_r": 1})
    m = {"p": 1}
    assert reader.enabled(m)
    fired = reader.fire(m)
    assert fired["p"] == 1  # the read token survives
    # a second reader is still enabled at the post-fire marking
    reader2 = Transition("r2", read={"p": 1})
    assert reader2.enabled(fired)


def test_consume_removes_tokens_and_can_disable():
    t = Transition("t", consume={"p": 2}, produce={"q": 1})
    assert not t.enabled({"p": 1})
    assert t.enabled({"p": 2})
    assert t.fire({"p": 3}) == {"p": 1, "q": 1}


def test_canon_drops_zeros_and_sorts():
    assert canon({"b": 0, "a": 2, "c": 1}) == (("a", 2), ("c", 1))


# --------------------------------------------------------------- reachability


def test_pipeline_all_transitions_fire_no_deadlock():
    """A -> B -> C monotone pipeline of fire-once activations (each consumes its
    own control token, reads the prior stage's provision, produces its own).
    Every activation completes; the one terminal marking has NO control token
    left over, so it is a clean quiescent state, not a deadlock."""
    net = Net([
        Transition("A", consume={"ctlA": 1}, produce={"a": 1, "doneA": 1}),
        Transition("B", consume={"ctlB": 1}, read={"a": 1}, produce={"b": 1, "doneB": 1}),
        Transition("C", consume={"ctlC": 1}, read={"b": 1}, produce={"c": 1, "doneC": 1}),
    ])
    r = reachable(net, {"ctlA": 1, "ctlB": 1, "ctlC": 1})
    assert r.fired == {"A", "B", "C"}
    assert r.dead_transitions == set()
    # exactly one terminal marking, and no control token survives it:
    assert len(r.dead_markings) == 1
    leftover = {p for p, _ in r.dead_markings[0] if p.startswith("ctl")}
    assert leftover == set()
    assert r.conclusive()


def test_starvation_deadlock_one_consumer_wins_the_token():
    """One producer mints a single single-consumer token; two fire-once
    consumers each need it. Whichever fires first consumes it, so the other is
    permanently starved: every terminal marking leaves the loser's control
    token unconsumed -- a genuine deadlock. This is the shape revl.liveness
    derives for a single-consumer coeffect contended by two consumers, the
    interleaving a pointwise check cannot see."""
    net = Net([
        Transition("prod", consume={"ctlP": 1}, produce={"tok": 1}),
        Transition("A", consume={"ctlA": 1, "tok": 1}, produce={"doneA": 1}),
        Transition("B", consume={"ctlB": 1, "tok": 1}, produce={"doneB": 1}),
    ])
    r = reachable(net, {"ctlP": 1, "ctlA": 1, "ctlB": 1})
    # both orderings reachable, so both A and B fire on SOME path...
    assert r.fired == {"prod", "A", "B"}
    # ...yet every terminal marking has one consumer done and the OTHER's
    # control token stranded -- the starved activation:
    assert r.dead_markings
    for dead in r.dead_markings:
        keys = dict(dead)
        starved = {"ctlA", "ctlB"} & set(keys)
        done = {"doneA", "doneB"} & set(keys)
        assert len(starved) == 1 and len(done) == 1
    assert r.conclusive()


def test_mutual_hold_and_wait_total_deadlock():
    """Two transitions each hold a token the other needs (a resource-ordering
    deadlock). From the state where each grabbed one lock, neither can proceed:
    a total deadlock with both completion transitions dead."""
    net = Net([
        # grab: each activation takes its own lock first
        Transition("A_grab_l1", consume={"l1": 1}, produce={"A_has_l1": 1}),
        Transition("B_grab_l2", consume={"l2": 1}, produce={"B_has_l2": 1}),
        # finish: needs BOTH locks
        Transition("A_fin", consume={"A_has_l1": 1, "l2": 1}, produce={"A_done": 1}),
        Transition("B_fin", consume={"B_has_l2": 1, "l1": 1}, produce={"B_done": 1}),
    ])
    r = reachable(net, {"l1": 1, "l2": 1})
    # the interleaving A_grab_l1 then B_grab_l2 strands both finishers
    stuck = [d for d in r.dead_markings
             if dict(d).get("A_has_l1") and dict(d).get("B_has_l2")]
    assert stuck, "expected the hold-and-wait dead marking"
    for d in stuck:
        keys = dict(d)
        assert "A_done" not in keys and "B_done" not in keys


def test_reachability_bound_is_reported_not_hidden():
    """An unbounded producer (tokens pile on `p` forever) must trip a cap and
    say so, never silently return a truncated 'no deadlock'."""
    net = Net([Transition("pump", produce={"p": 1})])
    r = reachable(net, {}, max_tokens=8)
    assert not r.conclusive()
    assert r.token_overflow


def test_state_cap_sets_bound_hit():
    net = Net([
        Transition("t0", produce={"p0": 1}),
        Transition("t1", read={"p0": 1}, produce={"p1": 1}),
    ])
    r = reachable(net, {}, max_states=1)
    assert r.bound_hit
    assert not r.conclusive()


# ------------------------------------------------------------- P-semiflows


def test_state_machine_conserves_token_count():
    """A token circulating a 3-place ring (t0: p0->p1, t1: p1->p2, t2: p2->p0)
    conserves total tokens, so the all-ones vector is a P-semiflow and every
    place is covered -> structurally bounded."""
    net = Net([
        Transition("t0", consume={"p0": 1}, produce={"p1": 1}),
        Transition("t1", consume={"p1": 1}, produce={"p2": 1}),
        Transition("t2", consume={"p2": 1}, produce={"p0": 1}),
    ])
    flows = p_semiflows(net)
    assert {"p0": 1, "p1": 1, "p2": 1} in flows
    assert covered_places(net) == {"p0", "p1", "p2"}
    assert structurally_bounded(net)


def test_weighted_invariant_recovered():
    """t consumes 2 from p and puts 1 on q; the conserved sum is p + 2q, i.e.
    the semiflow {p:1, q:2}."""
    net = Net([Transition("t", consume={"p": 2}, produce={"q": 1})])
    flows = p_semiflows(net)
    assert {"p": 1, "q": 2} in flows


def test_unbounded_place_is_not_covered():
    """`pump` only ever adds to `p`: no positive invariant carries `p`, so it is
    uncovered and the net is not certified bounded."""
    net = Net([Transition("pump", produce={"p": 1})])
    assert "p" not in covered_places(net)
    assert not structurally_bounded(net)


def test_producer_consumer_pair_is_bounded():
    """p produces onto buf, c consumes from it: buf + (in-flight) is conserved.
    A mutual-exclusion style net whose places are all covered."""
    net = Net([
        Transition("acquire", consume={"free": 1}, produce={"busy": 1}),
        Transition("release", consume={"busy": 1}, produce={"free": 1}),
    ])
    assert structurally_bounded(net)
    assert {"free": 1, "busy": 1} in p_semiflows(net)
