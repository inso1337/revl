"""A peer's identity as a key pair rather than a shared secret (issue #1278).

Five things are held here.

1. NON-VACUITY, and it is the whole point of the change. The baseline is not
   "no gate": it is item 524's gate, the shared-key one, which is a real gate
   that refuses fourteen shaped attacks. What it cannot refuse is the operator
   itself. :data:`FORGERIES` is a set of joins the OPERATOR manufactures for
   peers that never asked to join, using only material the pool hands it. Every
   one is ADMITTED by the shared-key pool and REFUSED by the asymmetric one, and
   a control that each peer signed itself is admitted by both.

2. NO DOWNGRADE, IN EITHER HALF. ``sign_alg`` selects one verifier and its
   failure is the answer; a peer with a pinned public key cannot present a
   shared-key join even in a mixed pool; and a join and the offer inside it must
   be backed the same way, so the advertised ceiling the grant is diffed against
   cannot be the weak half of a strong-looking pair.

3. WHAT THE SIGNATURE BINDS, member by member, with a test per member. Removing
   one, editing one, or moving a member between the join and the offer is a
   signature failure, and reordering members is not, because canonical JSON
   sorts them.

4. A REVOKED KEY STILL VERIFIES AND AUTHORISES NOTHING. The two facts are
   separate members of an :class:`Attribution`, never a boolean, which is item
   546's restore-versus-compensate distinction applied to identity: authority
   has an inverse and revocation is it; a signature having been made does not.

5. THE MIXTURE IS VISIBLE. A pool halfway through the migration reports which
   members are on which backing and names the weak ones, so a deployment is not
   silently weakest-link.

The cross-implementation half of this lives in
``tests/test_peer_identity_differential.py``, which requires ``cryptography`` as
a hard import for the reason its docstring gives.
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_offer, peer_pool as pp  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.peer_offer import Attestation, PeerOffer, ResourceOffer  # noqa: E402

OP_KEY = b"pool-operator-key-1278"
ATTEST_KEY = b"pool-attesting-key-1278"

GOOD_ARTIFACT = "c" * 64
POOL_CEILING = ('fs.read(path="/data")',)
ENTRY_CAPS = ('fs.read(path="/data/in")',)

#: The peers in the experiment. Eight is enough to make the count a count.
PEERS = tuple(f"peer-{n}" for n in range(8))

#: Deterministic identities. `identity_from_seed` is the FIXTURE constructor and
#: its docstring says why: the scalar is a hash of the seed, so a seed in a
#: repository is a published private key. Nothing here admits a real peer.
IDENTITIES = {peer: pi.identity_from_seed(peer, f"seed/{peer}".encode())
              for peer in PEERS}

#: The legacy shared secrets, one per peer. In the shared-key deployment the
#: operator holds every one of these, which is exactly the problem.
SHARED = {peer: f"shared-secret/{peer}".encode() for peer in PEERS}

#: A key pair the OPERATOR generated. It is an attacker's key in this story: it
#: is perfectly well formed and nobody pinned it.
OPERATOR_IDENTITY = pi.identity_from_seed("operator", b"seed/operator")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def make_charter(mode, **overrides) -> pp.PoolCharter:
    spec = dict(
        pool_id="lab-1278",
        ceiling=POOL_CEILING,
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS)},
        admit_key_ids=(key_id(OP_KEY),),
        revoke_key_ids=(key_id(OP_KEY), OPERATOR_IDENTITY.key_id),
        attest_key_ids=(key_id(ATTEST_KEY),),
        artifact_digests=(GOOD_ARTIFACT,),
        identity_mode=mode)
    spec.update(overrides)
    return pp.sign_charter(pp.PoolCharter(**spec), OP_KEY)


def make_offer(peer_id) -> PeerOffer:
    return PeerOffer(
        peer_id=peer_id,
        attestation=Attestation(trust="verified", region="eu", hardware="x86",
                                resource_offer=ResourceOffer(4, 1 << 30, 60.0)),
        grant_ceiling=POOL_CEILING)


def join_body(charter_record, peer_id, signed_offer, *, nonce="n-1",
              artifact=GOOD_ARTIFACT) -> pp.JoinRequest:
    return pp.JoinRequest(
        pool_id=charter_record["pool_id"],
        charter_digest=pp.canonical_digest(charter_record),
        peer_id=peer_id,
        offer=signed_offer,
        artifact_digest=artifact,
        nonce=nonce,
        issued_at=pp._iso(pp._utc_now()))


def shared_key_join(charter_record, peer_id, key, **kwargs) -> dict:
    """A join signed with a shared secret. Anyone holding that secret can
    produce this, which is the property under test."""
    offer = peer_offer.sign_offer(make_offer(peer_id), key)
    return pp.sign_join(join_body(charter_record, peer_id, offer, **kwargs),
                        key)


def identity_join(charter_record, peer_id, identity, **kwargs) -> dict:
    """A join signed with a key pair. Only the holder of the private scalar can
    produce this."""
    offer = peer_offer.sign_offer_identity(make_offer(peer_id), identity)
    return pp.sign_join_identity(
        join_body(charter_record, peer_id, offer, **kwargs), identity)


def pinned_directory(*peers) -> pi.IdentityDirectory:
    directory = pi.IdentityDirectory()
    for peer in peers or PEERS:
        directory.register(IDENTITIES[peer].public())
    return directory


def fresh_roster(charter_record) -> pp.Roster:
    return pp.Roster(charter_record["pool_id"],
                     pp.canonical_digest(charter_record))


def admit(charter_record, join, *, roster=None, directory=None,
          peer_keys=None, now=None) -> dict:
    return pp.admit(
        charter_record, join, charter_key=OP_KEY,
        peer_keys=peer_keys if peer_keys is not None else dict(SHARED),
        directory=directory, admitting_key_id=key_id(OP_KEY),
        roster=roster if roster is not None else fresh_roster(charter_record),
        now=now)


# ---------------------------------------------------------------------------
# 1. non-vacuity: what the operator can forge, and what it can no longer forge
# ---------------------------------------------------------------------------


def forgeries():
    """Joins the OPERATOR manufactures for peers that never asked to join.

    Each entry is ``(name, build_shared, build_asymmetric)``. ``build_shared``
    is what the operator can do in item 524's deployment; ``build_asymmetric``
    is the closest thing it can still do once identity is a key pair, using
    everything the pool hands it: the charter, its own admit key, every peer's
    PUBLIC key, and even the legacy shared secret it used to hold."""

    def with_the_peers_shared_secret(record, peer):
        return shared_key_join(record, peer, SHARED[peer])

    def replay_the_secret_it_still_holds(record, peer):
        # The operator did not forget the shared secret when the pool moved.
        return shared_key_join(record, peer, SHARED[peer])

    def sign_with_its_own_key_pair(record, peer):
        # A well-formed asymmetric join for someone else's peer_id, under a key
        # pair the operator generated itself.
        return identity_join(record, peer, OPERATOR_IDENTITY)

    def claim_the_peers_public_key(record, peer):
        # The nastiest of the three: take the peer's real public key and key_id,
        # which are not secret, and sign the body with the operator's scalar.
        join = identity_join(record, peer, OPERATOR_IDENTITY)
        join[pi.KEY_ID_FIELD] = IDENTITIES[peer].key_id
        join[pi.PUBLIC_KEY_FIELD] = IDENTITIES[peer].public_key.hex()
        return join

    return [
        ("operator-forges-with-the-shared-secret",
         with_the_peers_shared_secret, replay_the_secret_it_still_holds),
        ("operator-signs-with-its-own-key-pair",
         with_the_peers_shared_secret, sign_with_its_own_key_pair),
        ("operator-claims-the-peers-public-key",
         with_the_peers_shared_secret, claim_the_peers_public_key),
    ]


FORGERIES = forgeries()


@pytest.mark.parametrize("name,build_shared,build_asym", FORGERIES,
                         ids=[f[0] for f in FORGERIES])
def test_a_forgery_the_shared_key_pool_admits_is_refused_once_identity_is_a_key(
        name, build_shared, build_asym):
    """One peer, two deployments, the same attacker."""
    legacy = make_charter(pp.MODE_SHARED_KEY)
    forged = build_shared(legacy, PEERS[0])
    assert admit(legacy, forged)["verdict"] == pp.ADMIT, (
        f"{name} must be ADMITTED by the shared-key pool, or its refusal "
        f"below proves nothing new")

    strict = make_charter(pp.MODE_ASYMMETRIC)
    receipt = admit(strict, build_asym(strict, PEERS[0]),
                    directory=pinned_directory())
    assert receipt["verdict"] == pp.REFUSE, f"{name} was admitted"
    assert receipt["link"] in pp.REFUSAL_LINKS


def test_non_vacuity_counts():
    """The numbers, asserted rather than described.

    Eight peers the operator never spoke to, three forgery shapes each. Every
    one is admitted by item 524's gate and refused by this one. The control is
    the same eight peers joining honestly, and it is admitted by both."""
    legacy = make_charter(pp.MODE_SHARED_KEY)
    strict = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()

    admitted_before = 0
    refused_after = 0
    for _name, build_shared, build_asym in FORGERIES:
        for peer in PEERS:
            if admit(legacy, build_shared(legacy, peer))["verdict"] == pp.ADMIT:
                admitted_before += 1
            if admit(strict, build_asym(strict, peer),
                     directory=directory)["verdict"] == pp.REFUSE:
                refused_after += 1

    expected = len(FORGERIES) * len(PEERS)
    assert admitted_before == expected == refused_after == 24

    # The control: the peers themselves, honestly. Admitted in both worlds,
    # because a gate that refuses everything proves nothing either.
    controls_legacy = sum(
        admit(legacy, shared_key_join(legacy, peer, SHARED[peer]),
              roster=fresh_roster(legacy))["verdict"] == pp.ADMIT
        for peer in PEERS)
    controls_strict = sum(
        admit(strict, identity_join(strict, peer, IDENTITIES[peer]),
              roster=fresh_roster(strict), directory=directory,
              peer_keys={})["verdict"] == pp.ADMIT
        for peer in PEERS)
    assert controls_legacy == controls_strict == len(PEERS) == 8


def test_the_operator_holding_every_public_key_still_forges_nothing():
    """The public half is public. Holding all of them, plus the admit key, plus
    the charter key, must still not produce one admitted join."""
    strict = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    for peer in PEERS:
        public = IDENTITIES[peer].public_key
        assert directory.active(peer).public_key == public
        forged = identity_join(strict, peer, OPERATOR_IDENTITY)
        forged[pi.PUBLIC_KEY_FIELD] = public.hex()
        forged[pi.KEY_ID_FIELD] = IDENTITIES[peer].key_id
        assert admit(strict, forged, directory=directory)["verdict"] == pp.REFUSE


# ---------------------------------------------------------------------------
# 2. the new links, one shaped join each
# ---------------------------------------------------------------------------


def identity_corpus():
    """``(name, expected_link, build)``. ``build(directory)`` returns
    ``(charter_record, join, kwargs)``."""

    def asymmetric_join_at_a_shared_key_pool(directory):
        record = make_charter(pp.MODE_SHARED_KEY)
        return record, identity_join(record, PEERS[0], IDENTITIES[PEERS[0]]), {}

    def shared_key_join_at_an_asymmetric_pool(directory):
        record = make_charter(pp.MODE_ASYMMETRIC)
        return record, shared_key_join(record, PEERS[0], SHARED[PEERS[0]]), {}

    def an_algorithm_nobody_named(directory):
        record = make_charter(pp.MODE_MIXED)
        join = identity_join(record, PEERS[0], IDENTITIES[PEERS[0]])
        join["sign_alg"] = "rot13"
        return record, join, {}

    def downgrade_in_a_mixed_pool(directory):
        # The pool admits both backings. This peer has a pinned public key, so
        # it does not get to go back to the shared secret.
        record = make_charter(pp.MODE_MIXED)
        return record, shared_key_join(record, PEERS[0], SHARED[PEERS[0]]), {}

    def a_strong_join_carrying_a_weak_offer(directory):
        record = make_charter(pp.MODE_MIXED)
        weak_offer = peer_offer.sign_offer(make_offer(PEERS[0]),
                                           SHARED[PEERS[0]])
        join = pp.sign_join_identity(
            join_body(record, PEERS[0], weak_offer), IDENTITIES[PEERS[0]])
        return record, join, {}

    def a_key_nobody_pinned(directory):
        record = make_charter(pp.MODE_ASYMMETRIC)
        # A peer the directory knows, signing with a second key pair it never
        # registered. The peer is known; the KEY is not.
        stranger = pi.identity_from_seed(PEERS[0], b"seed/unregistered")
        return record, identity_join(record, PEERS[0], stranger), {}

    def a_revoked_key(directory):
        record = make_charter(pp.MODE_ASYMMETRIC)
        join = identity_join(record, PEERS[1], IDENTITIES[PEERS[1]])
        directory.revoke(PEERS[1], IDENTITIES[PEERS[1]].key_id,
                         reason="host seized")
        return record, join, {}

    def a_superseded_key(directory):
        record = make_charter(pp.MODE_ASYMMETRIC)
        join = identity_join(record, PEERS[2], IDENTITIES[PEERS[2]])
        directory.rotate(pi.identity_from_seed(PEERS[2],
                                               b"seed/peer-2/next").public())
        return record, join, {}

    def a_substituted_public_key(directory):
        record = make_charter(pp.MODE_ASYMMETRIC)
        join = identity_join(record, PEERS[3], IDENTITIES[PEERS[3]])
        join[pi.PUBLIC_KEY_FIELD] = OPERATOR_IDENTITY.public_key.hex()
        return record, join, {}

    def a_truncated_body(directory):
        record = make_charter(pp.MODE_ASYMMETRIC)
        join = identity_join(record, PEERS[4], IDENTITIES[PEERS[4]])
        del join["nonce"]
        return record, join, {}

    return [
        ("asymmetric-join-at-a-shared-key-pool", pp.LINK_IDENTITY_MODE,
         asymmetric_join_at_a_shared_key_pool),
        ("shared-key-join-at-an-asymmetric-pool", pp.LINK_IDENTITY_MODE,
         shared_key_join_at_an_asymmetric_pool),
        ("an-algorithm-nobody-named", pp.LINK_IDENTITY_MODE,
         an_algorithm_nobody_named),
        ("downgrade-in-a-mixed-pool", pp.LINK_IDENTITY_DOWNGRADE,
         downgrade_in_a_mixed_pool),
        ("strong-join-weak-offer", pp.LINK_IDENTITY_DOWNGRADE,
         a_strong_join_carrying_a_weak_offer),
        ("a-key-nobody-pinned", pp.LINK_UNKNOWN_KEY, a_key_nobody_pinned),
        ("a-revoked-key", pp.LINK_REVOKED_KEY, a_revoked_key),
        ("a-superseded-key", pp.LINK_REVOKED_KEY, a_superseded_key),
        ("a-substituted-public-key", pp.LINK_JOIN_SIGNATURE,
         a_substituted_public_key),
        ("a-truncated-body", pp.LINK_JOIN_SIGNATURE, a_truncated_body),
    ]


IDENTITY_CORPUS = identity_corpus()

#: The links this file's corpus actually produces in a run.
#: ``tests/test_peer_pool_admission.py`` unions this into its own reachability
#: assertion, so a link declared by `peer_pool` and exercised only here still
#: counts as exercised, and a link exercised nowhere still fails.
IDENTITY_CORPUS_LINKS = {link for _, link, _ in IDENTITY_CORPUS}


@pytest.mark.parametrize("name,link,build", IDENTITY_CORPUS,
                         ids=[c[0] for c in IDENTITY_CORPUS])
def test_each_identity_refusal_names_its_link(name, link, build):
    directory = pinned_directory()
    record, join, kwargs = build(directory)
    receipt = admit(record, join, directory=directory, **kwargs)
    assert receipt["verdict"] == pp.REFUSE, f"{name} was admitted"
    assert receipt["link"] == link, (
        f"{name} refused on {receipt['link']!r}, expected {link!r}: "
        f"{receipt['reason']}")


def test_the_corpus_reaches_every_link_this_change_added():
    added = {pp.LINK_IDENTITY_MODE, pp.LINK_IDENTITY_DOWNGRADE,
             pp.LINK_UNKNOWN_KEY, pp.LINK_REVOKED_KEY}
    assert added <= IDENTITY_CORPUS_LINKS


def test_an_unverifiable_signature_refuses_and_does_not_fall_back():
    """The failure direction, stated and held: the gate must never answer a
    failed asymmetric check by trying the shared key it also holds."""
    record = make_charter(pp.MODE_MIXED)
    directory = pinned_directory()
    peer = PEERS[0]
    join = identity_join(record, peer, IDENTITIES[peer])
    join[pp.SIGNATURE_FIELD] = "00" * 64
    receipt = admit(record, join, directory=directory,
                    peer_keys=dict(SHARED))
    assert receipt["verdict"] == pp.REFUSE
    assert receipt["link"] == pp.LINK_JOIN_SIGNATURE


def test_a_pool_with_no_directory_admits_no_asymmetric_peer():
    """Fail-closed on the absent case. A pool handed no directory has an EMPTY
    one, so every key is unknown, not every key is fine."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    join = identity_join(record, PEERS[0], IDENTITIES[PEERS[0]])

    # Nothing is held for this peer at all.
    nothing = admit(record, join, directory=None, peer_keys={})
    assert nothing["verdict"] == pp.REFUSE
    assert nothing["link"] == pp.LINK_UNKNOWN_PEER

    # The legacy shared secret is still held and is NOT a substitute for a
    # pinned public key. The refusal names the missing key, and the presence of
    # a shared secret changes nothing about the verdict.
    legacy_only = admit(record, join, directory=None, peer_keys=dict(SHARED))
    assert legacy_only["verdict"] == pp.REFUSE
    assert legacy_only["link"] == pp.LINK_UNKNOWN_KEY


# ---------------------------------------------------------------------------
# 3. what the signature binds, member by member
# ---------------------------------------------------------------------------

JOIN_MEMBERS = ["kind", "version", "pool_id", "charter_digest", "peer_id",
                "offer", "artifact_digest", "nonce", "issued_at", "sign_alg",
                "key_id", "public_key"]


@pytest.mark.parametrize("member", JOIN_MEMBERS)
def test_every_join_member_is_covered(member):
    """Removing any member breaks the signature. The covered set is derived
    from the record, so there is no field list to leave one out of."""
    peer = PEERS[0]
    record = make_charter(pp.MODE_ASYMMETRIC)
    join = identity_join(record, peer, IDENTITIES[peer])
    assert member in join, f"{member} is not in the join body"
    mutilated = {k: v for k, v in join.items() if k != member}
    ok, _ = pp.verify_join_identity(mutilated, IDENTITIES[peer].public_key)
    assert not ok, f"{member} can be removed without breaking the signature"


@pytest.mark.parametrize("member", ["pool_id", "charter_digest", "peer_id",
                                    "artifact_digest", "nonce", "issued_at"])
def test_every_join_member_is_covered_against_editing(member):
    peer = PEERS[0]
    record = make_charter(pp.MODE_ASYMMETRIC)
    join = identity_join(record, peer, IDENTITIES[peer])
    edited = dict(join)
    edited[member] = "z" * len(str(edited[member]))
    ok, _ = pp.verify_join_identity(edited, IDENTITIES[peer].public_key)
    assert not ok, f"{member} can be edited without breaking the signature"


def test_reordering_members_changes_nothing_because_canonical_json_sorts():
    """Reordering is not an attack and must not read as one. A verifier that
    depended on member order would refuse an honest record that passed through
    a JSON library, which is a false reject and the worse failure."""
    peer = PEERS[0]
    record = make_charter(pp.MODE_ASYMMETRIC)
    join = identity_join(record, peer, IDENTITIES[peer])
    reordered = dict(reversed(list(join.items())))
    assert list(reordered) != list(join)
    ok, _ = pp.verify_join_identity(reordered, IDENTITIES[peer].public_key)
    assert ok


def test_the_offer_cannot_be_swapped_for_another_peers():
    """The offer is carried INSIDE the signed body, so the ceiling the grant is
    diffed against cannot be replaced after the peer signed."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    join = identity_join(record, PEERS[0], IDENTITIES[PEERS[0]])
    join["offer"] = peer_offer.sign_offer_identity(make_offer(PEERS[1]),
                                                   IDENTITIES[PEERS[1]])
    ok, _ = pp.verify_join_identity(join, IDENTITIES[PEERS[0]].public_key)
    assert not ok


def test_a_join_does_not_verify_as_an_offer_under_the_same_key():
    """Domain separation survives the move to key pairs."""
    peer = PEERS[0]
    record = make_charter(pp.MODE_ASYMMETRIC)
    join = identity_join(record, peer, IDENTITIES[peer])
    ok, _ = peer_offer.verify_offer_identity(join,
                                             IDENTITIES[peer].public_key)
    assert not ok
    offer = peer_offer.sign_offer_identity(make_offer(peer), IDENTITIES[peer])
    ok, _ = pp.verify_join_identity(offer, IDENTITIES[peer].public_key)
    assert not ok


def test_a_replayed_join_is_still_used_once_and_a_stale_one_still_stale():
    """The two properties PR #1277 designed against survive the move: the
    signature covers the nonce and the timestamp, and the roster still spends
    them."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    peer = PEERS[0]
    join = identity_join(record, peer, IDENTITIES[peer])
    assert admit(record, join, roster=roster,
                 directory=directory)["verdict"] == pp.ADMIT
    roster.members.pop(peer)
    replayed = admit(record, join, roster=roster, directory=directory)
    assert replayed["link"] == pp.LINK_REPLAYED_JOIN

    stale_body = join_body(record, PEERS[1],
                           peer_offer.sign_offer_identity(
                               make_offer(PEERS[1]), IDENTITIES[PEERS[1]]))
    stale = pp.sign_join_identity(stale_body, IDENTITIES[PEERS[1]])
    later = pp._utc_now() + timedelta(hours=2)
    assert admit(record, stale, directory=directory,
                 now=later)["link"] == pp.LINK_STALE_JOIN


def test_the_charter_is_still_pinned_by_digest_not_by_name():
    record = make_charter(pp.MODE_ASYMMETRIC)
    wider = make_charter(pp.MODE_ASYMMETRIC,
                         ceiling=POOL_CEILING + ('net.fetch(host="x")',))
    peer = PEERS[0]
    join = identity_join(record, peer, IDENTITIES[peer])
    assert record["pool_id"] == wider["pool_id"]
    receipt = admit(wider, join, directory=pinned_directory())
    assert receipt["link"] == pp.LINK_POOL_IDENTITY


def test_the_admit_authority_check_still_runs_before_the_join_is_parsed():
    """An operator with no admit authority must not learn anything about the
    join. The refusal text must be the same for a valid join and for garbage."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    peer = PEERS[0]
    valid = pp.admit(record, identity_join(record, peer, IDENTITIES[peer]),
                     charter_key=OP_KEY, peer_keys={},
                     directory=pinned_directory(),
                     admitting_key_id="0" * 16, roster=fresh_roster(record))
    garbage = pp.admit(record, {"nonsense": True}, charter_key=OP_KEY,
                       peer_keys={}, directory=pinned_directory(),
                       admitting_key_id="0" * 16, roster=fresh_roster(record))
    assert valid["link"] == garbage["link"] == pp.LINK_ADMITTING_AUTHORITY
    assert valid["reason"] == garbage["reason"]


# ---------------------------------------------------------------------------
# 4. rotation and revocation: verifiable forever, authorising nothing
# ---------------------------------------------------------------------------


def test_a_revoked_keys_past_signature_still_verifies_and_confers_nothing():
    """The two facts, separately, which is the whole shape of this."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    peer = PEERS[0]
    join = identity_join(record, peer, IDENTITIES[peer])

    before = directory.attribute(pp.JOIN_DOMAIN, join, peer)
    assert before.verified and before.status == pi.KEY_ACTIVE
    assert before.confers_authority

    directory.revoke(peer, IDENTITIES[peer].key_id, reason="host seized")
    after = directory.attribute(pp.JOIN_DOMAIN, join, peer)
    assert after.verified, (
        "revocation must not reach into the past and un-make a signature; the "
        "record was signed and it stays checkable")
    assert after.status == pi.KEY_REVOKED
    assert not after.confers_authority


def test_a_rotated_key_keeps_verifying_and_stops_authorising():
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    peer = PEERS[0]
    old_join = identity_join(record, peer, IDENTITIES[peer])

    replacement = pi.identity_from_seed(peer, b"seed/peer-0/rotated")
    superseded, active = directory.rotate(replacement.public())
    assert superseded.key_id == IDENTITIES[peer].key_id
    assert active.key_id == replacement.key_id

    old = directory.attribute(pp.JOIN_DOMAIN, old_join, peer)
    assert old.verified and old.status == pi.KEY_SUPERSEDED
    assert not old.confers_authority

    fresh_body = join_body(record, peer,
                           peer_offer.sign_offer_identity(make_offer(peer),
                                                          replacement),
                           nonce="n-rotated")
    fresh = pp.sign_join_identity(fresh_body, replacement)
    assert admit(record, fresh, directory=directory)["verdict"] == pp.ADMIT


def test_only_the_active_status_confers_authority():
    """Asserted over the status list rather than left to each reader of it."""
    for status in pi.KEY_STATUSES:
        attribution = pi.Attribution(peer_id="p", key_id="k", verified=True,
                                     status=status, reason="")
        assert attribution.confers_authority == (status == pi.KEY_ACTIVE)
    assert not pi.Attribution(peer_id="p", key_id="k", verified=False,
                              status=pi.KEY_ACTIVE,
                              reason="").confers_authority


def test_a_revoked_peer_that_rejoins_starts_at_the_entry_tier_with_no_evidence():
    """Item 546 again: evidence is a record of what happened, not a credential
    that survives its holder's removal."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    peer = PEERS[0]
    assert admit(record, identity_join(record, peer, IDENTITIES[peer]),
                 roster=roster, directory=directory)["verdict"] == pp.ADMIT
    row = {**roster.members[peer].as_dict(),
           "caps": roster.members[peer].caps,
           "budgets": roster.members[peer].budgets,
           "evidence_digests": tuple(f"{n:064x}" for n in range(7)),
           "receipts": 9, "effects_witnessed": 4}
    # `evidence` is derived from the digests now, so it is not a constructor
    # argument and the rendered row carries it only for a reader.
    row.pop("evidence")
    roster.members[peer] = pp.Membership(**row)

    receipt = pp.withdraw(record, peer, "host seized", charter_key=OP_KEY,
                          roster=roster, directory=directory,
                          revoking_key_id=key_id(OP_KEY))
    assert receipt["verdict"] == pp.WITHDRAW
    assert receipt["retained"]["evidence"] == 7
    assert receipt["retained"]["signatures_verifiable"] is True
    assert receipt["keys_revoked"] == [IDENTITIES[peer].key_id]

    # Its key is revoked, so it cannot rejoin under it at all, and the ledger it
    # signed is still checkable.
    rejoin_body = join_body(record, peer,
                            peer_offer.sign_offer_identity(make_offer(peer),
                                                           IDENTITIES[peer]),
                            nonce="n-again")
    rejoin = pp.sign_join_identity(rejoin_body, IDENTITIES[peer])
    again = admit(record, rejoin, roster=roster, directory=directory)
    assert again["verdict"] == pp.REFUSE
    assert again["link"] in (pp.LINK_REVOKED_PEER, pp.LINK_REVOKED_KEY)


def test_withdrawal_still_reports_three_disjoint_sets():
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    peer = PEERS[0]
    admit(record, identity_join(record, peer, IDENTITIES[peer]),
          roster=roster, directory=directory)
    roster.outstanding[peer] = ["task-1", "task-2"]
    receipt = pp.withdraw(record, peer, "rebalance", charter_key=OP_KEY,
                          roster=roster, directory=directory,
                          revoking_key_id=key_id(OP_KEY))
    assert set(receipt["revoked"]) == set(ENTRY_CAPS)
    assert receipt["orphaned"] == ["task-1", "task-2"]
    assert isinstance(receipt["retained"], dict)
    assert "revoked" not in receipt["retained"]
    assert not isinstance(receipt.get("retained"), bool)


def test_a_withdrawal_is_verifiable_by_a_holder_of_the_revoke_public_key():
    """The third exit surface: withdrawal, not only join and offer."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    peer = PEERS[0]
    admit(record, identity_join(record, peer, IDENTITIES[peer]),
          roster=roster, directory=directory)
    receipt = pp.withdraw(record, peer, "rebalance", charter_key=OP_KEY,
                          roster=roster, directory=directory,
                          revoking_identity=OPERATOR_IDENTITY)
    ok, reason = pp.verify_withdrawal(receipt, OPERATOR_IDENTITY.public_key)
    assert ok, reason
    ok, _ = pp.verify_withdrawal(receipt, IDENTITIES[peer].public_key)
    assert not ok, "it must not verify under some other party's key"
    edited = dict(receipt)
    edited["peer_id"] = PEERS[1]
    assert not pp.verify_withdrawal(edited, OPERATOR_IDENTITY.public_key)[0]


def test_an_unsigned_withdrawal_does_not_verify_as_a_signed_one():
    """"unsigned" and "signed by somebody I cannot name" must not render the
    same."""
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    peer = PEERS[0]
    admit(record, identity_join(record, peer, IDENTITIES[peer]),
          roster=roster, directory=directory)
    receipt = pp.withdraw(record, peer, "rebalance", charter_key=OP_KEY,
                          roster=roster, directory=directory,
                          revoking_key_id=key_id(OP_KEY))
    assert pp.SIGNATURE_FIELD not in receipt
    assert not pp.verify_withdrawal(receipt, OPERATOR_IDENTITY.public_key)[0]


def test_the_key_that_may_admit_is_not_automatically_the_key_that_may_revoke():
    record = make_charter(pp.MODE_ASYMMETRIC,
                          revoke_key_ids=(key_id(b"someone-else"),))
    directory = pinned_directory()
    roster = fresh_roster(record)
    peer = PEERS[0]
    admit(record, identity_join(record, peer, IDENTITIES[peer]),
          roster=roster, directory=directory)
    refusal = pp.withdraw(record, peer, "no", charter_key=OP_KEY,
                          roster=roster, revoking_identity=OPERATOR_IDENTITY)
    assert refusal["link"] == pp.LINK_ADMITTING_AUTHORITY


# ---------------------------------------------------------------------------
# 5. the directory's own rules
# ---------------------------------------------------------------------------


def test_a_key_is_never_replaced_silently():
    directory = pi.IdentityDirectory()
    directory.register(IDENTITIES[PEERS[0]].public())
    with pytest.raises(pi.IdentityError) as caught:
        directory.register(
            pi.identity_from_seed(PEERS[0], b"seed/second").public())
    assert "rotate" in str(caught.value)


def test_a_revoked_key_is_not_re_registered():
    directory = pi.IdentityDirectory()
    directory.register(IDENTITIES[PEERS[0]].public())
    directory.revoke(PEERS[0], IDENTITIES[PEERS[0]].key_id, reason="seized")
    with pytest.raises(pi.IdentityError):
        directory.register(IDENTITIES[PEERS[0]].public())


def test_rotating_back_to_a_key_the_peer_already_used_is_not_a_rotation():
    directory = pi.IdentityDirectory()
    directory.register(IDENTITIES[PEERS[0]].public())
    replacement = pi.identity_from_seed(PEERS[0], b"seed/next")
    directory.rotate(replacement.public())
    with pytest.raises(pi.IdentityError):
        directory.rotate(IDENTITIES[PEERS[0]].public())


def test_an_unknown_key_is_unknown_and_not_a_default():
    directory = pinned_directory(PEERS[0])
    status, reason = directory.authority(PEERS[1], "0" * 16)
    assert status == pi.KEY_UNKNOWN
    assert "not" in reason or "no key" in reason


def test_a_revoked_peer_is_still_an_asymmetric_peer():
    """Otherwise revocation would be the route back to the shared-key path,
    which is the downgrade it exists to prevent."""
    directory = pinned_directory(PEERS[0])
    directory.revoke(PEERS[0], IDENTITIES[PEERS[0]].key_id, reason="seized")
    assert directory.active(PEERS[0]) is None
    assert directory.has_identity(PEERS[0])


def test_a_private_key_is_redacted_from_its_own_repr():
    identity = IDENTITIES[PEERS[0]]
    assert str(identity.private_key) not in repr(identity)
    assert "redacted" in repr(identity)


def test_an_off_curve_public_key_is_refused_where_it_is_pinned():
    with pytest.raises(pi.IdentityError):
        pi.PublicIdentity(peer_id="p", public_key=b"\x01" * 64)
    with pytest.raises(pi.IdentityError):
        pi.PublicIdentity(peer_id="p", public_key=b"\x00" * 64)


def test_generated_identities_differ():
    """A fresh draw is a fresh key. A generator that returned the same scalar
    twice would give every peer the same identity."""
    scalars = {pi.generate_identity("p").private_key for _ in range(16)}
    assert len(scalars) == 16


def test_the_directory_round_trips_through_json():
    directory = pinned_directory()
    directory.revoke(PEERS[0], IDENTITIES[PEERS[0]].key_id, reason="seized")
    directory.rotate(pi.identity_from_seed(PEERS[1], b"seed/rot").public())
    rebuilt = pi.IdentityDirectory.from_dict(
        json.loads(json.dumps(directory.as_dict())))
    assert rebuilt.as_dict() == directory.as_dict()
    assert rebuilt.authority(PEERS[0], IDENTITIES[PEERS[0]].key_id)[0] == \
        pi.KEY_REVOKED


def test_key_files_round_trip_and_the_private_one_is_not_world_readable(tmp_path):
    identity = pi.generate_identity("peer-files")
    private = tmp_path / "id.json"
    public = tmp_path / "id.pub.json"
    pi.write_private_identity(private, identity)
    pi.write_public_identity(public, identity.public())
    assert (private.stat().st_mode & 0o077) == 0
    assert pi.load_private_identity(private).private_key == identity.private_key
    assert pi.load_public_identity(public).key_id == identity.key_id
    assert pi.fingerprint_of(identity.public_key.hex()) == identity.key_id
    assert str(identity.private_key) not in public.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. the mixture is visible
# ---------------------------------------------------------------------------


def test_status_names_the_members_still_on_a_shared_key():
    record = make_charter(pp.MODE_MIXED)
    directory = pinned_directory(PEERS[0], PEERS[1])
    roster = fresh_roster(record)
    admit(record, identity_join(record, PEERS[0], IDENTITIES[PEERS[0]]),
          roster=roster, directory=directory)
    # PEERS[5] has no pinned key, so it is still a legacy shared-key member.
    admit(record, shared_key_join(record, PEERS[5], SHARED[PEERS[5]]),
          roster=roster, directory=directory)

    rendered = pp.render_status(record, roster, directory)
    assert f"mode={pp.MODE_MIXED}" in rendered
    assert f"{pi.IDENTITY_ASYMMETRIC}=1" in rendered
    assert f"{pi.IDENTITY_SHARED_KEY}=1" in rendered
    assert "WEAKEST LINK" in rendered
    assert PEERS[5] in rendered.split("WEAKEST LINK")[1]

    census = pp.identity_census(roster)
    assert census["mixed"] is True
    assert census["weakest"] == pi.IDENTITY_SHARED_KEY
    assert census["shared_key_members"] == [PEERS[5]]


def test_a_pool_with_no_weak_member_says_nothing_about_a_weakest_link():
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    admit(record, identity_join(record, PEERS[0], IDENTITIES[PEERS[0]]),
          roster=roster, directory=directory)
    rendered = pp.render_status(record, roster, directory)
    assert "WEAKEST LINK" not in rendered
    assert f"{pi.IDENTITY_SHARED_KEY}=0" in rendered


def test_the_member_row_names_the_backing_and_the_key():
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    receipt = admit(record, identity_join(record, PEERS[0],
                                          IDENTITIES[PEERS[0]]),
                    roster=roster, directory=directory)
    assert receipt["identity"] == pi.IDENTITY_ASYMMETRIC
    assert receipt["key_id"] == IDENTITIES[PEERS[0]].key_id
    rendered = pp.render_status(record, roster, directory)
    assert f"identity={pi.IDENTITY_ASYMMETRIC}/{IDENTITIES[PEERS[0]].key_id}" \
        in rendered


def test_the_roster_round_trips_with_the_backing():
    record = make_charter(pp.MODE_ASYMMETRIC)
    directory = pinned_directory()
    roster = fresh_roster(record)
    admit(record, identity_join(record, PEERS[0], IDENTITIES[PEERS[0]]),
          roster=roster, directory=directory)
    rebuilt = pp.Roster.from_dict(json.loads(json.dumps(roster.as_dict())))
    assert rebuilt.members[PEERS[0]].identity == pi.IDENTITY_ASYMMETRIC
    assert rebuilt.members[PEERS[0]].key_id == IDENTITIES[PEERS[0]].key_id


def test_a_charter_declaring_an_unknown_identity_mode_is_refused():
    with pytest.raises(pp.PoolError) as caught:
        pp.PoolCharter(pool_id="x", tiers={pp.ENTRY_TIER: pp.TierGrant()},
                       identity_mode="whatever")
    assert "identity_mode" in str(caught.value)


def test_the_identity_mode_is_covered_by_the_charter_signature():
    record = make_charter(pp.MODE_ASYMMETRIC)
    edited = dict(record)
    edited["identity_mode"] = pp.MODE_SHARED_KEY
    ok, _ = pp.verify_charter(edited, OP_KEY)
    assert not ok, (
        "an operator that could flip the identity mode without re-signing "
        "could downgrade the whole pool with one edit")


# ---------------------------------------------------------------------------
# 7. this module adds no guarantee code, and no second crypto implementation
# ---------------------------------------------------------------------------


def test_the_identity_links_register_no_guarantee_code():
    from revl import diagnostics

    for link in (pp.LINK_IDENTITY_MODE, pp.LINK_IDENTITY_DOWNGRADE,
                 pp.LINK_UNKNOWN_KEY, pp.LINK_REVOKED_KEY):
        assert link == link.lower()
        assert link not in diagnostics.GUARANTEES
    source = Path(pi.__file__).read_text(encoding="utf-8")
    code = [line for line in source.splitlines()
            if not line.lstrip().startswith("#")]
    assert not [line for line in code if "GUARANTEES" in line]


def test_the_identity_module_implements_no_curve_of_its_own():
    """Item 272's rule, held mechanically: the arithmetic lives in `tee_quote`,
    which is the implementation OpenSSL is differentially tested against. A
    second one here would be a second thing to get wrong and a second thing
    nothing cross-checks."""
    import ast

    source = Path(pi.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {node.name for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)}
    for forbidden in ("_add", "_mul", "_on_curve", "_rfc6979_k", "ecdsa_sign",
                      "ecdsa_verify"):
        assert forbidden not in defined, (
            f"peer_identity defines {forbidden}; the curve arithmetic belongs "
            f"to tee_quote, which the differential covers")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.split(".")[0] not in {"cryptography", "ecdsa", "nacl"}, (
                f"peer_identity imports {name}; revl ships with no runtime "
                f"dependencies and the differential must compare two "
                f"implementations, not one against itself")


def test_the_pool_publishes_no_asymmetric_primitive_to_revl_programs():
    """The exposure decision, held so it cannot drift silently.

    ``stdlib/crypto.rvl`` publishes four symmetric primitives and nothing
    asymmetric. If a later change publishes a signer or a verifier there, that
    is a decision with its own tier bar and its own differential, and it should
    fail this test and be argued rather than appear."""
    stdlib = Path(__file__).resolve().parents[1] / "stdlib" / "crypto.rvl"
    text = stdlib.read_text(encoding="utf-8")
    published = {line.split("fn ")[1].split("(")[0]
                 for line in text.splitlines()
                 if line.startswith("pub extern") and "fn " in line}
    assert published == {"sha256", "hmac_sha256", "ct_equal", "random_token"}


@pytest.mark.parametrize("hostile", [
    {"peer_id": "peer-0", "sign_alg": {"nested": "object"}},
    {"peer_id": "peer-0", "sign_alg": ["a", "list"]},
    {"peer_id": "peer-0", "sign_alg": None},
    {"peer_id": "peer-0", "sign_alg": pi.SIGN_ALG, "key_id": {"not": "a string"}},
    {"peer_id": "peer-0", "sign_alg": pi.SIGN_ALG, "key_id": "0" * 16,
     "signature": 17},
    {"peer_id": ["not", "a", "string"]},
    {"sign_alg": pi.SIGN_ALG},
    [],
    "a string",
    None,
])
def test_a_hostile_record_is_refused_not_raised(hostile):
    """The gate never raises on peer-supplied input, and the identity checks do
    not become the one place it does.

    An unhashable `sign_alg` is the specific shape worth naming: a dict lookup
    keyed on a record member would raise on it, and it would raise on the
    REFUSAL path, which is the path that exists for hostile input."""
    record = make_charter(pp.MODE_MIXED)
    receipt = admit(record, hostile, directory=pinned_directory())
    assert receipt["verdict"] == pp.REFUSE
    assert receipt["link"] in pp.REFUSAL_LINKS
    assert pp.identity_backing(hostile) in (None, pi.IDENTITY_ASYMMETRIC,
                                            pi.IDENTITY_SHARED_KEY)
