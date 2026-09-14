"""Quorum casts are attributed to a BOUND identity - issue #979, item 471.

Slice 1 and Slice 2 took a cast's identity from `as_token`, a string the caller
chose, and counted distinct NAMES. Distinctness of names is not distinctness of
principals, so one operator satisfied `require 2 of {alice, bob, carol}` by
asserting two of the names in turn and the crossing was admitted. That is the
whole guarantee `require N of M` advertises, so this suite is written as the
attack first and the mechanism second.

What binds the identity, and what that is worth, is spelled out in
`revl.mcp.quorum.resolve_cast` and Decision 7 of
`docs/design/471-quorum-approval.md`. In one line: a cast counts for the
session's serve-time operator, or for an operator whose declared vote credential
the caller PROVED, and for nothing else. N counted votes therefore require N
distinct secrets. They do not prove N humans: a credential is bearer, and every
cast still arrives over one session's wire.

The failure direction is CLOSED throughout. Every identity this session cannot
bind - no profile to check against, an operator the profile does not carry, an
operator with no declared credential, a missing credential, a wrong one - is
REFUSED and recorded. There is no path here that admits on a name alone, which
is what the `_unbindable` parametrisation pins.
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
from revl.mcp.approval import ApprovalRequired, ClassMap       # noqa: E402
from revl.mcp.session import Session, SessionError             # noqa: E402
from revl.policy import ApprovalRule, Policy                   # noqa: E402

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

#: The secrets the deployer issues out of band. The PROFILE never carries these
#: - it carries their digests - which is why a reader of the profile cannot vote
#: as anyone in it.
_SECRETS = {"alice": "alice-secret-0", "bob": "bob-secret-1",
            "carol": "carol-secret-2", "mallory": "mallory-secret-3"}


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _profile(keys=_SECRETS, shared=None) -> str:
    """An operator profile declaring a vote-credential digest per operator.

    `shared` maps a token to the token whose secret it REUSES, which is the
    misconfiguration `same-principal` exists for: two names, one principal.
    """
    lines = []
    for token in sorted(keys):
        secret = _SECRETS[(shared or {}).get(token, token)]
        lines.append(f"operator {token} key sha256:{_digest(secret)}")
        lines.append(f"operator {token} may approve on *")
    return "\n".join(lines) + "\n"


def _harness(tmp_path, *, rules=None, token="alice", profile=_profile(),
             name="wal.json"):
    """A session on the approval path with no cordis runtime, bound to ONE
    operator (`token`) and served with the whole registry - which is exactly the
    shape `revl mcp serve --operator-profile FILE --operator TOKEN` produces."""
    ir = copy.deepcopy(compile_source(_SOURCE, "quorum979.rvl"))
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
    registry = op.parse_profile(profile) if profile is not None else None
    session.operator_registry = registry
    session.operator = (registry.get(token) if registry is not None
                        else op.Operator(token=token))
    return session


def _ticket(session, args=("sink.log", "a")):
    with pytest.raises(ApprovalRequired) as caught:
        session._approval_decide_call("ops", "shout", list(args))
    return caught.value.ticket


def _crosses(session, args=("sink.log", "a")) -> bool:
    """Whether the crossing is admitted - the only observation that matters."""
    try:
        session._approval_decide_call("ops", "shout", list(args))
    except ApprovalRequired:
        return False
    return True


def _records(session) -> list:
    return _replay.WriteAheadLog.read(session._approval_wal().path)["records"]


def _refusals(session) -> list:
    return [r for r in _records(session) if r["record"] == "quorum-refused"]


@pytest.fixture
def quorum(tmp_path):
    return _harness(tmp_path)


# ---------------------------------------------------------------------------
# The attack the issue names
# ---------------------------------------------------------------------------

def test_one_caller_cannot_satisfy_two_of_m_by_asserting_two_names(quorum):
    """THE defect of issue #979. One bound operator asserts two of the rule's
    names in turn and holds neither credential. Both casts are refused, nothing
    is counted, and the crossing stays refused.

    Before the identity binding this admitted: `as_token` was believed, the two
    names were distinct, and the count reached two."""
    ticket = _ticket(quorum)

    for name in ("bob", "carol"):
        with pytest.raises(SessionError) as refused:
            quorum.approve_ticket(ticket["hash"], as_token=name)
        assert "issue #979" in str(refused.value)

    assert quorum.quorum_state(ticket["hash"])["counted"] == 0
    assert not _crosses(quorum)
    assert [r["reason"] for r in _refusals(quorum)] == \
        ["unproven-identity", "unproven-identity"]
    assert [r["voter"] for r in _refusals(quorum)] == ["bob", "carol"]


def test_the_self_quorum_is_refused_on_a_session_with_no_profile(tmp_path):
    """The same attack in the shape it had BEFORE any of this existed: a session
    served with no operator profile at all, which is the tree's default and the
    exact configuration the pre-fix suites drive. One bound operator asserts
    `bob` then `carol` - no new vocabulary, no credential, nothing but the two
    names the rule carries.

    On the pre-fix tree both casts counted and the crossing was admitted. Now a
    session with no profile has exactly one identity, a second one cannot be
    bound at all, and both casts are refused as `unbound-identity`."""
    session = _harness(tmp_path, profile=None)
    ticket = _ticket(session)

    for name in ("bob", "carol"):
        with pytest.raises(SessionError) as refused:
            session.approve_ticket(ticket["hash"], as_token=name)
        assert "DISTINCT PRINCIPALS" in str(refused.value)

    assert session.quorum_state(ticket["hash"])["counted"] == 0
    assert not _crosses(session)
    assert [r["reason"] for r in _refusals(session)] == \
        ["unbound-identity", "unbound-identity"]


def test_holding_one_credential_carries_one_vote_and_not_the_quorum(quorum):
    """The narrower, more realistic attack: the caller genuinely holds ONE of
    the named approvers' credentials. That buys exactly the one vote it proves.
    The second name is still unprovable, so `require 2` is not reached and the
    crossing stays refused."""
    ticket = _ticket(quorum)

    cast = quorum.approve_ticket(ticket["hash"], as_token="bob",
                                 as_secret=_SECRETS["bob"])
    assert (cast["counted"], cast["approved"]) == (1, False)

    with pytest.raises(SessionError):
        quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert quorum.quorum_state(ticket["hash"])["counted"] == 1
    assert not _crosses(quorum)


def test_two_names_sharing_one_credential_are_one_principal(tmp_path):
    """The distinctness unit is the PRINCIPAL, not the name. A profile that
    issues one secret to two operators declares two names and one principal, and
    the second cast is refused as `same-principal` - a refusal a count of names
    cannot make, because both names are the rule's own."""
    session = _harness(tmp_path, profile=_profile(shared={"carol": "bob"}))
    ticket = _ticket(session)

    session.approve_ticket(ticket["hash"], as_token="bob",
                           as_secret=_SECRETS["bob"])
    with pytest.raises(SessionError) as refused:
        session.approve_ticket(ticket["hash"], as_token="carol",
                               as_secret=_SECRETS["bob"])
    assert "same principal" in str(refused.value).lower()
    assert session.quorum_state(ticket["hash"])["counted"] == 1
    assert not _crosses(session)
    assert [r["reason"] for r in _refusals(session)] == ["same-principal"]


# ---------------------------------------------------------------------------
# Non-vacuity: the mechanism still admits what it is supposed to admit
# ---------------------------------------------------------------------------

def test_two_distinct_principals_still_satisfy_the_quorum(quorum):
    """The control. Two genuinely distinct credentials, each proven, reach the
    count and admit the crossing exactly as before - so the binding refuses the
    self-quorum without refusing the quorum.

    Note what this shows about the session's own identity: the proposer is the
    serve-time operator (`alice` here), and the proposer can never be one of the
    N, so on a single session ALL N counted votes come from credentials."""
    ticket = _ticket(quorum)

    first = quorum.approve_ticket(ticket["hash"], as_token="bob",
                                  as_secret=_SECRETS["bob"])
    assert (first["approved"], first["counted"]) == (False, 1)

    second = quorum.approve_ticket(ticket["hash"], as_token="carol",
                                   as_secret=_SECRETS["carol"])
    assert (second["approved"], second["counted"]) == (True, 2)
    assert second["satisfiedBy"] == "votes"
    assert _crosses(quorum)


def test_the_graph_names_the_principal_and_never_the_credential(quorum):
    """The decision graph records WHAT bound each cast and the derived principal,
    so an audit can see the two votes came from two principals. It records
    neither the secret nor the digest the profile declares: the graph is durable
    and copied into audits, and a row carrying the verifier would let a reader of
    the record forge the next cast."""
    ticket = _ticket(quorum)
    quorum.approve_ticket(ticket["hash"], as_token="bob",
                          as_secret=_SECRETS["bob"])
    quorum.approve_ticket(ticket["hash"], as_token="carol",
                          as_secret=_SECRETS["carol"])

    votes = [r for r in _records(quorum) if r["record"] == "quorum-vote"]
    assert [r["boundBy"] for r in votes] == ["credential", "credential"]
    assert len({r["principal"] for r in votes}) == 2

    blob = repr(_records(quorum))
    for name, secret in _SECRETS.items():
        assert secret not in blob, name
        assert _digest(secret) not in blob, name


# ---------------------------------------------------------------------------
# The failure direction: an identity that cannot be bound REFUSES
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile,as_token,as_secret,reason", [
    # nothing to check a second identity against at all
    (None, "bob", _SECRETS["bob"], "unbound-identity"),
    # a name the served profile does not carry
    (_profile(), "nobody", "whatever", "unknown-operator"),
    # in the profile, but with no credential declared, so nothing can prove it
    ("operator alice may approve on *\noperator bob may approve on *\n",
     "bob", _SECRETS["bob"], "unkeyed-identity"),
    # the credential is simply absent
    (_profile(), "bob", None, "unproven-identity"),
    # the credential is another operator's
    (_profile(), "bob", _SECRETS["carol"], "unproven-identity"),
    # a credential with no name beside it: the session will not go looking for
    # whichever identity a secret happens to open
    (_profile(), None, _SECRETS["bob"], "unnamed-credential"),
], ids=["no-profile", "unknown-operator", "no-credential-declared",
        "credential-missing", "credential-wrong", "credential-unnamed"])
def test_an_unbindable_identity_refuses_and_is_recorded(
        tmp_path, profile, as_token, as_secret, reason):
    """Every way an identity fails to bind FAILS CLOSED: the cast is refused,
    the refusal is written to the decision graph before it is raised, and the
    count does not move. None of these admits."""
    session = _harness(tmp_path, profile=profile)
    ticket = _ticket(session)

    with pytest.raises(SessionError):
        session.approve_ticket(ticket["hash"], as_token=as_token,
                               as_secret=as_secret)
    assert [r["reason"] for r in _refusals(session)] == [reason]
    assert _refusals(session)[0]["proven"] is False
    assert session.quorum_state(ticket["hash"])["counted"] == 0
    assert not _crosses(session)


def test_an_override_cannot_be_attributed_to_an_unprovable_operator(quorum):
    """The same binding on the emergency path. An override is the one act that
    admits a crossing WITHOUT the count, so who it is attributed to is the whole
    of its accountability: an override recorded against a name the caller merely
    typed is an unattributable act wearing somebody else's name. It is refused,
    and no `quorum-override` row is written."""
    ticket = _ticket(quorum)

    with pytest.raises(SessionError) as refused:
        quorum.override_ticket(ticket["hash"], reason="sev1",
                               as_token="carol")
    assert "issue #979" in str(refused.value)
    assert not [r for r in _records(quorum) if r["record"] == "quorum-override"]
    assert [r["reason"] for r in _refusals(quorum)] == ["unproven-identity"]
    assert not _crosses(quorum)


@pytest.mark.parametrize("verb", ["escalate_ticket", "revoke_ticket"])
def test_closing_a_question_also_takes_a_bound_identity(quorum, verb):
    """Escalation and revocation close a question on somebody's authority, so
    they are bound the same way. A proven credential closes it; an asserted name
    does not."""
    ticket = _ticket(quorum)

    with pytest.raises(SessionError):
        getattr(quorum, verb)(ticket["hash"], reason="stalled", as_token="bob")
    assert quorum.quorum_state(ticket["hash"])["outcome"] is None

    closed = getattr(quorum, verb)(ticket["hash"], reason="stalled",
                                   as_token="bob", as_secret=_SECRETS["bob"])
    assert closed["by"] == "bob"
    assert not _crosses(quorum)


# ---------------------------------------------------------------------------
# The profile surface
# ---------------------------------------------------------------------------

def test_the_profile_carries_a_digest_and_refuses_anything_else():
    """The profile declares the DIGEST of a credential, never the credential:
    the file is configuration that gets read, copied and diffed, and one that
    carried the secrets would hand every voter identity to anyone who can read
    it. A line that is not a digest is a parse error rather than a credential
    nothing can ever match."""
    registry = op.parse_profile(
        f"operator bob key sha256:{_digest(_SECRETS['bob'])}\n"
        f"operator carol key {_digest(_SECRETS['carol'])}\n"
        "operator carol may approve on *\n")
    assert registry.get("bob").vote_key == _digest(_SECRETS["bob"])
    assert registry.get("carol").vote_key == _digest(_SECRETS["carol"])
    assert registry.get("bob").grants == ()

    for bad in ("operator bob key hunter2\n",
                "operator bob key sha256:not-hex\n",
                f"operator bob key sha256:{_digest('x')[:63]}\n"):
        with pytest.raises(op.ProfileError):
            op.parse_profile(bad)


def test_the_json_profile_carries_the_same_credential():
    registry = op.parse_profile(
        '{"operators": [{"token": "bob", "key": "%s",'
        ' "grants": [{"verbs": ["approve"], "on": ["*"]}]}]}'
        % _digest(_SECRETS["bob"]))
    assert registry.get("bob").vote_key == _digest(_SECRETS["bob"])


def test_two_credentials_for_one_operator_are_refused():
    """One identity, one principal: a second credential for the same token would
    make it ambiguous which principal a cast proved, and two principals behind
    one name is the thing the count must not have."""
    with pytest.raises(op.ProfileError):
        op.parse_profile(
            f"operator bob key sha256:{_digest('a')}\n"
            f"operator bob key sha256:{_digest('b')}\n")
