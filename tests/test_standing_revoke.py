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
    """`announce`, `charge`, and `refund` share Agent's reach-closure candidate
    hash, so the CAPABILITY is the discriminator on revoke exactly as on mint:
    revoking `announce` leaves the pay grants untouched — pay still auto-approves.

    `pay` reaches BOTH `charge` AND `refund` (emit + compensate), each a distinct
    class-(c) capability, so a fully-silent pay needs a grant for EACH (245/246-F1:
    a `charge` grant alone does not cover the `refund` crossing). Granting both
    and revoking only `announce` proves the revoke is capability-scoped without
    relying on one grant over-covering a sibling capability."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=5)
    session.mint_standing_grant(capability="charge", uses=5)
    session.mint_standing_grant(capability="refund", uses=5)

    session.revoke_standing_grant(capability="announce")

    with pytest.raises(ApprovalRequired):                # announce revoked -> prompt
        session.call("ops", "shout", [sink, "hi"])
    session.call("ops", "pay", [sink, "invoice"])        # charge+refund still granted
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


# ---------------------------------------------------------------------------
# revocation-F1: revoke-by-capability retires the grant that COVERS the cap,
# using the SAME predicate auto-approve uses — find and revoke can never disagree
# ---------------------------------------------------------------------------

@needs_cordis
def test_revoke_by_capability_retires_the_covering_grant_multi_cap(sink):
    """`pay` reaches TWO class-(c) capabilities, `charge` and `refund`; grants for
    both make pay silent. `revl_revoke refund` must RETIRE the refund grant — count
    1, not the silent `count: 0` no-op the minted-key mismatch used to return — and
    the next pay prompts again because `refund` is no longer covered, even though
    the `charge` grant is still live. The coverage predicate `revoke` retires by is
    the identical one `_find_standing_grant` auto-approves by, so "revoke what is
    firing" and "auto-approve what is granted" agree by construction."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="charge", uses=9)
    session.mint_standing_grant(capability="refund", uses=9)

    session.call("ops", "pay", [sink, "a"])             # jointly covered -> silent
    assert _lines(sink) == ["charge:a"]
    assert session._owner.prompts["perCall"] == 0

    out = session.revoke_standing_grant(capability="refund")
    assert out["count"] == 1                            # NOT a no-op
    assert out["capability"] == "refund"

    with pytest.raises(ApprovalRequired) as exc:        # refund uncovered -> prompt
        session.call("ops", "pay", [sink, "b"])
    assert set(exc.value.ticket["capabilities"]) == {"charge", "refund"}
    assert _lines(sink) == ["charge:a"]                 # the second pay fired nothing
    assert session._owner.prompts["perCall"] == 1


# ---------------------------------------------------------------------------
# 245/246-F2: a spawn created DURING a call is owner-installed, so its frame
# joins the session's live-frame registry (the commit/abort gate target)
# ---------------------------------------------------------------------------

_SOURCE_SPAWN = (
    "service Job { fn run() -> Int }\n"
    "service Boss { async fn hire() -> Int }\n"
    "component Worker provides job: Job {\n"
    "  let m = effect Map.new() undo m.drop()\n"
    "  provide job { fn run() = 0 }\n"
    "}\n"
    "component Manager provides boss: Boss {\n"
    "  provide boss {\n"
    "    async fn hire() {\n"
    "      let w = effect spawn Worker with { } undo w.dispose()\n"
    "      return 1\n"
    "    }\n"
    "  }\n"
    "}\n"
)


@needs_cordis
def test_spawn_in_call_frame_joins_the_owner_registry():
    """A frame created DURING a `call` (a spawn-in-call) must capture a LIVE session
    owner at `Frame.__init__` and join its live-frame registry — the commit/abort
    gate target and the runtime class-(c) crossing check. The call path used never
    to install the owner (it was cleared after load), so a spawn-in-call frame
    captured a cleared ambient owner (None) and took the fail-open path: an
    unchecked class-(c) crossing and a lost revert, the sibling of the swap-owner
    bug. Installing the owner across the call fixes it, proven here by the spawned
    Worker frame calling `register_frame` on THIS session's owner during the call."""
    from revl.mcp.session import Session
    session = Session()
    session.approval_policy = "auto"
    session.load(compile_source(_SOURCE_SPAWN, "spawn_in_call.rvl"), record=True)

    owner = session._owner
    registered: list = []
    original = owner.register_frame
    owner.register_frame = lambda frame: (registered.append(frame),
                                          original(frame))[1]

    before = len(owner._registry)
    out = session.call("boss", "hire", [])
    assert out["result"] == 1
    # the spawn-in-call Worker frame joined the registry (0 without the owner
    # install — the fail-open regression this guards).
    assert len(registered) >= 1
    assert len(owner._registry) > before


# ---------------------------------------------------------------------------
# revocation-F5b: a capability-wide revoke of an AMBIGUOUS capability is refused
# exactly as an ambiguous proactive mint is — a subject-scoped operator cannot
# use it to retire grants on components outside its scope
# ---------------------------------------------------------------------------

_SOURCE_AMBIGUOUS = (
    "extern emission[net.send] fn wireA(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('A:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission[net.send] fn wireB(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('B:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service GwA { emission fn send(sink: Str, msg: Str) }\n"
    "service GwB { emission fn send(sink: Str, msg: Str) }\n"
    "component GateA provides a: GwA {\n"
    "  provide a { fn send(sink, msg) { emit wireA(sink, msg) } }\n"
    "}\n"
    "component GateB provides b: GwB {\n"
    "  provide b { fn send(sink, msg) { emit wireB(sink, msg) } }\n"
    "}\n"
)


@needs_cordis
def test_ambiguous_capability_wide_revoke_is_refused_like_mint(sink):
    """The capability `net.send` is emitted by TWO components (GateA and GateB), so
    it resolves to two distinct closures. A proactive `mint(capability=net.send)` is
    already refused for that ambiguity; a capability-wide REVOKE had no such guard,
    so a subject-scoped operator (`may approve on payments`) could `revl_revoke
    net.send` UNGATED — the operator layer defers an ambiguous capability (it cannot
    scope it to one component) on the assumption the handler refuses, true for mint,
    false for revoke — and retire grants on components outside its scope. The fix
    gives revoke the same ambiguity refusal mint has. Retiring one grant by
    `requestId` stays available (the precise, scope-safe shape)."""
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import SessionError
    session = _session()
    session.load(compile_source(_SOURCE_AMBIGUOUS, "ambiguous_send.rvl"),
                 record=True)

    # a proactive mint is refused for the ambiguity (baseline, unchanged)
    with pytest.raises(SessionError, match="distinct closures"):
        session.mint_standing_grant(capability="net.send", uses=3)

    # mint one grant per component off its own outstanding ticket
    with pytest.raises(ApprovalRequired) as exc_a:
        session.call("a", "send", [sink, "x"])
    g_a = session.mint_standing_grant(
        ticket_hash=exc_a.value.ticket["hash"], capability="net.send", uses=3)
    with pytest.raises(ApprovalRequired) as exc_b:
        session.call("b", "send", [sink, "y"])
    session.mint_standing_grant(
        ticket_hash=exc_b.value.ticket["hash"], capability="net.send", uses=3)

    # the capability-wide revoke is now refused (would cross operator scopes)
    with pytest.raises(SessionError, match="distinct closures"):
        session.revoke_standing_grant(capability="net.send")

    # the precise per-grant shape still works and touches only the named grant
    out = session.revoke_standing_grant(request_id=g_a["requestId"])
    assert out["count"] == 1 and out["requestIds"] == [g_a["requestId"]]
