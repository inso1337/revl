"""Session-scoped standing capability grants — roadmap item 344 (fork b).

Design: docs/design/246-auto-approve.md (Decision 3, open question 2) — this
closes that open question one item early, the 251 distillation target.

The Slice-1 ticket two-step (`revl_approve(hash)`) mints a SINGLE-USE, EXACT-HASH
approval: it covers only the identical call, so a repeat-shaped session (n
class-(c) calls to the same capability with differing args) prompts n times — the
`perCall = 1 per unique call` gap the harness H4 measurement found. Fork (b) adds
the missing shape: a SESSION-SCOPED STANDING GRANT an operator mints once against
a CAPABILITY (+ a `uses` count and/or a TTL), keyed by the capability's semantic
identity (its reach-closure candidate hash), that per-call class-(c) crossings
consume against instead of prompting.

This suite proves the measured outcome (three repeat class-(c) crossings go from
three prompts to one mint), every invariant the grant preserves (hash-bound to
the candidate hash, expiring on an injected clock, uses-bounded, single-session,
capability-scoped so a grant for A never covers B), the grant-consumed-vs-prompted
metrics, and policy-off byte-identity — end to end through the live cordis-py
runtime.

Reuses the 246 fixture: `stash` is class (a); `enqueue` is class (b); `shout` is
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
    reason="standing grants are proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh` and run under its venv",
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

# a swap candidate: the SAME services, but shout's body differs, so Agent's
# semantic entry — and thus the reach-closure candidate hash — changes. A grant
# minted against the old hash must fail the crossing after this swap (invariant 4).
_SOURCE_SWAPPED = _SOURCE.replace(
    "fn shout(sink, msg) { emit announce(sink, msg) }",
    "fn shout(sink, msg) { emit announce(sink, msg + \"!\") }")

_BASE = compile_source(_SOURCE, "standing_approval.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _session(policy="auto"):
    from revl.mcp.session import Session
    s = Session()
    s.approval_policy = policy
    return s


def _clocked_session(start_ms: int = 0):
    """A session whose approval clock is a mutable box, so expiry is testable
    without sleeping (invariant 3, checked at the crossing)."""
    s = _session()
    box = {"now": start_ms}
    s._clock_ms = lambda: box["now"]
    return s, box


def _lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# ---------------------------------------------------------------------------
# The measured outcome: three repeat class-(c) crossings, one mint (0 prompts)
# ---------------------------------------------------------------------------

@needs_cordis
def test_three_repeat_crossings_take_one_mint_not_three_prompts(sink):
    """The shell-escape shape. Proactively mint an `announce` grant for 3 uses,
    then the three (differing-arg) shout calls all auto-approve against it — the
    session sees ONE operator decision and ZERO per-call prompts."""
    session = _session()
    session.load(_ir(), record=True)

    grant = session.mint_standing_grant(capability="announce", uses=3)
    assert grant["granted"] and grant["kind"] == "standing-grant"

    for msg in ("a", "b", "c"):
        out = session.call("ops", "shout", [sink, msg])
        assert out["result"] is None
    assert _lines(sink) == ["announce:a", "announce:b", "announce:c"]

    owner = session._owner
    assert owner.prompts["perCall"] == 0            # zero prompts for three calls
    assert session._grants_consumed == 3
    assert session._grants[0]["remainingUses"] == 0  # all three uses spent


@needs_cordis
def test_grant_minted_from_the_first_ticket_covers_the_repeats(sink):
    """The other entry point: the first crossing prompts once, the operator
    widens THAT ticket into a standing grant, and the identical-shaped repeats
    (differing args) then auto-approve — one prompt for the whole burst."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)

    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "shout", [sink, "a"])
    assert _lines(sink) == []                       # nothing fired
    assert session._owner.prompts["perCall"] == 1

    # widen the outstanding ticket into a 3-use standing grant (single capability
    # on the ticket, so `capability` need not be named)
    grant = session.mint_standing_grant(ticket_hash=exc.value.ticket["hash"], uses=3)
    assert grant["capability"] == "announce"

    for msg in ("a", "b", "c"):
        session.call("ops", "shout", [sink, msg])
    assert _lines(sink) == ["announce:a", "announce:b", "announce:c"]
    assert session._owner.prompts["perCall"] == 1   # still just the one prompt
    assert session._grants_consumed == 3


# ---------------------------------------------------------------------------
# Uses exhausted: the grant stops covering
# ---------------------------------------------------------------------------

@needs_cordis
def test_uses_exhausted_refuses_the_next_crossing(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=2)

    session.call("ops", "shout", [sink, "a"])       # use 1
    session.call("ops", "shout", [sink, "b"])       # use 2
    with pytest.raises(ApprovalRequired):           # exhausted -> prompt
        session.call("ops", "shout", [sink, "c"])

    assert _lines(sink) == ["announce:a", "announce:b"]
    assert session._grants_consumed == 2
    assert session._owner.prompts["perCall"] == 1   # the exhausted-then-prompt
    assert session._grants[0]["consumed"] is True


# ---------------------------------------------------------------------------
# Expiry: a grant past its TTL refuses at the crossing (injected clock)
# ---------------------------------------------------------------------------

@needs_cordis
def test_expiry_refuses_after_ttl(sink):
    from revl.mcp.approval import ApprovalRequired
    session, clock = _clocked_session(start_ms=1_000)
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=99, ttl_ms=10_000)

    clock["now"] = 5_000                            # inside the window
    session.call("ops", "shout", [sink, "a"])
    assert _lines(sink) == ["announce:a"]

    clock["now"] = 1_000 + 10_000 + 1               # one ms past expiresAt
    with pytest.raises(ApprovalRequired):
        session.call("ops", "shout", [sink, "b"])
    assert _lines(sink) == ["announce:a"]           # the expired grant fired nothing
    assert session._grants_consumed == 1            # only the in-window use


# ---------------------------------------------------------------------------
# ROADMAP 427 F8 — an expiry that can un-expire is not an expiry
#
# `Session._now_ms` was `int(time.time() * 1000)` with an injectable override and
# no anchor, and every expiry site was a bare `now > exp` re-derived per crossing.
# So a clock that moved BACKWARDS below a grant's `expiresAt` — an NTP step, a
# manual `date`, a DST/timezone change, a VM restored from a snapshot, or the test
# hook itself — handed a dead grant back for its remaining uses.
#
# The fix is two halves and needs both. The MONOTONIC ANCHOR makes the reading
# correct: the wall clock is sampled once and elapsed time comes from
# `time.monotonic_ns()`, so nothing that moves the machine's notion of "now" moves
# this. The DEAD LATCH makes it irreversible: expiry is recorded as an EVENT on
# the record the first time it is observed, not re-derived from the clock on every
# read, so once a grant has been seen expired no later reading of any clock —
# injected included — can revive it. The anchor alone would still lose to an
# injected clock; the latch alone would still misjudge a grant it had not yet
# observed past its deadline.
# ---------------------------------------------------------------------------

@needs_cordis
def test_a_rewound_clock_never_resurrects_an_expired_grant(sink):
    """The finding, end to end. The grant is observed expired, THEN the clock is
    wound back well inside its original window. It stays dead: the crossing
    prompts, nothing fires, and no use is spent."""
    from revl.mcp.approval import ApprovalRequired
    session, clock = _clocked_session(start_ms=1_000)
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=99, ttl_ms=10_000)

    clock["now"] = 5_000                            # inside the window
    session.call("ops", "shout", [sink, "a"])
    assert _lines(sink) == ["announce:a"]

    clock["now"] = 1_000 + 10_000 + 1               # one ms past expiresAt
    with pytest.raises(ApprovalRequired):
        session.call("ops", "shout", [sink, "b"])

    clock["now"] = 2_000                            # rewound, deep inside the ttl
    with pytest.raises(ApprovalRequired):
        session.call("ops", "shout", [sink, "c"])
    assert _lines(sink) == ["announce:a"]           # the dead grant fired nothing
    assert session._grants_consumed == 1            # still only the in-window use


@needs_cordis
def test_a_rewind_alone_cannot_stretch_a_grants_window(sink):
    """The other half, without ever crossing the deadline: the clock is wound back
    BEFORE any expiry is observed, so the latch is not what is being tested. The
    ratchet holds the reading at its high-water mark, so the grant dies on the
    schedule it was minted with rather than gaining the rewound interval."""
    from revl.mcp.approval import ApprovalRequired
    session, clock = _clocked_session(start_ms=1_000)
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=99, ttl_ms=10_000)

    clock["now"] = 10_500                           # inside, near the end
    session.call("ops", "shout", [sink, "a"])

    clock["now"] = 1_500                            # rewound: buys no extra window
    assert session._now_ms() == 10_500
    clock["now"] = 11_002                           # one ms past expiresAt
    with pytest.raises(ApprovalRequired):
        session.call("ops", "shout", [sink, "b"])
    assert _lines(sink) == ["announce:a"]


@needs_cordis
def test_expiry_is_latched_on_the_record_not_re_derived(sink):
    """The mechanism, asserted directly so the latch cannot be quietly replaced by
    a re-derived comparison that happens to pass the black-box tests above."""
    session, clock = _clocked_session(start_ms=1_000)
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=99, ttl_ms=10_000)
    grant = session._grants[-1]
    assert grant.get("expiredAt") is None

    clock["now"] = 11_001                           # one ms past expiresAt
    assert session._expired(grant) is True
    assert grant["expiredAt"] == 11_001             # the observation is recorded

    # the deadline itself is now irrelevant: even pushed out of reach, and with
    # the clock back inside the original window, the record stays dead.
    grant["expiresAt"] = 10 ** 12
    clock["now"] = 2_000
    assert session._expired(grant) is True


def test_the_wall_clock_reading_is_monotonic_anchored_not_a_bare_time_call():
    """No injected clock here — the DEFAULT path is the one an operator runs. The
    reading must not be `time.time()`, or a settimeofday moves every deadline."""
    import time as _time  # noqa: PLC0415

    from revl.mcp.session import Session

    s = Session()
    first = s._now_ms()
    assert abs(first - int(_time.time() * 1000)) < 60_000   # still epoch-ms
    assert s._clock_anchor is not None                      # sampled once...
    anchor = s._clock_anchor
    assert s._now_ms() >= first
    assert s._clock_anchor is anchor                        # ...and not re-sampled

    # a wall clock that jumps backwards cannot drag the reading with it, because
    # elapsed time comes from the monotonic source, not from `time.time()`.
    wall, mono = anchor
    s._clock_anchor = (wall - 3_600_000, mono)   # as if the anchor were an hour ago
    assert s._now_ms() >= first                  # the ratchet floor still holds


# ---------------------------------------------------------------------------
# Capability-scoped: a grant for A does not cover B
# ---------------------------------------------------------------------------

@needs_cordis
def test_grant_for_capability_a_does_not_cover_capability_b(sink):
    """`announce` and `charge` share the per-component reach-closure candidate
    hash (both live in Agent), so the CAPABILITY binding is the discriminator: a
    grant for `announce` covers shout and refuses pay."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=5)

    session.call("ops", "shout", [sink, "hi"])      # announce: covered
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "pay", [sink, "invoice"])   # charge/refund: not covered
    assert set(exc.value.ticket["capabilities"]) == {"charge", "refund"}
    assert _lines(sink) == ["announce:hi"]          # pay's charge never fired
    assert session._grants_consumed == 1


# ---------------------------------------------------------------------------
# 245/246-F1: a grant for ONE reached class-(c) cap does not cover a SIBLING
# ---------------------------------------------------------------------------

@needs_cordis
def test_partial_grant_does_not_over_cover_a_multi_capability_call(sink):
    """The over-coverage hole (245/246-F1). `pay` reaches TWO distinct class-(c)
    capabilities, `charge` AND `refund` (emit + compensate). A standing grant for
    `charge` ALONE must NOT auto-approve pay: the old gate tested single-membership
    of the grant's cap against the WHOLE reach fold (`charge in {charge, refund}`),
    so it fired pay — and with it the un-granted `refund` crossing — on the back of
    a charge-only grant (the shell-covers-shell+mail shape). The fix requires EVERY
    class-(c) cap covered, so pay prompts and the charge grant is NOT spent.

    Granting BOTH capabilities lets the SAME pay auto-approve, jointly covered,
    spending one use of each grant — no over-refusal."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="charge", uses=5)

    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "pay", [sink, "one"])   # refund un-granted -> prompt
    assert set(exc.value.ticket["capabilities"]) == {"charge", "refund"}
    assert _lines(sink) == []                        # nothing fired: no over-coverage
    assert session._grants_consumed == 0             # the charge grant not spent
    assert session._grants[0]["remainingUses"] == 5  # untouched

    # widen to cover the whole class-(c) reach; now the SAME pay auto-approves
    session.mint_standing_grant(capability="refund", uses=5)
    session.call("ops", "pay", [sink, "two"])
    assert _lines(sink) == ["charge:two"]            # forward emission fired
    assert session._owner.prompts["perCall"] == 1    # only the first, honest prompt
    assert session._grants_consumed == 2             # one use of charge + one of refund


# ---------------------------------------------------------------------------
# Invariant 4: a swap that changes the closure invalidates the grant
# ---------------------------------------------------------------------------

@needs_cordis
def test_swap_changing_the_closure_invalidates_the_grant(sink):
    """A grant is hash-bound to the capability's semantic identity. A swap that
    changes Agent's reach closure recomputes a different candidate hash, so the
    standing grant (bound to the old hash) fails the crossing with no revocation
    bookkeeping — the same trick as the Slice-1 token."""
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    session.mint_standing_grant(capability="announce", uses=5)
    session.call("ops", "shout", [sink, "before"])  # covered pre-swap
    assert session._grants_consumed == 1

    session.swap(compile_source(_SOURCE_SWAPPED, "standing_approval2.rvl"))
    with pytest.raises(ApprovalRequired):           # candidate hash moved
        session.call("ops", "shout", [sink, "after"])
    assert session._grants_consumed == 1            # the stale grant covered nothing


# ---------------------------------------------------------------------------
# Metrics: grant-consumed vs prompted
# ---------------------------------------------------------------------------

@needs_cordis
def test_metrics_reflect_grant_consumed_vs_prompted(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)

    # a prompt first (pay is not covered), then a grant covering two shouts
    with pytest.raises(ApprovalRequired):
        session.call("ops", "pay", [sink, "invoice"])
    session.mint_standing_grant(capability="announce", uses=2)
    session.call("ops", "shout", [sink, "a"])
    session.call("ops", "shout", [sink, "b"])

    metrics = session.approval_metrics()
    assert metrics["grantsConsumed"] == 2
    assert metrics["prompts"]["perCall"] == 1
    assert metrics["promptsPerSession"] == 1
    grant_view = metrics["standingGrants"][0]
    assert grant_view["capability"] == "announce"
    assert grant_view["remainingUses"] == 0
    assert grant_view["consumed"] is True


# ---------------------------------------------------------------------------
# Guards on minting
# ---------------------------------------------------------------------------

@needs_cordis
def test_unbounded_grant_is_refused():
    from revl.mcp.session import SessionError
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(SessionError, match="must be bounded"):
        session.mint_standing_grant(capability="announce")  # no uses, no ttl


@needs_cordis
def test_multi_capability_ticket_requires_naming_the_capability(sink):
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import SessionError
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "pay", [sink, "invoice"])   # reaches charge AND refund
    with pytest.raises(SessionError, match="capabilities"):
        session.mint_standing_grant(ticket_hash=exc.value.ticket["hash"], uses=3)
    # naming one resolves it
    grant = session.mint_standing_grant(
        ticket_hash=exc.value.ticket["hash"], capability="charge", uses=3)
    assert grant["capability"] == "charge"


@needs_cordis
def test_unknown_ticket_hash_is_refused_for_a_grant():
    from revl.mcp.session import SessionError
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(SessionError, match="unknown ticket hash"):
        session.mint_standing_grant(ticket_hash="sha256:deadbeef", uses=3)


@needs_cordis
def test_unknown_capability_is_refused_for_a_proactive_grant():
    from revl.mcp.session import SessionError
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(SessionError, match="not a live class-\\(c\\) crossing"):
        session.mint_standing_grant(capability="deliver", uses=3)  # (b), never prompts


# ---------------------------------------------------------------------------
# Policy-off byte-identity: the standing-grant surface is inert with no policy
# ---------------------------------------------------------------------------

@needs_cordis
def test_policy_off_is_byte_identical(sink):
    session = _session(policy=None)
    session.load(_ir(), record=True)
    # a class-(c) emission fires immediately, no ticket, no grant machinery
    out = session.call("ops", "shout", [sink, "hi"])
    assert out["result"] is None
    assert _lines(sink) == ["announce:hi"]
    assert "approval" not in session.state()
    assert session._grants == [] and session._grants_consumed == 0
    assert session.approval_metrics() is None
