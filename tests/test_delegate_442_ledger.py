"""Typed delegation, the ledger slice: subject, chain, use attribution and
revocation (roadmap item 442, issue #2693;
docs/design/442-typed-delegation.md §5.3).

The design's in-process feature is one slice in spirit and several seams in
practice. This file pins the LEDGER half of it, the half the issue's first slice
names first:

  * a grant row gains `subject` (who the authority was lent to) and `chain` (who
    lent what to whom, narrowed how), beside the shipped `remainingUses`,
    `consumed` and `revoked`;
  * a delegated use is attributed to BOTH parties: `component` stays the lender
    (the lease holder), `subject` becomes the delegatee, and the use is spent on
    the lender's grant (consume-before-fire unchanged);
  * the delegated handle is revoked when the lender's lease lapses (TTL) or the
    lender's generation is swapped out from under it, forward-only: no new
    crossing through a dead delegation, and the row reads `revoked`;
  * one grant is lent to one subject at a time, and only on the surface it was
    minted for.

What this file does NOT pin, because it is not implemented yet: the mint (an
`effect lease` naming a wiring key, yielding a typed `Delegate[S]` handle), the
delegatee's call lowering/emission, and the runtime crossing that calls
`_delegate_use`. Those are the remaining half of the issue and are named in the
PR body rather than pretended here.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from revl.mcp.session import Session, SessionError  # noqa: E402

_CAP = 'fs.write(path="/tmp")'


def _session() -> Session:
    session = Session()
    session._session_id = "sess-1"
    return session


def _grant(session: Session, *, capability: str = _CAP, uses: int = 3,
           ttl_ms: int = 600_000, component: str = "A",
           candidate_hash: str = "hash-A") -> dict:
    """A standing-grant row with the exact shape `_mint_grant` writes.

    Built here rather than minted because a proactive mint needs a bound
    approval policy and a live class map, which would drag a whole composition
    into a test of the ledger's own enforcement. The row is the same dict the
    mint appends (`_mint_grant` writes `subject`/`chain` with the other fields),
    so the enforcement below sees exactly what a real mint produces.
    """
    now = session._now_ms()
    row = {
        "requestId": f"grant:{len(session._grants) + 1}:{capability}",
        "kind": "standing-grant",
        "capability": capability,
        "candidateHash": candidate_hash,
        "component": component,
        "session": session._session_id,
        "grantedAt": now,
        "expiresAt": now + ttl_ms,
        "remainingUses": uses,
        "consumed": False,
        "revoked": False,
        "lease": True,
        "declaredCeilings": {},
        # The delegation record, written by the first delegated use. A real mint
        # starts these None; the row a delegated use finds carries the lender's
        # scope hash taken when the handle was acquired.
        "subject": None,
        "chain": [],
        "lender": None,
        "lenderGeneration": None,
    }
    session._grants.append(row)
    return row


class _SwappedMap:
    """A class map whose lender closure resolves to `current` — the only surface
    `_lender_generation_live` reads."""

    def __init__(self, current: str) -> None:
        self.current = current
        self._reach: dict = {}

    def candidate_hash(self, closure) -> str:  # noqa: ARG002
        return self.current


# ---------------------------------------------------------------------------
# use attribution: three uses to B on A's grant, the fourth refused
# ---------------------------------------------------------------------------

def test_three_delegated_uses_succeed_and_are_attributed_to_both_parties():
    session = _session()
    grant = _grant(session, uses=3)

    for _ in range(3):
        session._delegate_use(grant["requestId"], "B", "fs.write")

    assert grant["remainingUses"] == 0
    assert grant["consumed"] is True, "the third use exhausts the grant"
    assert grant["component"] == "A", "the lender is unchanged: a delegation shares"
    assert grant["subject"] == "B", "the delegatee is the subject"
    assert grant["chain"] == [
        {"from": "A", "to": "B", "narrowed": ["path"]}], \
        "one link at depth one, recording the parameter the lease narrowed"

    with pytest.raises(SessionError, match="no uses left"):
        session._delegate_use(grant["requestId"], "B", "fs.write")
    assert grant["subject"] == "B", "a refused use changes no attribution"


def test_attribution_is_written_once_for_the_same_subject():
    session = _session()
    grant = _grant(session, uses=3)

    session._delegate_use(grant["requestId"], "B", "fs.write")
    session._delegate_use(grant["requestId"], "B", "fs.write")

    assert grant["chain"] == [{"from": "A", "to": "B", "narrowed": ["path"]}]
    assert grant["remainingUses"] == 1


def test_a_second_distinct_subject_is_refused():
    session = _session()
    grant = _grant(session, uses=3)
    session._delegate_use(grant["requestId"], "B", "fs.write")

    with pytest.raises(SessionError, match="already lent to `B`"):
        session._delegate_use(grant["requestId"], "C", "fs.write")
    assert grant["subject"] == "B"
    assert grant["remainingUses"] == 2, "a refused use is not spent"


def test_a_use_outside_the_granted_surface_is_refused():
    session = _session()
    grant = _grant(session, uses=3)

    with pytest.raises(SessionError, match="covers"):
        session._delegate_use(grant["requestId"], "B", "fs.read")
    assert grant["remainingUses"] == 3
    assert grant["subject"] is None


def test_an_unknown_request_id_is_refused_and_spends_nothing():
    session = _session()
    _grant(session, uses=3)

    with pytest.raises(SessionError, match="no standing grant"):
        session._delegate_use("grant:nope", "B", "fs.write")
    assert session._grants[0]["remainingUses"] == 3


def test_a_grant_minted_in_another_session_is_refused():
    session = _session()
    grant = _grant(session, uses=3)
    grant["session"] = "sess-other"

    with pytest.raises(SessionError, match="another session"):
        session._delegate_use(grant["requestId"], "B", "fs.write")


# ---------------------------------------------------------------------------
# revocation: the lender's lease lapses or the lender is swapped
# ---------------------------------------------------------------------------

def test_an_expired_lease_revokes_the_delegated_handle():
    session = _session()
    grant = _grant(session, uses=3, ttl_ms=1_000)
    session._clock_ms = lambda: grant["expiresAt"] + 1

    with pytest.raises(SessionError, match="lapsed"):
        session._delegate_use(grant["requestId"], "B", "fs.write")

    assert grant["revoked"] is True, "the row reads revoked"
    assert grant["remainingUses"] == 3, "a lapsed grant spends nothing"


def test_a_swapped_lender_revokes_the_delegated_handle():
    session = _session()
    grant = _grant(session, uses=3, candidate_hash="hash-A")
    grant["lender"] = "A"
    grant["lenderGeneration"] = "hash-A"
    session._class_map = _SwappedMap("hash-A-swapped")

    with pytest.raises(SessionError, match="swapped"):
        session._delegate_use(grant["requestId"], "B", "fs.write")

    assert grant["revoked"] is True
    assert grant["remainingUses"] == 3


def test_an_unchanged_lender_generation_keeps_the_delegation_live():
    session = _session()
    grant = _grant(session, uses=3, candidate_hash="hash-A")
    grant["lender"] = "A"
    grant["lenderGeneration"] = "hash-A"
    session._class_map = _SwappedMap("hash-A")

    session._delegate_use(grant["requestId"], "B", "fs.write")
    assert grant["revoked"] is False
    assert grant["remainingUses"] == 2


def test_an_explicitly_revoked_grant_cannot_be_used():
    session = _session()
    grant = _grant(session, uses=3)
    grant["revoked"] = True

    with pytest.raises(SessionError, match="revoked"):
        session._delegate_use(grant["requestId"], "B", "fs.write")
    assert grant["remainingUses"] == 3
