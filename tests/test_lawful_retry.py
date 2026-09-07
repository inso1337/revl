"""Lawful-retry dispatcher (#480, Primitive 2).

These pin the effect-class decision table (pure/idempotent-external freely or
boundedly replayed; witnessed handed off; deferred-irreversible held at a commit
authority; secret-bearing gated to attested+ peers), and the two brackets the
design promises: every REPLAY passes ``peer_authority.check_delegation_chain``
(a retry may narrow, never widen; the retry budget runs out down the chain) and
every replay peer clears ``peer_offer.offer_eligible``. Fail-closed cases — an
exhausted budget, no eligible peer, a widening retry hop, a bare `verified` peer
for a secret — all refuse rather than widen.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import peer_offer  # noqa: E402
from revl.lawful_retry import (  # noqa: E402
    Attempt,
    Decision,
    Disposition,
    EffectClass,
    classify_extern,
    dispatch_on_loss,
)
from revl.peer_authority import Grant  # noqa: E402

KEY = b"pool-shared-secret"


def offer(peer_id="peer-2", trust="verified",
          ceiling=('fs.read(path="/data")', "net.fetch")):
    return peer_offer.sign_offer(
        peer_offer.PeerOffer(
            peer_id=peer_id,
            attestation=peer_offer.Attestation(
                trust=trust,
                resource_offer=peer_offer.ResourceOffer(4, 1 << 30, 300.0)),
            grant_ceiling=tuple(ceiling)),
        KEY)


def base_slot(trust_floor="verified"):
    return peer_offer.PlacementSlot(trust_floor=trust_floor)


def attempt(effect_class, caps=('fs.read(path="/data/jobs")',), retries=3,
            has_inverse=False, trust_floor="verified"):
    return Attempt(
        effect_class=effect_class,
        grant=Grant("lost-peer", tuple(caps), {"retries": retries}),
        has_recorded_inverse=has_inverse,
        base_slot=base_slot(trust_floor))


# --------------------------------------------------------------- classify


def test_classify_precedence_secret_wins():
    assert classify_extern({"secret": True, "idempotent": True}) is EffectClass.SECRET_BEARING
    assert classify_extern({"deferred": True}) is EffectClass.DEFERRED_IRREVERSIBLE
    assert classify_extern({"witnessed": True}) is EffectClass.WITNESSED
    assert classify_extern({"idempotent": True}) is EffectClass.IDEMPOTENT_EXTERNAL
    assert classify_extern({"undo_idempotent": True}) is EffectClass.IDEMPOTENT_EXTERNAL
    assert classify_extern({}) is EffectClass.PURE


# ------------------------------------------------------ non-replay classes


def test_witnessed_is_a_durable_handoff_not_a_replay():
    d = dispatch_on_loss([Grant("root")], attempt(EffectClass.WITNESSED),
                         candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.DURABLE_HANDOFF
    assert d.peer_id is None


def test_deferred_irreversible_only_at_commit_authority():
    d = dispatch_on_loss([Grant("root")],
                         attempt(EffectClass.DEFERRED_IRREVERSIBLE),
                         candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.COMMIT_AUTHORITY_ONLY


# --------------------------------------------------------------- pure replay


def test_pure_replays_to_an_eligible_peer():
    root = Grant("root", ('fs.read(path="/data")',), {"retries": 9})
    lost = Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 3})
    d = dispatch_on_loss(
        [root, lost],
        Attempt(EffectClass.PURE, lost, base_slot=base_slot()),
        candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.REPLAY
    assert d.peer_id == "peer-2"
    # the retry grant narrowed the budget by one (5->... here 3->2)
    assert d.retry_grant.budgets["retries"] == 2


def test_replay_refuses_when_no_peer_is_eligible():
    # the only candidate's ceiling does not cover the retry grant
    narrow = offer(ceiling=("net.fetch",))
    root = Grant("root", ('fs.read(path="/data")',), {"retries": 9})
    lost = Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 3})
    d = dispatch_on_loss([root, lost],
                         Attempt(EffectClass.PURE, lost, base_slot=base_slot()),
                         candidates=[narrow], key=KEY)
    assert d.disposition is Disposition.REFUSE
    assert "no eligible peer" in d.reason


def test_replay_refuses_with_no_candidates():
    root = Grant("root", ('fs.read(path="/data")',), {"retries": 9})
    lost = Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 3})
    d = dispatch_on_loss([root, lost],
                         Attempt(EffectClass.PURE, lost, base_slot=base_slot()),
                         candidates=[], key=KEY)
    assert d.disposition is Disposition.REFUSE


# --------------------------------------------- idempotent-external & budget


def test_idempotent_replays_while_budget_remains():
    d = dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',), {"retries": 9})],
        attempt(EffectClass.IDEMPOTENT_EXTERNAL, retries=2),
        candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.REPLAY


def test_exhausted_budget_compensates_when_inverse_recorded():
    d = dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',))],
        attempt(EffectClass.IDEMPOTENT_EXTERNAL, retries=0, has_inverse=True),
        candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.COMPENSATE


def test_exhausted_budget_refuses_with_no_inverse():
    d = dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',))],
        attempt(EffectClass.IDEMPOTENT_EXTERNAL, retries=0, has_inverse=False),
        candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.REFUSE


def test_a_grant_naming_no_retry_budget_cannot_be_replayed():
    lost = Grant("lost-peer", ('fs.read(path="/data/jobs")',))  # no `retries`
    d = dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',))],
        Attempt(EffectClass.PURE, lost, base_slot=base_slot()),
        candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.REFUSE
    assert "no retry budget" in d.reason


# ----------------------------------------------------- secret-bearing gate


def test_secret_bearing_refuses_a_bare_verified_peer():
    # design: "never a bare verified offer". A verified peer is skipped; the
    # only candidate cannot clear the attested floor, so the dispatch refuses.
    d = dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',), {"retries": 9})],
        attempt(EffectClass.SECRET_BEARING),
        candidates=[offer(trust="verified")], key=KEY)
    assert d.disposition is Disposition.REFUSE
    assert "below the slot floor" in d.reason


def test_secret_bearing_replays_to_an_attested_peer():
    d = dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',), {"retries": 9})],
        attempt(EffectClass.SECRET_BEARING),
        candidates=[offer(trust="attested")], key=KEY)
    assert d.disposition is Disposition.REPLAY
    assert d.peer_id == "peer-2"


def test_secret_floor_is_never_lowered_by_the_base_slot():
    # even if the base slot only asked for `verified`, a secret keeps `attested`.
    d = dispatch_on_loss(
        [Grant("root", ('fs.read(path="/data")',), {"retries": 9})],
        Attempt(EffectClass.SECRET_BEARING,
                Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 3}),
                base_slot=base_slot(trust_floor="verified")),
        candidates=[offer(trust="verified")], key=KEY)
    assert d.disposition is Disposition.REFUSE


# ---------------------------------------------- monotonicity bracket (Prim 3)


def test_the_retry_grant_only_narrows_the_lost_attempt():
    # the dispatcher derives the retry grant from the LOST grant by narrowing
    # (same caps, decremented budget), so a widening retry is impossible to
    # construct — the retry grant can never grant a cap the lost attempt lacked.
    root = Grant("root", ('fs.read(path="/data")',), {"retries": 9})
    lost = Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 3})
    d = dispatch_on_loss([root, lost],
                         Attempt(EffectClass.PURE, lost, base_slot=base_slot()),
                         candidates=[offer(ceiling=('fs.read(path="/data")', "net.fetch"))],
                         key=KEY)
    assert d.disposition is Disposition.REPLAY
    assert d.retry_grant.caps == lost.caps            # no cap added
    assert d.retry_grant.budgets["retries"] == 2      # budget only shrank


def test_a_non_monotone_chain_refuses_the_replay_fail_closed():
    # the monotonicity bracket validates the WHOLE chain including the new hop:
    # if the chain handed in already widens (agent re-widened past root), the
    # replay is refused rather than appended onto an unlawful chain.
    root = Grant("root", ('fs.read(path="/data/jobs")',), {"retries": 9})
    agent = Grant("agent", ('fs.read(path="/data")',), {"retries": 5})  # widened!
    lost = Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 3})
    d = dispatch_on_loss(
        [root, agent, lost],
        Attempt(EffectClass.PURE, lost, base_slot=base_slot()),
        candidates=[offer(ceiling=('fs.read(path="/data")', "net.fetch"))],
        key=KEY)
    assert d.disposition is Disposition.REFUSE


def test_retry_budget_runs_out_down_a_replay_chain():
    # each replay decrements the budget by one; a one-budget attempt yields a
    # zero-budget retry grant, so a NEXT loss on that grant cannot replay again.
    root = Grant("root", ('fs.read(path="/data")',), {"retries": 9})
    lost = Grant("lost-peer", ('fs.read(path="/data/jobs")',), {"retries": 1})
    d = dispatch_on_loss([root, lost],
                         Attempt(EffectClass.PURE, lost, base_slot=base_slot()),
                         candidates=[offer()], key=KEY)
    assert d.disposition is Disposition.REPLAY
    assert d.retry_grant.budgets["retries"] == 0
    # feed the retry grant back as the next lost attempt: no budget left.
    nxt = dispatch_on_loss(
        [root, lost, d.retry_grant],
        Attempt(EffectClass.PURE, d.retry_grant, base_slot=base_slot()),
        candidates=[offer()], key=KEY)
    assert nxt.disposition is Disposition.REFUSE
