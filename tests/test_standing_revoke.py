"""Early revocation of a session-scoped standing grant — roadmap item 379.

Design: docs/design/246-auto-approve.md (Decision 3, open question 2). Item 344
(fork b) added the STANDING GRANT — `revl_approve` against a CAPABILITY mints a
session-scoped grant that per-call class-(c) crossings consume against instead of
prompting single-use. A grant lapses on its own at its TTL, when its uses run
out, or at session end. What 344 has NO verb for is revoking a grant EARLY — an
operator who granted `announce` for 10 minutes and then changes their mind must
wait for the TTL. The harness (revl-harness F-R2.2) implemented that revocation
itself in `approval_gate.rvl`; this makes it a native revl verb (`revl_revoke`)
so the workaround can be deleted.

`revl_revoke` is SYMMETRIC with 344's `revl_approve`: it targets the SAME key
`revl_approve` mints against — a CAPABILITY (token when scoped by item 343, name
fallback otherwise) — and revokes every live standing grant for that capability
in this session, effective immediately, mid-session, without ending it. The next
matching class-(c) crossing prompts again (the grant no longer auto-approves).
Revoking a capability with no live grant is a clean typed no-op (count 0), not a
crash. A precise `request_id` (the id `mint_standing_grant` returns) revokes one
specific grant.

This suite proves: mint -> auto-approve -> revoke -> prompts-again; the clean
no-op on a missing grant; capability scoping (revoking A leaves B live); the
request-id precise shape; composition with an item-343 TOKEN-keyed grant; and
policy-off byte-identity. End to end through the live cordis-py runtime.

Reuses the 344 fixture: `stash` is class (a); `enqueue` is class (b); `shout` is
class (c) reaching capability `announce`; `pay` is class (c) reaching `charge`
and `refund`.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="standing-grant revocation is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

_SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "extern emission deferred fn deliver(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('deliver:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn charge(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('charge:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn refund(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('refund:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "  emission fn pay(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "    fn pay(sink, msg) { emit charge(sink, msg) compensate refund(sink, msg) }\n"
    "  }\n"
    "}\n"
)

# item 343: a capability-scoped emission extern is keyed on its declared TOKEN
# (`gateway.send`), not the extern name. A grant/revoke against `gateway.send`
# must compose with the token key, exactly as a name-keyed one does.
_SOURCE_TOKEN = (
    "extern emission[gateway.send] fn wire(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('wire:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Gw {\n"
    "  emission fn send(sink: Str, msg: Str)\n"
    "}\n"
    "component Gate provides gw: Gw {\n"
    "  provide gw {\n"
    "    fn send(sink, msg) { emit wire(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "standing_revoke.rvl")
_BASE_TOKEN = compile_source(_SOURCE_TOKEN, "standing_revoke_token.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _ir_token() -> dict:
    return copy.deepcopy(_BASE_TOKEN)


def _session(policy="auto"):
    from revl.mcp.session import Session
    s = Session()
    s.approval_policy = policy
    return s


def _lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# ---------------------------------------------------------------------------
# The headline: mint -> auto-approve -> revoke -> the next crossing PROMPTS again
# ---------------------------------------------------------------------------

@needs_cordis
def test_mint_autoapprove_revoke_then_prompts_again(sink):
    """The whole reason the verb exists. A standing `announce` grant auto-approves
    a shout; `revl_revoke announce` retires it mid-session; the NEXT shout — which
    the grant WOULD have covered — prompts again (fail-closed), nothing fires."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)

    session.mint_standing_grant(capability="announce", uses=99)
    session.call("ops", "shout", [sink, "before"])       # auto-approved
    assert _lines(sink) == ["announce:before"]
    assert session._owner.prompts["perCall"] == 0

    result = session.revoke_standing_grant(capability="announce")
    assert result["revoked"] is True
    assert result["count"] == 1
    assert result["capability"] == "announce"

    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "shout", [sink, "after"])
    assert _lines(sink) == ["announce:before"]           # the revoked grant fired nothing
    assert exc.value.ticket["component"] == "Agent"
    assert session._owner.prompts["perCall"] == 1        # prompts again


# ---------------------------------------------------------------------------
# Clean no-op: revoking a capability with no live grant is a typed result
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoke_missing_grant_is_a_clean_noop():
    """No live grant for the capability -> a typed `revoked: True, count: 0`
    result, NOT a crash. Idempotent: a second revoke of the same capability is
    also count 0."""
    session = _session()
    session.load(_ir(), record=True)

    out = session.revoke_standing_grant(capability="announce")   # never minted
    assert out == {"revoked": True, "count": 0, "capability": "announce",
                   "requestIds": []}

    session.mint_standing_grant(capability="announce", uses=2)
    first = session.revoke_standing_grant(capability="announce")
    assert first["count"] == 1
    second = session.revoke_standing_grant(capability="announce")  # already gone
    assert second["count"] == 0


@needs_cordis
def test_revoke_unknown_request_id_is_a_clean_noop():
    session = _session()
    session.load(_ir(), record=True)
    out = session.revoke_standing_grant(request_id="grant:99:nope")
    assert out["revoked"] is True and out["count"] == 0


# ---------------------------------------------------------------------------
# Capability scoping: revoking A leaves B's grant live
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoke_is_capability_scoped(sink):
    """`announce` and `charge` share Agent's reach-closure candidate hash, so the
    CAPABILITY is the discriminator on revoke exactly as on mint: revoking
    `announce` leaves a `charge` grant untouched — pay still auto-approves."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=5)
    session.mint_standing_grant(capability="charge", uses=5)

    session.revoke_standing_grant(capability="announce")

    with pytest.raises(ApprovalRequired):                # announce revoked -> prompt
        session.call("ops", "shout", [sink, "hi"])
    session.call("ops", "pay", [sink, "invoice"])        # charge still granted
    assert _lines(sink) == ["charge:invoice"]            # shout blocked, pay fired


# ---------------------------------------------------------------------------
# Precise shape: revoke ONE grant by the request id mint returns
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoke_by_request_id_targets_one_grant(sink):
    """Two grants for the same capability; a `request_id` revokes exactly the one
    it names, leaving the other live (the capability-wide revoke would take both)."""
    session = _session()
    session.load(_ir(), record=True)
    g1 = session.mint_standing_grant(capability="announce", uses=1)
    session.mint_standing_grant(capability="announce", uses=5)

    out = session.revoke_standing_grant(request_id=g1["requestId"])
    assert out["count"] == 1 and out["requestIds"] == [g1["requestId"]]

    # the second grant still covers the crossing — no prompt
    session.call("ops", "shout", [sink, "x"])
    assert _lines(sink) == ["announce:x"]
    assert session._owner.prompts["perCall"] == 0


# ---------------------------------------------------------------------------
# Item 343 composition: revoke a TOKEN-keyed grant
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoke_composes_with_a_token_keyed_grant(sink):
    """item 343 keys `emission[gateway.send]` on the TOKEN. A grant/revoke targets
    that same token key — mint against `gateway.send`, auto-approve, revoke
    `gateway.send`, and the next send prompts again."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir_token(), record=True)

    session.mint_standing_grant(capability="gateway.send", uses=99)
    session.call("gw", "send", [sink, "one"])            # auto-approved on the token
    assert _lines(sink) == ["wire:one"]

    result = session.revoke_standing_grant(capability="gateway.send")
    assert result["count"] == 1 and result["capability"] == "gateway.send"

    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "send", [sink, "two"])
    assert _lines(sink) == ["wire:one"]                  # revoked -> nothing fired
    assert "gateway.send" in exc.value.ticket["capabilities"]


# ---------------------------------------------------------------------------
# Metrics reflect the revocation
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoked_grant_shows_in_metrics(sink):
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=5)
    session.call("ops", "shout", [sink, "a"])            # one consumed
    session.revoke_standing_grant(capability="announce")

    metrics = session.approval_metrics()
    assert metrics["grantsConsumed"] == 1
    view = metrics["standingGrants"][0]
    assert view["capability"] == "announce"
    assert view["revoked"] is True
    assert view["consumed"] is True


# ---------------------------------------------------------------------------
# Policy-off byte-identity: the revoke surface is inert with no policy
# ---------------------------------------------------------------------------

@needs_cordis
def test_policy_off_revoke_is_inert():
    session = _session(policy=None)
    session.load(_ir(), record=True)
    out = session.revoke_standing_grant(capability="announce")
    assert out == {"revoked": True, "count": 0, "capability": "announce",
                   "requestIds": []}
    assert session._grants == []
    assert session.approval_metrics() is None


# ---------------------------------------------------------------------------
# Guard: revoke needs a capability or a request id
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoke_needs_a_selector():
    from revl.mcp.session import SessionError
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(SessionError, match="capability"):
        session.revoke_standing_grant()
