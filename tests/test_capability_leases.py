"""The `effect lease` capability-lease form — item 294 Slice 2.

`let l = effect lease fs.write(path="/tmp") ttl 10m uses 3 undo l.revoke()`
acquires a TTL/uses-bounded standing grant over the capability's cone. The
acquisition is CLASS-(c)-GATED and TICKET-MEDIATED, never a silent self-mint:

  * an UNGATED run (no approval policy) REFUSES the lease at load — there is no
    operator to consent, and a program may not self-convert prompt-per-call into
    prompt-never (the consent bypass the design review found and closed);
  * a GATED run raises ONE ticket naming the lease cone and ttl/uses; the
    operator mints the standing grant FROM that ticket (the shipped 246 path),
    and the body's class-(c) crossings within the cone auto-approve against it
    (the item-248 economics: three prompts to one, never to zero);
  * the disposer `l.revoke()` retires the grant by its OWN requestId on the LIFO
    teardown (the one scoped revoke exemption), and a ttl/uses lease lapses via
    the shipped `expiresAt`/`remainingUses`.

Grammar/lowering is checked without a runtime; the lifecycle runs end to end
through the live cordis-py composition.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import RevlError, compile_source  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402
from revl.parser import LeaseAcquire, LetEffect, Parser  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the lease lifecycle is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh`",
)


# A component that acquires an `fs.write(path="/tmp")` lease in its activation
# body and, while it holds the lease, crosses that boundary from a provide
# method. The crossing declares the SAME cone the lease grants.
_SOURCE = (
    'extern emission[fs.write(path="/tmp")] fn wr(sink: Str, msg: Str)'
    " = @py {\n    with open(sink, 'a') as f: f.write('w:' + msg + '\\n')\n"
    "    return\n}\n"
    "service Ops { emission fn go(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    '  let l = effect lease fs.write(path="/tmp") ttl 10m uses 3 undo l.revoke()\n'
    "  provide ops { fn go(sink, msg) { emit wr(sink, msg) } }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "capability_lease.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# ---------------------------------------------------------------------------
# Grammar and lowering (no runtime)
# ---------------------------------------------------------------------------

def _lease_component(src_body: str):
    src = ("service S { fn a() -> Int }\n"
           "component C provides x: S {\n  " + src_body + "\n"
           "  provide x { fn a() = 0 }\n}\n")
    return Parser(src, "<t>").parse().components[0]


def test_lease_parses_to_lease_acquire():
    comp = _lease_component(
        'let l = effect lease fs.write(path="/tmp") ttl 10m uses 3 undo l.revoke()')
    stmt = comp.body[0]
    assert isinstance(stmt, LetEffect)
    assert isinstance(stmt.acquire, LeaseAcquire)
    assert stmt.acquire.capability == 'fs.write(path="/tmp")'
    assert stmt.acquire.ttl_ms == 600_000
    assert stmt.acquire.uses == 3


def test_lease_lowers_to_gated_acquire_and_own_revoke():
    ir = _ir()
    step = next(s for c in ir["components"] for s in c["body"] if s.get("lease"))
    assert step["acquire"]["kind"] == "lease-acquire"
    assert step["lease"]["capability"] == 'fs.write(path="/tmp")'
    assert step["undo"]["kind"] == "lease-revoke"
    assert step["undo"]["handle"] == step["bind"]


@pytest.mark.parametrize("body,needle", [
    ('let l = effect lease fs.write(path="/tmp") undo l.revoke()',
     "must be bounded"),
    ('let l = effect lease fs.write(pth="/tmp") ttl 1m undo l.revoke()',
     "unknown capability parameter"),
    ('let l = effect lease fs.write(path="relative") ttl 1m undo l.revoke()',
     "is not absolute"),
])
def test_lease_parse_refusals(body, needle):
    with pytest.raises(RevlError) as exc:
        _lease_component(body)
    assert needle in str(exc.value)


def test_lease_is_a_contextual_keyword_not_reserved():
    # `lease` still names a field/binding/method: the lease form is only the
    # `effect lease <ident…>` position, so adding it reserved nothing.
    src = ("service S { fn lease() -> Int }\n"
           "component C provides x: S {\n"
           "  provide x { fn lease() = 0 }\n}\n")
    prog = Parser(src, "<t>").parse()
    assert "lease" in prog.services[0].methods


def test_lease_undo_must_be_own_revoke():
    # a disposer naming anything but `<bind>.revoke()` is refused at lowering: the
    # own-requestId revoke is the only exempt disposer.
    src = ("service S { fn a() -> Int }\n"
           "component C provides x: S {\n"
           '  let l = effect lease fs.write(path="/tmp") ttl 1m undo l.dispose()\n'
           "  provide x { fn a() = 0 }\n}\n")
    with pytest.raises(RevlError, match="own revoke"):
        compile_source(src, "<t>")


# ---------------------------------------------------------------------------
# Ungated refusal: no silent mint
# ---------------------------------------------------------------------------

@needs_cordis
def test_ungated_run_refuses_the_lease():
    """A run with no approval policy REFUSES `effect lease` at load — it does not
    silently mint a grant. This is the self-mint consent bypass, closed."""
    session = Session()          # no approval policy
    with pytest.raises(SessionError, match="effect lease` refused"):
        session.load(_ir(), record=True)
    # nothing was minted
    assert session._grants == []


# ---------------------------------------------------------------------------
# Ticket-gated acquisition, mint from the ticket, LIFO teardown revoke
# ---------------------------------------------------------------------------

def _gated_session():
    s = Session()
    s.approval_policy = "auto"
    return s


@needs_cordis
def test_lease_raises_one_ticket_minted_from_the_ticket_hash(sink):
    """The gated run raises a lease ticket at load; the operator mints the grant
    from its hash; the reloaded body crosses under the lease promptless; teardown
    revokes the lease's own grant."""
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session()

    with pytest.raises(ApprovalRequired) as exc:
        session.load(_ir(), record=True)
    ticket = exc.value.ticket
    assert ticket["kind"] == "lease"
    assert ticket["capabilities"] == ['fs.write(path="/tmp")']
    assert ticket["leaseTtlMs"] == 600_000 and ticket["leaseUses"] == 3

    grant = session.mint_standing_grant(ticket_hash=ticket["hash"])
    assert grant["remainingUses"] == 3
    minted = session._grants[-1]
    assert minted["lease"] is True

    # reload: the lease gate now finds the live grant and boots (a fresh owner,
    # so its per-call prompt counter starts at zero)
    session.load(_ir(), record=True)
    assert session._owner.prompts["perCall"] == 0

    # the body crosses the leased boundary WITHOUT prompting (auto-approved
    # against the lease grant); the one consent was the lease acquisition, never
    # per crossing (the item-248 economics: the prompt moved, it did not vanish)
    session.call("ops", "go", [sink, "a"])
    session.call("ops", "go", [sink, "b"])
    assert _lines(sink) == ["w:a", "w:b"]
    assert session._owner.prompts["perCall"] == 0     # no per-crossing prompt
    assert session._grants_consumed == 2              # two lease uses spent

    # teardown: the disposer revokes the lease's OWN grant on the LIFO chain
    assert not minted["revoked"]
    session.unload()
    assert minted["revoked"] is True
    assert minted["consumed"] is True


@needs_cordis
def test_lease_uses_exhaust_then_prompt(sink):
    """A `uses 3` lease auto-approves three crossings, then the fourth prompts
    (the grant's shipped `remainingUses`, not a new counter)."""
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session()
    try:
        session.load(_ir(), record=True)
    except ApprovalRequired as exc:
        session.mint_standing_grant(ticket_hash=exc.ticket["hash"])
    session.load(_ir(), record=True)

    for msg in ("a", "b", "c"):
        session.call("ops", "go", [sink, msg])
    assert _lines(sink) == ["w:a", "w:b", "w:c"]
    with pytest.raises(ApprovalRequired):
        session.call("ops", "go", [sink, "d"])


@needs_cordis
def test_lease_ttl_lapses_at_the_crossing(sink):
    """A lease past its TTL prompts at the crossing (the injected clock, invariant
    3, checked at the crossing — reused verbatim from the standing grant)."""
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session()
    box = {"now": 1_000}
    session._clock_ms = lambda: box["now"]
    try:
        session.load(_ir(), record=True)
    except ApprovalRequired as exc:
        session.mint_standing_grant(ticket_hash=exc.ticket["hash"])
    session.load(_ir(), record=True)

    box["now"] = 5_000                       # inside the 10m window
    session.call("ops", "go", [sink, "a"])
    assert _lines(sink) == ["w:a"]

    box["now"] = 1_000 + 600_000 + 1         # one ms past expiresAt
    with pytest.raises(ApprovalRequired):
        session.call("ops", "go", [sink, "b"])
    assert _lines(sink) == ["w:a"]           # the expired lease fired nothing
