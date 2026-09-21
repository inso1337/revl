"""The private-pool admission gate (roadmap item 524, issue #1198).

Four things are held here, in the order they matter.

1. NON-VACUITY. Before this change the only check a peer faced was
   ``peer_offer.verify_offer``. There was no pool for it to join, so nothing
   refused it on pool grounds. The corpus below is a set of join requests that
   ALL clear that pre-existing check and that the gate refuses, each on a
   different named link, plus a control that both admit. A gate that refuses
   nothing and a gate that refuses everything are both excluded by the same
   table.

2. EVERY NAMED REFUSAL IS REACHED, and no link is named that is not. Held both
   by the corpus and by an AST walk over the module's own ``_refusal`` calls, so
   a link cannot be declared and left unreachable (which reads as a gate that
   exists) nor reached and left unnamed.

3. THE AUTHORITY DIFF IS A PRECONDITION, not a stage (item 518's shape). Proved
   structurally: an AST walk asserts a ``Membership`` is constructed in exactly
   one function, that the tier grant is computed in exactly one function, and
   that every function that issues a membership also runs the diff. A behavioural
   test cannot prove this: it can only show the orderings it happened to try,
   so the structure is asserted directly.

4. WITHDRAWAL IS THREE WORDS (item 546's shape). What a withdrawal restores,
   what it retains with no inverse, and what it orphans are three disjoint sets,
   and a re-admitted identity does not recover the tier its evidence bought.
"""

import ast
import inspect
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import peer_pool as pp  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.lawful_retry import EffectClass  # noqa: E402
from revl.peer_offer import (  # noqa: E402
    Attestation,
    PeerOffer,
    ResourceOffer,
    sign_offer,
    verify_offer,
)

OP_KEY = b"pool-operator-key-1198"
ATTEST_KEY = b"pool-attesting-key-1198"
ALPHA_KEY = b"peer-alpha-key-1198"
MALLORY_KEY = b"peer-mallory-key-1198"

GOOD_ARTIFACT = "a" * 64
OTHER_ARTIFACT = "b" * 64

POOL_CEILING = ('fs.read(path="/data")', 'net.fetch(host="api.internal")')
ENTRY_CAPS = ('fs.read(path="/data/in")',)


def make_charter(**overrides) -> pp.PoolCharter:
    spec = dict(
        pool_id="lab",
        ceiling=POOL_CEILING,
        ceiling_budgets={"retries": 3},
        tiers={
            pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS, budgets={"retries": 1}),
            "replayable": pp.TierGrant(caps=('fs.read(path="/data")',),
                                       budgets={"retries": 2},
                                       evidence_required=3),
        },
        admit_key_ids=(key_id(OP_KEY),),
        revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=(key_id(ATTEST_KEY),),
        artifact_digests=(GOOD_ARTIFACT,),
        trust_floor="verified",
    )
    spec.update(overrides)
    return pp.PoolCharter(**spec)


def make_offer(peer_id="alpha", key=ALPHA_KEY, trust="verified",
               ceiling=('fs.read(path="/data")',)) -> dict:
    offer = PeerOffer(
        peer_id=peer_id,
        attestation=Attestation(trust=trust, region="eu", hardware="x86",
                                resource_offer=ResourceOffer(4, 1 << 30, 60.0)),
        grant_ceiling=ceiling)
    return sign_offer(offer, key)


def make_join(charter_record, *, peer_id="alpha", key=ALPHA_KEY,
              offer=None, artifact=GOOD_ARTIFACT, nonce="n-1",
              pool_id=None, charter_digest=None, issued_at=None,
              extra=None) -> dict:
    """A signed join request. ``extra`` members go INSIDE the signed body, which
    is how a peer that wants to argue for a higher tier would have to do it."""
    join = pp.JoinRequest(
        pool_id=pool_id or charter_record["pool_id"],
        charter_digest=charter_digest or pp.canonical_digest(charter_record),
        peer_id=peer_id,
        offer=offer if offer is not None else make_offer(peer_id, key),
        artifact_digest=artifact,
        nonce=nonce,
        issued_at=issued_at or pp._iso(pp._utc_now()))
    body = join.body()
    if extra:
        body.update(extra)
    body["key_id"] = key_id(key)
    body[pp.SIGNATURE_FIELD] = pp._mac(pp.JOIN_DOMAIN, body, key)
    return body


def fresh_roster(charter_record) -> pp.Roster:
    return pp.Roster(charter_record["pool_id"],
                     pp.canonical_digest(charter_record))


def run_admit(charter_record, join_record, *, roster=None,
              peer_keys=None, admitting=None, now=None) -> dict:
    return pp.admit(
        charter_record, join_record,
        charter_key=OP_KEY,
        peer_keys=peer_keys if peer_keys is not None
        else {"alpha": ALPHA_KEY, "mallory": MALLORY_KEY},
        admitting_key_id=admitting or key_id(OP_KEY),
        roster=roster if roster is not None else fresh_roster(charter_record),
        now=now)


# ---------------------------------------------------------------------------
# the corpus: one entry per join-shaped refusal, plus a control
# ---------------------------------------------------------------------------


def corpus():
    """Join requests the PRE-EXISTING machinery admits and this gate refuses.

    Each entry is ``(name, expected_link, build)`` where ``build(charter_record)``
    returns ``(join_record, kwargs_for_admit)``. Every entry carries a peer offer
    that verifies under a key the pool holds, which is the whole pre-existing
    admission check for a peer, so every entry is baseline-admitted."""

    def unknown_peer(rec):
        return make_join(rec, peer_id="nobody", key=MALLORY_KEY), {
            "peer_keys": {"alpha": ALPHA_KEY}}

    def join_signature(rec):
        join = make_join(rec)
        join["artifact_digest"] = OTHER_ARTIFACT  # edited after signing
        return join, {}

    def pool_identity_name(rec):
        return make_join(rec, pool_id="other-lab"), {}

    def pool_identity_terms(rec):
        return make_join(rec, charter_digest="0" * 64), {}

    def stale_join(rec):
        stale = pp._utc_now() - timedelta(hours=2)
        return make_join(rec, issued_at=pp._iso(stale)), {}

    def replayed_join(rec):
        roster = fresh_roster(rec)
        roster.spent_nonces.add(("alpha", "n-1"))
        return make_join(rec), {"roster": roster}

    def revoked_peer(rec):
        roster = fresh_roster(rec)
        roster.revoked.add("alpha")
        return make_join(rec), {"roster": roster}

    def duplicate_member(rec):
        roster = fresh_roster(rec)
        run_admit(rec, make_join(rec, nonce="first"), roster=roster)
        return make_join(rec, nonce="second"), {"roster": roster}

    def offer_signature(rec):
        # A relayed offer: alpha signs the join correctly but embeds an offer
        # for "alpha" that mallory's key signed, so the identity the pool would
        # grant to is vouched for by a key that is not alpha's.
        return make_join(rec, offer=make_offer("alpha", MALLORY_KEY)), {}

    def offer_identity(rec):
        # The offer verifies under alpha's key, but it describes a different
        # peer: the grant would be issued to alpha on beta's advertised terms.
        return make_join(rec, offer=make_offer("beta", ALPHA_KEY)), {}

    def trust_floor(rec):
        return make_join(rec), {}  # charter override supplies the floor

    def artifact_digest(rec):
        return make_join(rec, artifact=OTHER_ARTIFACT), {}

    def entry_tier(rec):
        # The peer argues for a tier INSIDE its signed body, so the signature is
        # valid and the envelope accepts the extra member. It still enters at
        # the bottom or not at all.
        return make_join(rec, extra={"tier": "replayable"}), {}

    def grant_ceiling_peer(rec):
        # The peer's own advertised ceiling does not cover the entry grant.
        return make_join(rec, offer=make_offer(
            "alpha", ALPHA_KEY, ceiling=('fs.read(path="/tmp")',))), {}

    return [
        ("unknown-peer", pp.LINK_UNKNOWN_PEER, unknown_peer, {}),
        ("edited-after-signing", pp.LINK_JOIN_SIGNATURE, join_signature, {}),
        ("another-pool", pp.LINK_POOL_IDENTITY, pool_identity_name, {}),
        ("another-charter", pp.LINK_POOL_IDENTITY, pool_identity_terms, {}),
        ("stale", pp.LINK_STALE_JOIN, stale_join, {}),
        ("replayed", pp.LINK_REPLAYED_JOIN, replayed_join, {}),
        ("revoked", pp.LINK_REVOKED_PEER, revoked_peer, {}),
        ("already-a-member", pp.LINK_DUPLICATE_MEMBER, duplicate_member, {}),
        ("relayed-offer", pp.LINK_OFFER_SIGNATURE, offer_signature, {}),
        ("offer-for-someone-else", pp.LINK_OFFER_IDENTITY, offer_identity, {}),
        ("below-the-floor", pp.LINK_TRUST_FLOOR, trust_floor,
         {"trust_floor": "attested"}),
        ("unadmitted-artifact", pp.LINK_ARTIFACT_DIGEST, artifact_digest, {}),
        ("asks-for-a-tier", pp.LINK_ENTRY_TIER, entry_tier, {}),
        ("over-the-peer-ceiling", pp.LINK_GRANT_CEILING, grant_ceiling_peer, {}),
    ]


CORPUS = corpus()


def baseline_admits(join_record) -> bool:
    """The whole admission check a peer faced BEFORE this module existed: its
    signed offer verifies under the key the operator holds for it. There was no
    pool, so there was nothing else to fail."""
    offer = join_record.get("offer")
    if not isinstance(offer, dict):
        return False
    for key in (ALPHA_KEY, MALLORY_KEY):
        ok, _ = verify_offer(offer, key)
        if ok:
            return True
    return False


@pytest.mark.parametrize("name,link,build,charter_kwargs",
                         CORPUS, ids=[c[0] for c in CORPUS])
def test_every_corpus_entry_clears_the_old_check_and_this_gate_refuses_it(
        name, link, build, charter_kwargs):
    record = pp.sign_charter(make_charter(**charter_kwargs), OP_KEY)
    join, kwargs = build(record)
    assert baseline_admits(join), (
        f"{name} must clear the pre-existing check, or its refusal here proves "
        f"nothing new")
    receipt = run_admit(record, join, **kwargs)
    assert receipt["verdict"] == pp.REFUSE, f"{name} was admitted"
    assert receipt["link"] == link, (
        f"{name} refused on {receipt['link']!r}, expected {link!r}: "
        f"{receipt['reason']}")


def test_the_control_is_admitted_by_both():
    """A gate that refuses everything proves nothing either. The control differs
    from the corpus only in being well formed."""
    record = pp.sign_charter(make_charter(), OP_KEY)
    join = make_join(record)
    assert baseline_admits(join)
    receipt = run_admit(record, join)
    assert receipt["verdict"] == pp.ADMIT, receipt.get("reason")
    assert receipt["tier"] == pp.ENTRY_TIER
    assert receipt["caps"] == sorted(ENTRY_CAPS)
    assert receipt["effect_ceiling"] == EffectClass.PURE.value


def test_non_vacuity_counts():
    """The numbers, asserted rather than described: every corpus entry is
    admitted by the old check and refused by the new one, on distinct links, and
    the control is admitted by both."""
    admitted_before = 0
    refused_after = 0
    for name, link, build, charter_kwargs in CORPUS:
        record = pp.sign_charter(make_charter(**charter_kwargs), OP_KEY)
        join, kwargs = build(record)
        admitted_before += int(baseline_admits(join))
        refused_after += int(run_admit(record, join, **kwargs)["verdict"]
                             == pp.REFUSE)
    assert admitted_before == len(CORPUS) == refused_after
    assert len({link for _, link, _, _ in CORPUS}) >= 12, (
        "the corpus must spread across the named links, not pile onto one")

    record = pp.sign_charter(make_charter(), OP_KEY)
    control = make_join(record)
    assert baseline_admits(control)
    assert run_admit(record, control)["verdict"] == pp.ADMIT


# ---------------------------------------------------------------------------
# the refusals the corpus cannot reach: charter, authority, promotion
# ---------------------------------------------------------------------------


def test_a_charter_signed_by_another_key_is_refused_before_anything_is_read():
    record = pp.sign_charter(make_charter(), OP_KEY)
    receipt = pp.admit(record, make_join(record), charter_key=b"not-the-key",
                       peer_keys={"alpha": ALPHA_KEY},
                       admitting_key_id=key_id(OP_KEY),
                       roster=fresh_roster(record))
    assert receipt["link"] == pp.LINK_CHARTER_SIGNATURE


def test_an_operator_without_admit_authority_cannot_use_the_gate_as_an_oracle():
    """The authority check runs BEFORE the join is parsed, so a caller with no
    admit authority learns nothing about whether the peer would have passed."""
    record = pp.sign_charter(make_charter(), OP_KEY)
    good = run_admit(record, make_join(record), admitting=key_id(b"someone"))
    bad = run_admit(record, make_join(record, artifact=OTHER_ARTIFACT),
                    admitting=key_id(b"someone"))
    assert good["link"] == bad["link"] == pp.LINK_ADMITTING_AUTHORITY
    assert good["reason"] == bad["reason"], (
        "an unauthorized caller must not be able to tell a good join from a bad "
        "one by the refusal it gets back")


def test_the_key_that_may_admit_is_not_automatically_the_key_that_may_revoke():
    charter = make_charter(revoke_key_ids=(key_id(b"revoker"),))
    record = pp.sign_charter(charter, OP_KEY)
    roster = fresh_roster(record)
    assert run_admit(record, make_join(record), roster=roster)["verdict"] == pp.ADMIT
    receipt = pp.withdraw(record, "alpha", "because", charter_key=OP_KEY,
                          roster=roster, revoking_key_id=key_id(OP_KEY))
    assert receipt["link"] == pp.LINK_ADMITTING_AUTHORITY
    assert "alpha" in roster.members, "a refused withdrawal must not remove"


def test_promotion_without_evidence_leaves_the_member_where_it_was():
    """The failure direction of the arrow that raises authority: a peer whose
    evidence is missing keeps the authority it had. It does not inherit the
    tier it asked for, and it is not demoted either."""
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    receipt = pp.promote(record, "alpha", "replayable", charter_key=OP_KEY,
                         roster=roster, receipts=[])
    assert receipt["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert receipt["stays_at"] == pp.ENTRY_TIER
    assert roster.members["alpha"].tier == pp.ENTRY_TIER
    assert roster.members["alpha"].caps == tuple(sorted(ENTRY_CAPS))


def test_an_evidence_count_cannot_be_stated_at_all():
    """The fail-open item 524's receipt slice closes. `Membership.evidence` was
    an integer a caller handed in and the promotion gate believed it; a peer
    that states its own evidence count can state any evidence count.

    It is now derived from the receipt digests the pool verified, so there is
    no argument left to state it through. Asserted at the two places a number
    used to enter: the constructor, and the roster file on disk."""
    with pytest.raises(TypeError):
        pp.Membership(peer_id="alpha", tier=pp.ENTRY_TIER, caps=(), budgets={},
                      artifact_digest=GOOD_ARTIFACT, admitted_at="t",
                      evidence=99)
    with pytest.raises(TypeError):
        pp._issue_membership("alpha", pp.ENTRY_TIER,
                             pp.peer_authority.Grant(holder="peer:alpha",
                                                     caps=(), budgets={}),
                             GOOD_ARTIFACT, "t", evidence=99)

    # A roster file that claims a count is read as claiming nothing. The member
    # is otherwise intact, so this is the count being ignored and not the row
    # being refused.
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    on_disk = roster.as_dict()
    on_disk["members"]["alpha"]["evidence"] = 99
    reloaded = pp.Roster.from_dict(on_disk)
    assert reloaded.members["alpha"].evidence == 0
    assert reloaded.members["alpha"].tier == pp.ENTRY_TIER


def test_a_member_with_stored_digests_and_no_receipts_is_not_promoted():
    """The digests a roster carries are a RECORD of what was counted, never an
    input to the count. A roster that lists three digests and a caller that
    presents no receipts gets the authority of a peer with no evidence."""
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    roster.members["alpha"] = pp._issue_membership(
        "alpha", pp.ENTRY_TIER, roster.members["alpha"].grant(), GOOD_ARTIFACT,
        "t", evidence_digests=("d0" * 32, "d1" * 32, "d2" * 32))
    assert roster.members["alpha"].evidence == 3
    receipt = pp.promote(record, "alpha", "replayable", charter_key=OP_KEY,
                         roster=roster, receipts=[])
    assert receipt["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert receipt["has"] == 0, "the count comes from the receipts, not the row"
    assert roster.members["alpha"].tier == pp.ENTRY_TIER


def test_promotion_and_not_a_member_and_unknown_tier_are_named():
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    assert pp.promote(record, "ghost", "replayable", charter_key=OP_KEY,
                      roster=roster,
                      receipts=[])["link"] == pp.LINK_NOT_A_MEMBER
    assert pp.withdraw(record, "ghost", "x", charter_key=OP_KEY, roster=roster,
                       revoking_key_id=key_id(OP_KEY)
                       )["link"] == pp.LINK_NOT_A_MEMBER
    run_admit(record, make_join(record), roster=roster)
    assert pp.promote(record, "alpha", "durable", charter_key=OP_KEY,
                      roster=roster,
                      receipts=[])["link"] == pp.LINK_UNKNOWN_TIER


def test_a_tier_grant_wider_than_the_pool_ceiling_is_refused_at_every_rung():
    """Both entry and promotion diff against the CHARTER CEILING. A charter that
    declares a tier it does not itself hold cannot issue that tier to anybody,
    and the rung below it does not launder it."""
    charter = make_charter(tiers={
        pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS),
        "replayable": pp.TierGrant(caps=('fs.read(path="/etc")',),
                                   evidence_required=0),
    })
    record = pp.sign_charter(charter, OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    receipt = pp.promote(record, "alpha", "replayable", charter_key=OP_KEY,
                         roster=roster, receipts=[])
    assert receipt["link"] == pp.LINK_GRANT_CEILING
    assert 'fs.read(path="/etc")' in receipt["widened_caps"]
    assert roster.members["alpha"].tier == pp.ENTRY_TIER


def test_an_entry_grant_wider_than_the_ceiling_admits_nobody():
    charter = make_charter(tiers={
        pp.ENTRY_TIER: pp.TierGrant(caps=('net.fetch(host="*")',)),
    })
    record = pp.sign_charter(charter, OP_KEY)
    receipt = run_admit(record, make_join(
        record, offer=make_offer("alpha", ALPHA_KEY,
                                ceiling=('net.fetch(host="*")',))))
    assert receipt["link"] == pp.LINK_GRANT_CEILING


def test_a_budget_the_pool_does_not_hold_widens():
    charter = make_charter(
        ceiling_budgets={},
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS,
                                           budgets={"money": 100})})
    record = pp.sign_charter(charter, OP_KEY)
    receipt = run_admit(record, make_join(record))
    assert receipt["link"] == pp.LINK_GRANT_CEILING
    assert ["money", None, 100] in receipt["widened_budgets"]


# ---------------------------------------------------------------------------
# what a tier may be SENT
# ---------------------------------------------------------------------------


def test_the_entry_tier_is_sent_pure_work_only():
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    member = roster.members["alpha"]
    ok, _ = pp.work_admissible(member, EffectClass.PURE)
    assert ok
    for harder in (EffectClass.IDEMPOTENT_EXTERNAL, EffectClass.WITNESSED):
        ok, reason = pp.work_admissible(member, harder)
        assert not ok and "higher tier" in reason


def test_two_effect_classes_are_unreachable_from_every_rung():
    """Not an omission: `lawful_retry` already says an irreversible commit
    belongs at a trusted commit authority and a secret-bearing effect needs an
    attested host. Climbing the pool ladder never reaches either."""
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    base = roster.members["alpha"]
    for tier in pp.TIER_ORDER:
        member = pp._issue_membership(
            "alpha", tier, base.grant(), GOOD_ARTIFACT, "t")
        for effect in pp.UNREACHABLE_EFFECTS:
            ok, reason = pp.work_admissible(member, effect)
            assert not ok
            assert "no pool tier admits" in reason


def test_an_effect_class_nothing_has_seen_is_refused_not_defaulted():
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    ok, reason = pp.work_admissible(roster.members["alpha"], "brand-new")
    assert not ok and "unknown effect class" in reason


def test_every_effect_class_is_either_ranked_or_named_unreachable():
    """The fail-closed direction, held against the enum itself: adding a class
    to `lawful_retry` and not here leaves it inadmissible everywhere, and this
    test says so out loud rather than letting it read as an oversight."""
    for effect in EffectClass:
        assert effect in pp._EFFECT_RANK or effect in pp.UNREACHABLE_EFFECTS


# ---------------------------------------------------------------------------
# withdrawal: three words, not one
# ---------------------------------------------------------------------------


def admitted_pool():
    # A tier this pool grants on no evidence, so the fixture can run a REAL
    # promotion and the ledger below carries a real PROMOTE event. What a
    # promotion requires of receipts is tested in `test_pool_receipts_524.py`,
    # which has the asymmetric identities that a receipt needs.
    record = pp.sign_charter(make_charter(tiers={
        pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS, budgets={"retries": 1}),
        "replayable": pp.TierGrant(caps=('fs.read(path="/data")',),
                                   budgets={"retries": 2},
                                   evidence_required=0),
    }), OP_KEY)
    roster = fresh_roster(record)
    run_admit(record, make_join(record), roster=roster)
    pp.promote(record, "alpha", "replayable", charter_key=OP_KEY, roster=roster,
               receipts=[])
    # The history it goes on to accumulate. Opaque here: these stand for
    # receipts the pool verified, and nothing reads a stored digest back.
    promoted = roster.members["alpha"]
    roster.members["alpha"] = pp._issue_membership(
        "alpha", promoted.tier, promoted.grant(), GOOD_ARTIFACT,
        promoted.admitted_at,
        evidence_digests=("d0" * 32, "d1" * 32, "d2" * 32), receipts=3,
        effects_witnessed=2)
    roster.outstanding["alpha"] = ["task-7", "task-9"]
    return record, roster


def test_withdrawal_reports_three_disjoint_sets():
    record, roster = admitted_pool()
    receipt = pp.withdraw(record, "alpha", "attestation drift",
                          charter_key=OP_KEY, roster=roster,
                          revoking_key_id=key_id(OP_KEY))
    assert receipt["verdict"] == pp.WITHDRAW
    # restored: an inverse exists and this is it
    assert receipt["revoked"] == ['fs.read(path="/data")']
    assert receipt["revoked_budgets"] == {"retries": 2}
    # retained: no inverse. Withdrawing the peer does not un-observe its work.
    assert receipt["retained"]["receipts"] == 3
    assert receipt["retained"]["effects_witnessed"] == 2
    assert receipt["retained"]["tier_reached"] == "replayable"
    # orphaned: neither, and named for `lawful_retry` rather than resolved here
    assert receipt["orphaned"] == ["task-7", "task-9"]
    assert set(receipt["revoked"]).isdisjoint(receipt["orphaned"])


def test_a_withdrawn_peer_holds_exactly_what_a_peer_that_never_joined_holds():
    """The half of the withdrawal that IS an inverse, checked as an inverse."""
    record, roster = admitted_pool()
    pp.withdraw(record, "alpha", "x", charter_key=OP_KEY, roster=roster,
                revoking_key_id=key_id(OP_KEY))
    assert "alpha" not in roster.members
    assert roster.outstanding.get("alpha") is None


def test_the_admission_event_survives_the_withdrawal():
    """A withdrawn peer and a peer that never joined must not render the same:
    the first one ran work whose effects are still in the world. The ledger is
    append-only, so the admission is still there after the withdrawal."""
    record, roster = admitted_pool()
    before = len(roster.events)
    pp.withdraw(record, "alpha", "x", charter_key=OP_KEY, roster=roster,
                revoking_key_id=key_id(OP_KEY))
    assert len(roster.events) == before + 1
    verdicts = [e["verdict"] for e in roster.events]
    assert verdicts == [pp.ADMIT, pp.PROMOTE, pp.WITHDRAW]


def test_accumulated_evidence_does_not_survive_its_holder():
    """Item 546's point, on the one piece of state that would be tempting to
    restore. The evidence stays in the ledger as a record of what happened; it
    is not a credential, so a re-admitted identity starts again at the bottom
    and in this pool does not get back in at all until its key is replaced."""
    record, roster = admitted_pool()
    receipt = pp.withdraw(record, "alpha", "x", charter_key=OP_KEY,
                          roster=roster, revoking_key_id=key_id(OP_KEY))
    assert receipt["retained"]["evidence"] == 3
    again = run_admit(record, make_join(record, nonce="n-2"), roster=roster)
    assert again["link"] == pp.LINK_REVOKED_PEER
    roster.revoked.discard("alpha")
    again = run_admit(record, make_join(record, nonce="n-3"), roster=roster)
    assert again["verdict"] == pp.ADMIT
    assert again["tier"] == pp.ENTRY_TIER, (
        "a re-admitted identity must not recover the tier its evidence bought")
    assert roster.members["alpha"].evidence == 0


# ---------------------------------------------------------------------------
# the signing discipline (item 517)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("member", ["key_id", "pool_id", "ceiling",
                                    "trust_floor", "admit_key_ids"])
def test_every_charter_member_is_covered_including_key_id(member):
    """The anti-pattern item 517 was written after: a signed receipt in this
    tree carried a `key_id` that was appended to the body AFTER the MAC, so the
    member every reader used to choose a verification key was the one member
    nothing covered. The covered set here is derived from the record, so there
    is no field list to fall out of date."""
    record = dict(pp.sign_charter(make_charter(), OP_KEY))
    record[member] = "tampered" if isinstance(record[member], str) else ["x"]
    ok, reason = pp.verify_charter(record, OP_KEY)
    assert not ok and "signature mismatch" in reason


def test_a_member_added_to_a_signed_charter_breaks_it():
    record = dict(pp.sign_charter(make_charter(), OP_KEY))
    record["admit_key_ids_v2"] = ["anybody"]
    ok, _ = pp.verify_charter(record, OP_KEY)
    assert not ok


def test_the_covered_set_is_derived_from_the_record_not_a_field_list():
    """Structural, not behavioural: `_mac` must build its covered set by
    filtering the body, so a member added to a record is covered the day it is
    added. A hand-written tuple of names here would be the defect."""
    source = inspect.getsource(pp._mac)
    tree = ast.parse(source.lstrip())
    comprehensions = [n for n in ast.walk(tree) if isinstance(n, ast.DictComp)]
    assert comprehensions, "`_mac` must derive its covered set from the body"
    assert "SIGNATURE_FIELD" in source


@pytest.mark.parametrize("domain_a,domain_b", [
    (pp.CHARTER_DOMAIN, pp.JOIN_DOMAIN),
    (pp.CHARTER_DOMAIN, pp.RECEIPT_DOMAIN),
    (pp.JOIN_DOMAIN, pp.RECEIPT_DOMAIN),
])
def test_the_three_protocols_have_three_domains(domain_a, domain_b):
    assert domain_a != domain_b
    body = {"kind": "x"}
    assert pp._mac(domain_a, body, OP_KEY) != pp._mac(domain_b, body, OP_KEY)


def test_a_peer_offer_does_not_verify_as_a_join_under_the_same_key():
    """Domain separation, exercised rather than asserted: three protocols share
    one construction, so without it an offer could be replayed as a join."""
    offer = make_offer("alpha", ALPHA_KEY)
    ok, _ = pp.verify_join(offer, ALPHA_KEY)
    assert not ok


def test_a_charter_body_macd_under_another_domain_does_not_verify():
    """The same key, the same canonical bytes, a different protocol's domain
    prefix: `attest` signs compositions and this signs charters, and without
    separation one signature would read as the other."""
    from revl import attest

    body = make_charter().body()
    body["key_id"] = key_id(OP_KEY)
    body[pp.SIGNATURE_FIELD] = pp._mac(attest.SIGN_DOMAIN, body, OP_KEY)
    ok, reason = pp.verify_charter(body, OP_KEY)
    assert not ok and "signature mismatch" in reason


# ---------------------------------------------------------------------------
# the gate never raises
# ---------------------------------------------------------------------------


HOSTILE = [
    None, 42, "a join", [], {}, {"peer_id": None}, {"peer_id": "alpha"},
    {"peer_id": "alpha", "signature": 7},
    {"peer_id": "alpha", "signature": "0" * 64, "offer": "not an object"},
    {"peer_id": "alpha", "signature": "0" * 64, "issued_at": "not a date"},
]


@pytest.mark.parametrize("hostile", HOSTILE, ids=range(len(HOSTILE)))
def test_a_hostile_join_is_refused_not_raised(hostile):
    """The wire is hostile. A record a peer controls must not be able to break
    the gate's contract, which is why `_refusal` carries no internal assertion
    and every parse is guarded."""
    record = pp.sign_charter(make_charter(), OP_KEY)
    receipt = run_admit(record, hostile)
    assert receipt["verdict"] == pp.REFUSE
    assert receipt["link"] in pp.REFUSAL_LINKS


def test_a_hostile_charter_is_refused_not_raised():
    for hostile in HOSTILE:
        ok, reason = pp.verify_charter(hostile, OP_KEY)
        assert not ok and isinstance(reason, str)


# ---------------------------------------------------------------------------
# the structural proofs (item 518's shape)
# ---------------------------------------------------------------------------


def module_tree() -> ast.Module:
    return ast.parse(Path(pp.__file__).read_text(encoding="utf-8"))


def functions_calling(tree: ast.Module, name: str) -> set:
    """Every top-level function whose body contains a call to ``name``."""
    out = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                    and call.func.id == name:
                out.add(node.name)
    return out


def test_a_membership_is_constructed_in_exactly_one_function():
    """If a `Membership` could be built anywhere, the ceiling diff would be a
    stage some path could skip rather than a precondition."""
    assert functions_calling(module_tree(), "Membership") == {"_issue_membership"}


def test_the_tier_grant_is_computed_in_exactly_one_function():
    assert functions_calling(module_tree(), "_tier_grant") == {
        "_ceiling_precondition"}


def test_every_function_that_issues_a_membership_runs_the_diff_first():
    """Item 518's shape: the authority diff is a PRECONDITION of holding a
    grant, not a stage the grant passes through. `_ceiling_precondition` is the
    only producer of a grant and `_issue_membership` only consumes one, so there
    is no ordering in which a membership exists and the diff did not run."""
    tree = module_tree()
    issuers = functions_calling(tree, "_issue_membership")
    checkers = functions_calling(tree, "_ceiling_precondition")
    assert issuers, "nothing issues a membership; the gate would be dead code"
    assert issuers <= checkers, (
        f"{sorted(issuers - checkers)} issue a membership without running the "
        f"ceiling diff")


def test_issue_membership_computes_no_authority_of_its_own():
    """The consumer side of the same statement: the single construction site
    must take the grant it is handed. A call to `_tier_grant`, `parse_cap` or
    `covers_set` in here would mean authority is decided after the diff."""
    source = inspect.getsource(pp._issue_membership)
    body = source.split('"""', 2)[-1]
    for forbidden in ("_tier_grant", "parse_cap", "covers_set",
                      "grant_widenings", "ceiling"):
        assert forbidden not in body, (
            f"`_issue_membership` mentions {forbidden!r}; it must only assemble "
            f"the grant the precondition produced")


def refusal_links_in_source(tree: ast.Module) -> set:
    """Every link name a `_refusal(...)` call in this module passes."""
    links = set()
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_refusal"):
            continue
        first = call.args[0] if call.args else None
        assert isinstance(first, ast.Name), (
            "a refusal must name a LINK_ constant, not build a link inline")
        links.add(first.id)
    return links


def test_every_refusal_names_a_declared_link():
    declared = {name for name in dir(pp) if name.startswith("LINK_")}
    used = refusal_links_in_source(module_tree())
    assert used <= declared, f"undeclared links: {sorted(used - declared)}"
    assert {getattr(pp, name) for name in used} <= set(pp.REFUSAL_LINKS)


def test_no_declared_link_is_unreachable():
    """A link that is declared and never reached reads as a gate that exists.
    Every name in `REFUSAL_LINKS` must be passed by some `_refusal` call."""
    used = {getattr(pp, name) for name in refusal_links_in_source(module_tree())}
    assert set(pp.REFUSAL_LINKS) == used, (
        f"declared but never refused on: {sorted(set(pp.REFUSAL_LINKS) - used)}")


def test_every_declared_link_is_reached_by_a_test_or_the_corpus():
    """Reachable in the source is weaker than reached in a run. The corpus plus
    the named tests above must together produce every link.

    The identity links (issue #1278) are produced by
    ``tests/test_peer_pool_identity.py``'s own corpus, which this unions in by
    IMPORTING the set that corpus builds rather than re-listing the names here.
    A list would go stale silently; the import fails loudly if that file stops
    producing them, and a link produced by neither file still fails."""
    from test_peer_pool_identity import IDENTITY_CORPUS_LINKS

    reached = {link for _, link, _, _ in CORPUS}
    reached |= {pp.LINK_CHARTER_SIGNATURE, pp.LINK_ADMITTING_AUTHORITY,
                pp.LINK_PROMOTION_EVIDENCE, pp.LINK_NOT_A_MEMBER,
                pp.LINK_UNKNOWN_TIER}
    reached |= IDENTITY_CORPUS_LINKS
    assert set(pp.REFUSAL_LINKS) == reached, (
        f"never exercised: {sorted(set(pp.REFUSAL_LINKS) - reached)}")


def test_the_pool_registers_no_new_guarantee_code():
    """The `revl.deploy` discipline: a pool refusal is a decision about a
    deployment, not a verdict about a program, so it names a lowercase link and
    does not enter `revl.diagnostics.GUARANTEES` (whose entries each require an
    `examples/rejections/` reproducer there is no program to write)."""
    from revl import diagnostics

    for link in pp.REFUSAL_LINKS:
        assert link == link.lower()
        assert link not in diagnostics.GUARANTEES
    code = [line for line in Path(pp.__file__).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]
    assert not [line for line in code if "GUARANTEES" in line], (
        "a pool refusal must not reach the checker's guarantee register")


# ---------------------------------------------------------------------------
# charter construction refuses rather than defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overrides,fragment", [
    ({"pool_id": ""}, "pool_id"),
    ({"tiers": {}}, "entry tier"),
    ({"tiers": {"nonsense": pp.TierGrant()}}, "entry tier"),
    ({"ceiling": ("fs.read(bogus=1)",)}, ""),
    ({"trust_floor": "whatever"}, ""),
    ({"join_window_s": 0}, "join_window_s"),
    ({"ceiling_budgets": {"money": -1}}, "non-negative"),
])
def test_a_malformed_charter_raises_at_construction(overrides, fragment):
    with pytest.raises(Exception) as caught:
        make_charter(**overrides)
    assert fragment in str(caught.value)


def test_a_charter_round_trips_through_its_record():
    charter = make_charter()
    record = pp.sign_charter(charter, OP_KEY)
    rebuilt = pp.charter_from_record(record)
    assert rebuilt.pool_id == charter.pool_id
    assert sorted(rebuilt.ceiling) == sorted(charter.ceiling)
    assert sorted(rebuilt.tiers) == sorted(charter.tiers)
    assert pp.canonical_digest(pp.sign_charter(rebuilt, OP_KEY)) == \
        pp.canonical_digest(record)


def test_signing_is_deterministic():
    a = pp.sign_charter(make_charter(), OP_KEY)
    b = pp.sign_charter(make_charter(), OP_KEY)
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------------------
# the roster is the product surface
# ---------------------------------------------------------------------------


def test_the_roster_round_trips_through_json():
    record, roster = admitted_pool()
    pp.withdraw(record, "alpha", "x", charter_key=OP_KEY, roster=roster,
                revoking_key_id=key_id(OP_KEY))
    rebuilt = pp.Roster.from_dict(json.loads(json.dumps(roster.as_dict())))
    assert rebuilt.as_dict() == roster.as_dict()
    assert rebuilt.revoked == roster.revoked
    assert rebuilt.spent_nonces == roster.spent_nonces


def test_status_names_the_authority_view_and_the_effect_ceiling():
    record, roster = admitted_pool()
    rendered = pp.render_status(record, roster)
    assert key_id(OP_KEY) in rendered
    assert key_id(ATTEST_KEY) in rendered
    assert "effects<=idempotent-external" in rendered
    assert "trust >= verified" in rendered


def test_a_refused_join_does_not_spend_the_peers_nonce():
    """An operator who fixes the cause of a refusal can retry the same request;
    only an admission spends the nonce."""
    record = pp.sign_charter(make_charter(), OP_KEY)
    roster = fresh_roster(record)
    bad = make_join(record, artifact=OTHER_ARTIFACT, nonce="n-once")
    assert run_admit(record, bad, roster=roster)["link"] == pp.LINK_ARTIFACT_DIGEST
    assert roster.spent_nonces == set()
    good = make_join(record, nonce="n-once")
    assert run_admit(record, good, roster=roster)["verdict"] == pp.ADMIT
    assert ("alpha", "n-once") in roster.spent_nonces


def test_the_evidence_count_is_a_precondition_not_a_stage():
    """Item 518's shape again, applied to the second thing a promotion needs.

    `_ceiling_precondition` is the only producer of a grant and
    `_evidence_precondition` is the only producer of a count, so a count that
    did not come from the recount cannot reach the threshold comparison. A
    behavioural test could only show the orderings it happened to try."""
    tree = module_tree()
    counters = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and \
                    isinstance(call.func, ast.Attribute) and \
                    call.func.attr == "count_evidence":
                counters.add(node.name)
    assert counters == {"_evidence_precondition"}, (
        f"the evidence recount happens in {sorted(counters)}; it must happen "
        f"in exactly one function")
    assert functions_calling(tree, "_evidence_precondition") == {"promote"}


def test_promote_never_reads_a_members_stored_evidence():
    """The fail-open was `member.evidence` being an input. It is now an
    OUTPUT: derived from the digests the recount produced. A `promote` that
    read it back would have reopened the hole, so it is asserted structurally
    rather than left to review."""
    function = ast.parse(inspect.getsource(pp.promote).lstrip()).body[0]
    reads = [node for node in ast.walk(function)
             if isinstance(node, ast.Attribute)
             and isinstance(node.value, ast.Name)
             and node.value.id == "member"
             and node.attr in ("evidence", "evidence_digests")]
    assert reads == [], (
        "`promote` reads the member's stored evidence; the count must come "
        "from the receipts it verified")

    taken = {arg.arg for arg in function.args.args + function.args.kwonlyargs}
    assert "evidence_key_ids" not in taken, (
        "`promote` still takes a caller-supplied list of evidence key ids")
    assert "receipts" in taken, "`promote` must be handed the receipts"
