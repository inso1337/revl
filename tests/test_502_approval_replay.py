"""A single-use typed `Approval[C]` must not re-arm across a generation
boundary — roadmap item 246, Decision 3 (exit test 7, "non-replay").

Design: docs/design/246-auto-approve.md (Decision 3). The typed-approval surface
is single-use by construction: `await approval[C] { fields }` mints a token bound
to the component's reach-closure candidate hash, and the crossing spends it
(`consume_approval`), so a second crossing on the same token is refused.

The spend was recorded only on the generation's owner. `SessionOwner.grant_approval`
stores a COPY of each grant, and the frame check spends that copy, so the
session's own `_approval_grants` entry kept reading `consumed: False` forever.
Every generation install re-seeds the successor owner from `_approval_grants`
(`_configure_owner_approvals`), so the spent token came back as an unspent one:
ONE operator approval authorized an unbounded number of irreversible class-(c)
crossings — across `unload`/`load`, across `swap`, and across `rollback`.

The generation boundary is the only place the two records meet, so the fix
settles the spend there (`Session._settle_approval_spend`), from the owner that
actually recorded it.

This suite proves the replay is closed on all three boundaries and — the control
that makes the fix strictly narrower than the bug — that a never-spent approval
still fires, including one minted after a spent one.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from revl.compiler import compile_source

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the typed-approval runtime is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh`",
)


def _biller_src(sink, msg="hi", extra=False):
    """A Biller whose activation body crosses once, `with a`.

    `extra` adds an UNRELATED component, outside Biller's reach closure: the
    successor generation is a different composition while the candidate hash the
    token was minted against stays bit-identical (that is the case the hash
    binding cannot contain)."""
    tail = ("component Extra provides ex: Ops {\n"
            "  provide ex { fn ping() = 2 }\n"
            "}\n") if extra else ""
    return (
        "extern emission fn charge(sink: Str, msg: Str) requires approval = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('charge:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        "component Biller provides ops: Ops {\n"
        f"  let a = await approval[\"charge\"] {{ amount: 1 }}\n"
        f"  emit charge(\"{sink}\", \"{msg}\") with a\n"
        "  provide ops { fn ping() = 1 }\n"
        "}\n" + tail
    )


def _biller_quiet_src(sink):
    """A Biller with the SAME reach closure but no crossing at all — so a swap
    to it installs cleanly and leaves a generation to roll back from."""
    return (
        "extern emission fn charge(sink: Str, msg: Str) requires approval = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('charge:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        "component Biller provides ops: Ops {\n"
        "  provide ops { fn ping() = 1 }\n"
        "}\n"
    )


def _lines(sink):
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


def _component_state(session, name):
    for c in session.state()["components"]:
        if c["name"] == name:
            return c["state"]
    return None


def _grant(session, ir, capability="charge", component="Biller"):
    h = session._approval_candidate_hashes(ir)[component]
    return session.grant_language_approval(capability, component,
                                           fields={"amount": 1},
                                           candidate_hash=h)


def _tolerate(fn, *args, **kwargs):
    """Drive a generation boundary, tolerating the item-334 health gate.

    A refused crossing leaves the successor fiber FAILED, and the health gate
    then refuses to install that generation. That refusal is the fail-closed
    outcome we want; the SECURITY assertion is that no extra crossing fired, so
    the gate's rejection must not mask it."""
    from revl.mcp.session import SessionError
    try:
        return fn(*args, **kwargs)
    except SessionError as exc:
        return exc


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


@needs_cordis
def test_control_a_never_spent_grant_fires_exactly_once(sink):
    """The control: the fix must not disable typed approvals generally."""
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    _grant(session, ir)
    session.load(ir, record=True)
    assert _lines(sink) == ["charge:hi"]
    assert _component_state(session, "Biller") == "ACTIVE"
    assert session._owner.approval_ledger[0]["consumed"] is True


@needs_cordis
def test_spent_grant_does_not_rearm_across_unload_reload(sink):
    """The reported defect: unload, then reload with NO new grant."""
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    _grant(session, ir)
    session.load(ir, record=True)
    assert _lines(sink) == ["charge:hi"]
    session.unload()
    # the session's own record now agrees with the owner that spent it
    assert session._approval_grants[0]["consumed"] is True
    session.load(ir, record=True)
    assert _lines(sink) == ["charge:hi"]           # NOT ['charge:hi', 'charge:hi']
    assert _component_state(session, "Biller") == "FAILED"


@needs_cordis
def test_spent_grant_does_not_rearm_across_swap(sink):
    """A successor generation whose Biller closure is unchanged (the candidate
    hash still matches, so the hash binding does not contain this)."""
    from revl.mcp.session import Session
    gen1 = compile_source(_biller_src(sink), "biller1.rvl")
    gen2 = compile_source(_biller_src(sink, extra=True), "biller2.rvl")
    session = Session()
    _grant(session, gen1)
    session.load(gen1, record=True)
    assert _lines(sink) == ["charge:hi"]
    # the hash binding genuinely does not contain this: the closure is identical
    assert (session._approval_candidate_hashes(gen1)["Biller"]
            == session._approval_candidate_hashes(gen2)["Biller"])
    _tolerate(session.swap, gen2)
    assert _lines(sink) == ["charge:hi"]           # NOT ['charge:hi', 'charge:hi']


@needs_cordis
def test_spent_grant_does_not_rearm_across_rollback(sink):
    """Roll back to the generation that already spent the token."""
    from revl.mcp.session import Session
    gen1 = compile_source(_biller_src(sink, msg="one"), "biller1.rvl")
    quiet = compile_source(_biller_quiet_src(sink), "quiet.rvl")
    session = Session()
    _grant(session, gen1)
    session.load(gen1, record=True)
    assert _lines(sink) == ["charge:one"]
    session.swap(quiet)                            # installs cleanly, no crossing
    assert _lines(sink) == ["charge:one"]
    _tolerate(session.rollback)                    # rollback re-activates gen1
    assert _lines(sink) == ["charge:one"]          # NOT [..., 'charge:one']


@needs_cordis
def test_a_freshly_minted_grant_still_fires_after_a_spent_one(sink):
    """The strictly-narrower control: only the SPENT token is settled."""
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    _grant(session, ir)
    session.load(ir, record=True)
    assert _lines(sink) == ["charge:hi"]
    session.unload()
    _grant(session, ir)                            # a NEW, unspent token
    session.load(ir, record=True)
    assert _lines(sink) == ["charge:hi", "charge:hi"]
    assert _component_state(session, "Biller") == "ACTIVE"
