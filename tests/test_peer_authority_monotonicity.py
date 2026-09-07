"""Authority-monotonicity invariant for the verifiable private peer pool
(#480, Slice 1).

The invariant under test: a peer may receive no more authority, data, time,
money, or retry budget than the delegating composition possesses — authority
may only NARROW along a delegation edge and along a retry reissue, never widen.
These pin the two dimensions (capabilities via `cap_order.covers`, scalar
budgets via the non-increasing/fail-closed rule), the chain walk and its
transitivity, that a retry hop is held to the same rule as a fresh delegation,
and that every ambiguous case refuses (malformed spelling, budget absent
upstream). Grounded on the same `cap_order` algebra the spawn-attenuation fold
uses, so a cone/ceiling widening is caught here exactly as it is on spawn.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import cap_order  # noqa: E402
from revl.peer_authority import (  # noqa: E402
    AuthorityWidening,
    Grant,
    chain_is_monotone,
    check_delegation_chain,
    check_hop,
    grant_widenings,
)


def g(holder: str, caps=(), **budgets) -> Grant:
    return Grant(holder, tuple(caps), dict(budgets))


# --------------------------------------------------------------- capabilities


def test_identity_delegation_is_monotone():
    root = g("root", ["net.fetch", "fs.read"])
    peer = g("peer", ["net.fetch", "fs.read"])
    check_delegation_chain([root, peer])  # does not raise
    assert chain_is_monotone([root, peer])


def test_dropping_a_capability_narrows():
    root = g("root", ["net.fetch", "fs.read", "db.write"])
    peer = g("peer", ["net.fetch"])
    check_delegation_chain([root, peer])
    assert grant_widenings(root, peer) == ((), ())


def test_granting_a_capability_not_held_widens():
    root = g("root", ["net.fetch"])
    peer = g("peer", ["net.fetch", "db.write"])
    with pytest.raises(AuthorityWidening) as ei:
        check_delegation_chain([root, peer])
    assert ei.value.caps == ("db.write",)
    assert ei.value.index == 1
    assert ei.value.holder == "peer"
    assert ei.value.delegator == "root"


def test_resource_cone_subset_narrows():
    root = g("root", ['fs.read(path="/data")'])
    peer = g("peer", ['fs.read(path="/data/incoming")'])  # sub-cone
    check_delegation_chain([root, peer])


def test_resource_cone_superset_widens():
    root = g("root", ['fs.read(path="/data/incoming")'])
    peer = g("peer", ['fs.read(path="/data")'])  # a wider cone
    with pytest.raises(AuthorityWidening):
        check_delegation_chain([root, peer])


def test_dropping_a_resource_parameter_widens():
    # a bare token is the top of its cone; dropping `path=` widens (matches
    # cap_order/spawn-attenuation semantics).
    root = g("root", ['fs.read(path="/data")'])
    peer = g("peer", ["fs.read"])
    with pytest.raises(AuthorityWidening) as ei:
        check_delegation_chain([root, peer])
    assert ei.value.caps == ("fs.read",)


# ----------------------------------------------- time & data ride on ceilings


def test_smaller_time_ceiling_narrows():
    root = g("root", ['model.complete(time="30s")'])
    peer = g("peer", ['model.complete(time="10s")'])
    check_delegation_chain([root, peer])


def test_larger_time_ceiling_widens():
    root = g("root", ['model.complete(time="10s")'])
    peer = g("peer", ['model.complete(time="30s")'])
    with pytest.raises(AuthorityWidening):
        check_delegation_chain([root, peer])


def test_larger_data_ceiling_widens():
    root = g("root", ['fs.read(path="/data", size="1MB")'])
    peer = g("peer", ['fs.read(path="/data", size="10MB")'])
    with pytest.raises(AuthorityWidening):
        check_delegation_chain([root, peer])


# ------------------------------------------------ money & retry-budget scalars


def test_retry_budget_may_only_shrink():
    root = g("root", ["net.fetch"], retries=5)
    peer = g("peer", ["net.fetch"], retries=2)
    check_delegation_chain([root, peer])


def test_retry_budget_increase_widens():
    root = g("root", ["net.fetch"], retries=2)
    peer = g("peer", ["net.fetch"], retries=5)
    with pytest.raises(AuthorityWidening) as ei:
        check_delegation_chain([root, peer])
    assert ei.value.budgets == (("retries", 2, 5),)


def test_money_budget_absent_upstream_widens_fail_closed():
    # the delegator holds no `money` budget: it cannot hand any down.
    root = g("root", ["net.fetch"], retries=5)
    peer = g("peer", ["net.fetch"], retries=5, money=100)
    with pytest.raises(AuthorityWidening) as ei:
        check_delegation_chain([root, peer])
    assert ei.value.budgets == (("money", None, 100),)


def test_zero_budget_never_widens():
    root = g("root", ["net.fetch"], retries=3)
    peer = g("peer", ["net.fetch"], retries=3, money=0)  # holds none, grants none
    check_delegation_chain([root, peer])


def test_negative_budget_refused():
    root = g("root", ["net.fetch"], retries=3)
    peer = g("peer", ["net.fetch"], retries=-1)
    with pytest.raises(ValueError):
        check_delegation_chain([root, peer])


# -------------------------------------------------------- chain & transitivity


def test_multi_hop_narrowing_chain():
    chain = [
        g("root", ['fs.read(path="/data")', "net.fetch"], retries=8),
        g("agent", ['fs.read(path="/data/jobs")', "net.fetch"], retries=4),
        g("peer", ['fs.read(path="/data/jobs/42")'], retries=1),
    ]
    check_delegation_chain(chain)
    assert chain_is_monotone(chain)


def test_widening_detected_at_the_offending_hop():
    chain = [
        g("root", ['fs.read(path="/data")'], retries=8),
        g("agent", ['fs.read(path="/data/jobs")'], retries=4),
        g("peer", ['fs.read(path="/data")'], retries=4),  # re-widens vs agent
    ]
    with pytest.raises(AuthorityWidening) as ei:
        check_delegation_chain(chain)
    assert ei.value.index == 2
    assert ei.value.delegator == "agent"
    assert ei.value.holder == "peer"


def test_grant_covered_by_root_but_not_immediate_delegator_still_refused():
    # peer's grant is within the ROOT's authority, but the immediate delegator
    # (agent) already narrowed past it: agent cannot hand down what it no longer
    # holds. The chain is checked edge-by-edge, not against the root.
    chain = [
        g("root", ["net.fetch", "db.write"]),
        g("agent", ["net.fetch"]),          # dropped db.write
        g("peer", ["net.fetch", "db.write"]),  # tries to reacquire it
    ]
    with pytest.raises(AuthorityWidening) as ei:
        check_delegation_chain(chain)
    assert ei.value.caps == ("db.write",)
    assert ei.value.index == 2


def test_empty_and_single_chains_are_vacuously_monotone():
    check_delegation_chain([])
    check_delegation_chain([g("root", ["net.fetch"])])
    assert chain_is_monotone([])


# -------------------------------------------------------------- retry reissue


def test_retry_reissue_held_to_the_same_rule():
    # a "retry" is just a later hop: it may not widen the attempt it retries.
    attempt = g("peer@t1", ['net.fetch(host="a.internal")'], retries=3)
    retry = g("peer@t2", ['net.fetch(host="a.internal")'], retries=2)
    check_hop(1, attempt, retry)  # narrower retry: fine

    wider_retry = g("peer@t2", ['net.fetch'], retries=2)  # dropped host cone
    with pytest.raises(AuthorityWidening):
        check_hop(1, attempt, wider_retry)


# ------------------------------------------------------------------ fail-close


def test_malformed_capability_spelling_raises_not_admits():
    root = g("root", ["fs.read(bogus=1)"])  # unregistered parameter -> CapError
    peer = g("peer", ["fs.read"])
    with pytest.raises(cap_order.CapError):
        check_delegation_chain([root, peer])
    # and the boolean form does not swallow it into a `True`
    with pytest.raises(cap_order.CapError):
        chain_is_monotone([root, peer])


def test_error_message_names_the_edge_and_the_widening():
    root = g("root", ["net.fetch"], retries=2)
    peer = g("peer", ["net.fetch", "db.write"], retries=9)
    with pytest.raises(AuthorityWidening) as ei:
        check_delegation_chain([root, peer])
    msg = str(ei.value)
    assert "`root`" in msg and "`peer`" in msg
    assert "db.write" in msg
    assert "retries" in msg
    assert "narrow" in msg
