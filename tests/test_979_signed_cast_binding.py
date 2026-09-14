"""A cast is SIGNED over the question it answers - issue #979, item 471.

`tests/test_979_quorum_identity_binding.py` is the first half of this: a cast is
attributed to a bound identity, and the count is of derived principals rather
than of strings. It binds identities with a BEARER credential, and the design
note says plainly what that leaves open (Decision 7, "the honest bound"): the
secret is handed to the session to cast it, so everything that can see one
honest cast - the session process, the transport, a log, the proposer watching
the question it opened - can cast as that operator on every later question.

This suite is the second half. An operator declares the PUBLIC half of a P-256
key in the profile and signs the question's own binding; the private half never
crosses the wire. Written attack-first, because the attacks are the content:

  * `test_a_captured_bearer_credential_satisfies_a_later_question` is the
    baseline, and it PASSES - it is the standing weakness, pinned so the suite
    below is measured against something real rather than against a strawman;
  * the signed cases then show the same capture buying nothing: a proof lifted
    off one question does not answer another question, another round of the same
    question, another act on it, or the other vote;
  * `test_a_signed_operator_cannot_be_cast_for_with_a_bearer_secret` pins that
    there is no downgrade. An operator that declares a signing key is cast for
    by proof or not at all - if a secret were also accepted, declaring the key
    would bound nothing.

Credential LIFETIME is the other half of the file. A credential with no expiry
and no revocation path is a permanent grant, and #979's residual list names that
as its own missing piece. `until` and `revoked` are checked before the proof is
looked at, so a revoked operator's still-valid signature is refused on the same
ground a lapsed one is.

WHAT THIS DOES NOT PROVE, and the suite says so in code rather than only in
prose: `test_one_caller_holding_two_private_keys_still_satisfies_the_quorum` is
the attack that still works. Signing moves the thing that must be held from a
value the session can capture to one it never sees. It does not make the count a
count of people, and closing that needs the per-caller authenticated transport.
The test is written as an EXPECTED ADMISSION so that a later change which
actually closes it will red this file and force the claim to be restated.
"""

import copy
import hashlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import replay as _replay                                       # noqa: E402

from revl.compiler import compile_source                       # noqa: E402
from revl.mcp import operator as op                            # noqa: E402
from revl.mcp import quorum as _quorum                          # noqa: E402
from revl.mcp.approval import ApprovalRequired, ClassMap       # noqa: E402
from revl.mcp.session import Session, SessionError             # noqa: E402
from revl.policy import ApprovalRule, Policy                   # noqa: E402
from revl.tee_quote import (CURVE_P256, derive_public_key,     # noqa: E402
                            private_key_from_seed)

_SOURCE = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_THREE = ("alice", "bob", "carol")

#: The bearer secrets of the older, weaker form, kept so the two can be compared
#: side by side in one session.
_SECRETS = {"alice": "alice-secret-0", "bob": "bob-secret-1",
            "carol": "carol-secret-2"}


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _keypair(token: str):
    """A deterministic P-256 keypair per operator. Deterministic so a failure is
    reproducible from the name alone; the seeds are in this file, which is the
    whole point - a test fixture's private key is not a secret and pretending
    otherwise would only make the fixture unreviewable."""
    private = private_key_from_seed(CURVE_P256, f"979/{token}".encode("utf-8"))
    return private, derive_public_key(CURVE_P256, private).hex()


_KEYS = {token: _keypair(token) for token in ("alice", "bob", "carol", "dave")}


def _signed_profile(tokens=_THREE, shared=None, until=None, revoked=()) -> str:
    """A profile whose operators bind casts BY SIGNATURE.

    `shared` maps a token to the token whose KEY it reuses, which is the
    misconfiguration `same-principal` exists for and which a count of names
    cannot see. `until` and `revoked` drive the lifetime cases."""
    lines = []
    for token in sorted(tokens):
        _, public = _KEYS[(shared or {}).get(token, token)]
        clause = f"operator {token} sign p256:{public}"
        if until is not None:
            clause += f" until {until}"
        lines.append(clause)
        if token in revoked:
            lines.append(f"operator {token} revoked")
        lines.append(f"operator {token} may approve on *")
    return "\n".join(lines) + "\n"


def _bearer_profile(tokens=_THREE) -> str:
    lines = []
    for token in sorted(tokens):
        lines.append(f"operator {token} key sha256:{_digest(_SECRETS[token])}")
        lines.append(f"operator {token} may approve on *")
    return "\n".join(lines) + "\n"


def _harness(tmp_path, *, rules=None, token="dave", profile=None,
             name="wal.json", clock=None):
    """A session on the approval path with no cordis runtime.

    The bound operator defaults to `dave`, who is NOT one of the rule's named
    approvers. That matters: on one session the bound operator is the proposer
    and separation of duties excludes it from the count, so a quorum gathered
    here is gathered entirely from credentials - which is the shape the
    credential mechanism actually has to carry."""
    ir = copy.deepcopy(compile_source(_SOURCE, "quorum979s.rvl"))
    session = Session()
    session.recorder = _replay.Recorder(copy.deepcopy(ir))
    session._wal_path = str(tmp_path / name)
    session._generation = 1
    session._ensure_wal_open()
    session.approval_policy = "auto"
    session._class_map = ClassMap(ir)
    session.sandbox = Policy(approval_rules=tuple(
        rules if rules is not None
        else [ApprovalRule("announce", None, 2, _THREE)]))
    registry = op.parse_profile(
        profile if profile is not None else _signed_profile())
    session.operator_registry = registry
    session.operator = registry.get(token) or op.Operator(token=token)
    if clock is not None:
        session._clock_ms = clock
    return session


def _ticket(session, args=("sink.log", "a")):
    with pytest.raises(ApprovalRequired) as caught:
        session._approval_decide_call("ops", "shout", list(args))
    return caught.value.ticket


def _binding(session, ticket, *, token, action="vote", vote="approve"):
    """The exact binding the session will hand the verifier, built through the
    session's own `_cast_binding` rather than reassembled here. A test that
    rebuilt the message independently would pass while the client and the server
    disagreed about it, which is the one failure this protocol cannot tolerate.

    Only a vote carries a vote: an escalation, a revocation and an override are
    acts on the question and have nothing to approve or deny, so their binding
    names no vote and a signature for one is not a signature for a ballot."""
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    return session._cast_binding(record, action, as_token=token,
                                 vote=vote if action == "vote" else None)


def _proof(session, ticket, token, *, action="vote", vote="approve",
           signer=None):
    """`token`'s signature over this question's binding."""
    private, _ = _KEYS[signer or token]
    return _quorum.sign_cast(
        private, _binding(session, ticket, token=token, action=action,
                          vote=vote))


def _open(session, ticket):
    """Open the decision graph without casting, so a binding can be built for a
    question no vote has touched yet."""
    rule = session._ticket_approval_shape(ticket)
    return session._open_quorum(ticket, rule)


# --------------------------------------------------------------------------- #
# The standing weakness the signed form answers                               #
# --------------------------------------------------------------------------- #

def test_a_captured_bearer_credential_satisfies_a_later_question(tmp_path):
    """The baseline, and it PASSES: a bearer secret is a credential for every
    question, not for the one it was presented on.

    Read this as the measurement the rest of the file is against. `bob` votes
    honestly on question one; the secret is now on the wire, in the session's
    own arguments, and in anything that logged the call. Presented verbatim
    against an entirely different question it is accepted, because a digest
    comparison has no idea which question it is being asked about."""
    session = _harness(tmp_path, profile=_bearer_profile())
    first = _ticket(session, ("one.log", "a"))
    session.approve_ticket(first["hash"], as_token="bob",
                           as_secret=_SECRETS["bob"])

    second = _ticket(session, ("two.log", "b"))
    replayed = session.approve_ticket(second["hash"], as_token="bob",
                                      as_secret=_SECRETS["bob"])
    assert replayed["voter"] == "bob"
    assert replayed["counted"] == 1


def test_a_signed_cast_is_not_replayable_against_another_question(tmp_path):
    """The same capture against the signed form buys nothing.

    `bob`'s proof for question one is lifted verbatim and presented on question
    two. It is not a signature over question two's binding, so it is refused as
    `unproven-signature` and the second question stays at zero counted votes.
    A captured proof is a receipt for a cast already made, never a credential
    for the next one."""
    session = _harness(tmp_path)
    first = _ticket(session, ("one.log", "a"))
    _open(session, first)
    stolen = _proof(session, first, "bob")
    session.approve_ticket(first["hash"], as_token="bob", as_proof=stolen)

    second = _ticket(session, ("two.log", "b"))
    with pytest.raises(SessionError) as caught:
        session.approve_ticket(second["hash"], as_token="bob",
                               as_proof=stolen)
    assert "not a valid P-256 signature over THIS question" in str(caught.value)

    record = session._quorums[session._quorum_request_id(second["hash"])]
    assert record["votes"] == {}
    refusals = [row for row in session._approval_records
                if row.get("record") == "quorum-refused"
                and row.get("requestId") == record["requestId"]]
    assert [row["reason"] for row in refusals] == ["unproven-signature"]
    assert refusals[0]["voter"] == "bob"
    # the refusal row records who was ATTEMPTED and never the proof presented
    # against them: the graph is durable and read by auditors.
    assert "proof" not in refusals[0] and stolen not in str(refusals[0])


@pytest.mark.parametrize("action", ["escalate", "revoke", "override"])
def test_a_vote_proof_does_not_authorize_another_act_on_the_question(
        tmp_path, action):
    """One question, one signature, and still not a blank cheque.

    A proof made to APPROVE is presented to escalate, to revoke and to override
    the very same question. Each is a different authority - the override is
    gated by its own operator verb precisely because being trusted to cast one
    of N votes is not being trusted to stand in for all of them - so each needs
    its own signature and each is refused without one."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    _open(session, ticket)
    vote_proof = _proof(session, ticket, "bob", action="vote")

    verb = {"escalate": session.escalate_ticket,
            "revoke": session.revoke_ticket,
            "override": session.override_ticket}[action]
    with pytest.raises(SessionError) as caught:
        verb(ticket["hash"], reason="stalled", as_token="bob",
             as_proof=vote_proof)
    assert "not a valid P-256 signature over THIS question" in str(caught.value)

    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["outcome"] is None  # still open

    # ... and the act's OWN proof is accepted, so the refusal above is about the
    # binding and not about the act being unreachable.
    own = _proof(session, ticket, "bob", action=action)
    verb(ticket["hash"], reason="stalled", as_token="bob", as_proof=own)
    assert record["outcome"] is not None


def test_a_proof_for_approve_cannot_be_re_presented_as_a_denial(tmp_path):
    """The vote is inside the signed message, so a captured approval cannot be
    turned into a denial (nor a denial into an approval).

    This matters more than it looks: a denial can CLOSE a question outright once
    the remaining approvers cannot reach the count, so flipping a captured vote
    is not a lesser attack than forging one."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    _open(session, ticket)
    approval = _proof(session, ticket, "bob", vote="approve")

    with pytest.raises(SessionError) as caught:
        session.approve_ticket(ticket["hash"], vote="deny", as_token="bob",
                               as_proof=approval)
    assert "THIS question" in str(caught.value)
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["votes"] == {} and record["outcome"] is None


def test_a_proof_is_not_transferable_to_another_name(tmp_path):
    """`asToken` is inside the signed message, so a proof `carol` made cannot be
    re-presented as `bob`'s - even when the profile hands both the same key.

    The shared-key half is the interesting one. Two operators issued one key are
    ONE principal and supply one vote between them, and the older `same-principal`
    check catches that after the fact. This catches it before: the row the graph
    is being asked to write is part of what was signed, so the second name cannot
    even be attempted with the first's proof."""
    session = _harness(tmp_path,
                       profile=_signed_profile(shared={"bob": "carol"}))
    ticket = _ticket(session)
    _open(session, ticket)
    carols = _proof(session, ticket, "carol")

    with pytest.raises(SessionError) as caught:
        session.approve_ticket(ticket["hash"], as_token="bob",
                               as_proof=carols)
    assert "THIS question" in str(caught.value)

    # the key really is shared, so this is about the BINDING and not about bob
    # being unable to prove the key at all.
    session.approve_ticket(ticket["hash"], as_token="bob",
                           as_proof=_proof(session, ticket, "bob",
                                           signer="carol"))
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert set(record["votes"]) == {"bob"}


def test_a_signed_operator_cannot_be_cast_for_with_a_bearer_secret(tmp_path):
    """There is no downgrade path, which is the whole reason the two credential
    kinds are mutually exclusive.

    An operator that declares a signing key is cast for by proof or not at all.
    If a secret were accepted beside the key, an attacker holding the secret
    would not care that a stronger credential also existed, and declaring the
    key would bound nothing."""
    session = _harness(tmp_path)
    ticket = _ticket(session)

    with pytest.raises(SessionError) as caught:
        session.approve_ticket(ticket["hash"], as_token="bob",
                               as_secret=_SECRETS["bob"])
    message = str(caught.value)
    assert "binds its casts by SIGNATURE" in message
    assert "replayable against every later question" in message

    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    refusals = [row for row in session._approval_records
                if row.get("record") == "quorum-refused"
                and row.get("requestId") == record["requestId"]]
    assert [row["reason"] for row in refusals] == ["unsigned-cast"]


def test_a_bare_name_is_still_refused_against_a_signing_profile(tmp_path):
    """The fail-closed floor, restated for the signed form: a name with no proof
    behind it is refused, exactly as a name with no secret behind it was."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    with pytest.raises(SessionError) as caught:
        session.approve_ticket(ticket["hash"], as_token="bob")
    assert "must carry `asProof`" in str(caught.value)


def test_a_proof_against_an_operator_with_no_signing_key_is_refused(tmp_path):
    """A proof needs something to be checked against. An operator declared with
    a bearer digest has no public key, so a signature attributed to it verifies
    against nothing and is refused rather than waved through."""
    session = _harness(tmp_path, profile=_bearer_profile())
    ticket = _ticket(session)
    _open(session, ticket)
    with pytest.raises(SessionError) as caught:
        session.approve_ticket(ticket["hash"], as_token="bob",
                               as_proof="00" * 64)
    assert "declares no cast-signing key" in str(caught.value)


@pytest.mark.parametrize("proof", ["", "not-hex", "ab", "00" * 64, "ff" * 64,
                                   "00" * 200])
def test_a_malformed_or_zero_proof_never_admits(tmp_path, proof):
    """Every shape of junk answers the same way: refused. The all-zero signature
    is the classic admission an ECDSA verifier gets wrong, and the empty string
    is the one that would read as "no proof presented" if the normalisation were
    sloppy - both land on a refusal, neither on a count."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    with pytest.raises(SessionError):
        session.approve_ticket(ticket["hash"], as_token="bob", as_proof=proof)
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["votes"] == {}


# --------------------------------------------------------------------------- #
# Non-vacuity: genuinely distinct signed principals still satisfy the quorum   #
# --------------------------------------------------------------------------- #

def test_two_distinct_signed_principals_still_admit_the_crossing(tmp_path):
    """The control. A mechanism that refused everything would pass every test
    above and be worthless, so this pins that the honest path still works end to
    end: two named approvers with two distinct keys sign this question's binding,
    the count is reached, the approval is minted, and the graph records that each
    cast was bound by SIGNATURE and which principal it counted as."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    _open(session, ticket)

    first = session.approve_ticket(ticket["hash"], as_token="bob",
                                   as_proof=_proof(session, ticket, "bob"))
    assert first["approved"] is False and first["counted"] == 1

    second = session.approve_ticket(ticket["hash"], as_token="carol",
                                    as_proof=_proof(session, ticket, "carol"))
    assert second["approved"] is True
    assert second["counted"] == 2

    votes = [row for row in session._approval_records
             if row.get("record") == "quorum-vote"]
    assert {row["voter"] for row in votes} == {"bob", "carol"}
    assert {row["boundBy"] for row in votes} == {"signature"}
    assert len({row["principal"] for row in votes}) == 2
    assert all(row["principal"].startswith("sign:") for row in votes)


def test_two_names_sharing_one_signing_key_are_one_principal(tmp_path):
    """Distinctness is of KEYS, not of names, on the signed path too.

    `bob` and `carol` are issued one key. Both sign correctly for their own
    name - so neither cast is forged - and the second is still refused as
    `same-principal`, because a rule that wants two distinct approvers has been
    handed one key twice."""
    session = _harness(tmp_path,
                       profile=_signed_profile(shared={"carol": "bob"}))
    ticket = _ticket(session)
    _open(session, ticket)

    session.approve_ticket(ticket["hash"], as_token="bob",
                           as_proof=_proof(session, ticket, "bob"))
    with pytest.raises(SessionError) as caught:
        session.approve_ticket(
            ticket["hash"], as_token="carol",
            as_proof=_proof(session, ticket, "carol", signer="bob"))
    assert "SAME principal" in str(caught.value)
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["outcome"] is None and set(record["votes"]) == {"bob"}


# --------------------------------------------------------------------------- #
# Credential lifetime: expiry and revocation                                  #
# --------------------------------------------------------------------------- #

def test_an_expired_credential_binds_no_cast(tmp_path):
    """A credential with an `until` clause stops binding when it lapses, and the
    refusal comes BEFORE the proof is examined: the question is not whether the
    holder can still prove the credential, it is whether the credential still
    means anything."""
    session = _harness(tmp_path,
                       profile=_signed_profile(until="2020-01-01T00:00:00Z"))
    ticket = _ticket(session)
    _open(session, ticket)
    with pytest.raises(SessionError) as caught:
        session.approve_ticket(ticket["hash"], as_token="bob",
                               as_proof=_proof(session, ticket, "bob"))
    assert "lapsed at" in str(caught.value)
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    refusals = [row for row in session._approval_records
                if row.get("record") == "quorum-refused"
                and row.get("requestId") == record["requestId"]]
    assert [row["reason"] for row in refusals] == ["expired-credential"]
    assert record["votes"] == {}


def test_a_credential_still_inside_its_window_counts(tmp_path):
    """The other side of the same line, so the expiry check is not just a
    refusal machine: a window that has not closed yet admits normally."""
    session = _harness(tmp_path,
                       profile=_signed_profile(until="2999-01-01T00:00:00Z"))
    ticket = _ticket(session)
    _open(session, ticket)
    result = session.approve_ticket(ticket["hash"], as_token="bob",
                                    as_proof=_proof(session, ticket, "bob"))
    assert result["voter"] == "bob" and result["counted"] == 1


def test_a_revoked_credential_binds_no_cast_even_with_a_valid_proof(tmp_path):
    """Revocation is not expiry arriving early - it is the deployer saying this
    identity no longer acts, effective now, with no window to wait out.

    `bob`'s signature is correct and current and is refused anyway, which is the
    property revocation has to have: a compromised key is revoked precisely
    BECAUSE somebody else can still produce valid signatures with it."""
    session = _harness(tmp_path, profile=_signed_profile(revoked=("bob",)))
    ticket = _ticket(session)
    _open(session, ticket)
    good = _proof(session, ticket, "bob")
    with pytest.raises(SessionError) as caught:
        session.approve_ticket(ticket["hash"], as_token="bob", as_proof=good)
    assert "REVOKED" in str(caught.value)
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["votes"] == {}

    # carol is untouched: revocation is per identity, not a profile-wide kill
    # switch that a typo could turn into an outage.
    session.approve_ticket(ticket["hash"], as_token="carol",
                           as_proof=_proof(session, ticket, "carol"))
    assert set(record["votes"]) == {"carol"}


def test_a_revoked_credential_cannot_be_worked_around_by_the_other_verbs(
        tmp_path):
    """Revocation reaches every act, not only the vote. Escalation and the
    emergency override are exactly what a revoked operator would reach for."""
    session = _harness(tmp_path, profile=_signed_profile(revoked=("bob",)))
    ticket = _ticket(session)
    _open(session, ticket)
    for action, verb in (("escalate", session.escalate_ticket),
                         ("override", session.override_ticket),
                         ("revoke", session.revoke_ticket)):
        with pytest.raises(SessionError) as caught:
            verb(ticket["hash"], reason="because", as_token="bob",
                 as_proof=_proof(session, ticket, "bob", action=action))
        assert "REVOKED" in str(caught.value)
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["outcome"] is None


# --------------------------------------------------------------------------- #
# The profile refuses what it cannot bind                                     #
# --------------------------------------------------------------------------- #

def test_a_profile_may_not_declare_both_credential_kinds():
    """Both kinds on one identity would leave the bearer path permanently open
    beside the signed one, so the signed one would bound nothing. It is a parse
    error rather than a silent preference, because a deployer who wrote both
    believes both are in force."""
    _, public = _KEYS["alice"]
    for text in (f"operator alice key sha256:{_digest('s')}\n"
                 f"operator alice sign p256:{public}\n",
                 f"operator alice sign p256:{public}\n"
                 f"operator alice key sha256:{_digest('s')}\n"):
        with pytest.raises(op.ProfileError) as caught:
            op.parse_profile(text)
        assert "already declares a vote credential" in str(caught.value)

    with pytest.raises(op.ProfileError) as caught:
        op.parse_profile(
            '{"operators": [{"token": "alice", "key": "sha256:' +
            _digest("s") + '", "sign": "p256:' + public + '"}]}')
    assert "declares both" in str(caught.value)


@pytest.mark.parametrize("clause", [
    "sign p256:not-hex",
    "sign p256:" + "ab" * 32,                 # a P-256 key is 64 bytes, not 32
    "sign " + "ab" * 64,                      # no suite named
    # 64 bytes of well-formed hex that is not a point on the curve: the shape is
    # right and the point is wrong, which is the case a length check misses.
    "sign p256:" + "11" * 64,
])
def test_an_unusable_signing_key_is_a_parse_error_not_a_dead_credential(clause):
    """An off-curve or malformed key is refused when the PROFILE LOADS.

    The alternative is a credential nothing can ever match, which the operator
    discovers as an unexplainable refusal in the middle of a quorum. Verifying
    against a point that is not on the curve is not verification, so there is no
    tolerant reading available either."""
    with pytest.raises(op.ProfileError):
        op.parse_profile(f"operator alice {clause}\n")


def test_a_naive_until_timestamp_is_refused():
    """A credential whose expiry moves with the reader's timezone is a
    credential whose lifetime nobody can state, so an offset is required."""
    _, public = _KEYS["alice"]
    with pytest.raises(op.ProfileError) as caught:
        op.parse_profile(
            f"operator alice sign p256:{public} until 2030-01-01T00:00:00\n")
    assert "carries no UTC offset" in str(caught.value)


def test_the_profile_carries_no_private_key_material():
    """What a `sign` profile is worth rests on this: the file names only public
    keys, so a reader of the profile - a backup, a diff, a config repo - cannot
    cast as anybody in it. The bearer form could never say that about the secrets
    it was issued from; it could only say the file did not happen to contain
    them."""
    profile = _signed_profile()
    for token in _THREE:
        private, public = _KEYS[token]
        assert public in profile
        assert format(private, "x") not in profile
        assert str(private) not in profile
        registry = op.parse_profile(profile)
        assert registry.get(token).sign_key == public
        assert registry.get(token).vote_key is None


# --------------------------------------------------------------------------- #
# The bound this does NOT close                                               #
# --------------------------------------------------------------------------- #

def test_one_caller_holding_two_private_keys_still_satisfies_the_quorum(
        tmp_path):
    """THE ATTACK THAT STILL WORKS, pinned as an admission on purpose.

    One caller, one session, two of the named approvers' private keys. It signs
    twice and the crossing is admitted, and the decision graph reads as two
    distinct principals because to this boundary it WAS two distinct keys.

    Nothing in this file claims otherwise. Signing changes what must be held
    (a key the session never sees, rather than a secret it is handed) and what a
    captured value is worth (one question, rather than all of them). It does not
    make the count a count of people: every cast still arrives on one session's
    wire, and a caller who has collected the keys satisfies the rule.

    Closing that is the per-caller authenticated transport - N casts on N
    authenticated connections, counted as connections. This test is written as
    an EXPECTED ADMISSION so that the change which finally closes it reds this
    file and forces the claim in the docs to be rewritten rather than quietly
    left overstated."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    _open(session, ticket)

    session.approve_ticket(ticket["hash"], as_token="bob",
                           as_proof=_proof(session, ticket, "bob"))
    admitted = session.approve_ticket(ticket["hash"], as_token="carol",
                                      as_proof=_proof(session, ticket, "carol"))
    assert admitted["approved"] is True
