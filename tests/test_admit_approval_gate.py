"""The admitted turn is inside the approval gate, not outside it.

`Session.admit` WIDENS the callable surface. Before this was fixed it rebuilt
nothing: the turn's keys were immediately callable, `ClassMap.classify_call`
found no provider in the stale per-generation class map, and the per-call
decision read that `None` as "not a boundary call" — so a class-(c) crossing
that prompts when called directly fired through an admitted turn with no ticket,
no ledger entry, no grant and no posture counter. The item-329 untrusted-author
profile is not a mitigation: the turn declares no host code of its own, it only
forwards to a GRANTED provider whose emission is class (c).

Four exploits, each a test here:

  D1  the same crossing through an admitted turn must prompt;
  D2  a class-(c) crossing in the turn's ACTIVATION body is gated at WIRE time,
      before the body runs (there is no call to gate);
  D3  the public `revl.gate.Gate` facade with an approver that denies everything
      must CONSULT the approver and leave the sink empty;
  D4  an unresolved classification refuses instead of proceeding — including the
      ambiguous multi-realm key `ClassMap._provider_of` used to fold away.

Plus F6: an enabled policy opens the WAL, so the consume-before-fire spend the
gate documents as durable actually is.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the admit+run crossing is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh`",
)

# The running composition: one GRANTED tool whose only crossing is an immediate
# emission with no checked inverse — class (c), the posture that prompts.
_BASE = (
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

# The untrusted turn: NO host code of its own, it only forwards to `ops`.
_TURN_FORWARD = (
    "service Turn { emission fn run(sink: Str, msg: Str) }\n"
    "component TurnComp requires ops: Ops provides turn: Turn {\n"
    "  provide turn {\n"
    '    fn run(sink, msg) { emit ops.shout(sink, msg) }\n'
    "  }\n"
    "}\n"
)

# The sharper shape: the crossing sits in the turn's ACTIVATION body, so it
# fires the moment the turn is wired — no call is ever made.
_TURN_ACTIVATION = (
    "service Turn {{ emission fn run() }}\n"
    "component TurnComp requires ops: Ops provides turn: Turn {{\n"
    '  emit ops.shout("{sink}", "activation")\n'
    "  provide turn {{ fn run() {{ }} }}\n"
    "}}\n"
)


def _base_ir():
    from revl import compile_files
    from revl._paths import stdlib_root
    admit_path = str(stdlib_root() / "admit.rvl")
    base_abs = os.path.abspath("base.rvl")
    return compile_files([base_abs, admit_path], sources={base_abs: _BASE})


def _gated_session(tmp_path, wal=None):
    from revl.mcp.session import Session
    session = Session()
    session.approval_policy = "auto"
    session._wal_path = str(wal or (tmp_path / "session.wal"))
    session.load(copy.deepcopy(_base_ir()), record=True)
    return session


def _lines(sink):
    return Path(sink).read_text().splitlines() if os.path.exists(sink) else []


# --------------------------------------------------------------------------- #
# CONTROL — the granted tool called DIRECTLY prompts.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_control_direct_class_c_call_prompts(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    sink = str(tmp_path / "control.log")
    with pytest.raises(ApprovalRequired) as caught:
        session.call("ops", "shout", [sink, "control"])
    assert "announce" in caught.value.ticket["classCCapabilities"]
    assert _lines(sink) == [], "the crossing fired despite the ticket"
    assert session._owner.prompts["perCall"] == 1


# --------------------------------------------------------------------------- #
# D1 — the SAME crossing, routed through an admitted turn.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_d1_crossing_through_an_admitted_turn_prompts(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    sink = str(tmp_path / "d1.log")
    verdict = session.admit(_TURN_FORWARD, granted=["Ops"])
    assert verdict.admitted, verdict.message

    with pytest.raises(ApprovalRequired) as caught:
        session.call("turn", "run", [sink, "SEKRIT-CANARY-APV-turn"])

    # the ticket names the class-(c) capability the turn REACHES, not the turn.
    assert "announce" in caught.value.ticket["classCCapabilities"]
    assert _lines(sink) == [], "the class-(c) emission fired without approval"
    assert session._owner.prompts["perCall"] == 1, "the prompt was not counted"


@needs_cordis
def test_d1_admitted_turn_fires_once_the_ticket_is_approved(tmp_path):
    """The gate is a two-step, not a wall: approving the turn's ticket lets the
    identical retry consume the standing approval and fire exactly once."""
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    sink = str(tmp_path / "d1ok.log")
    assert session.admit(_TURN_FORWARD, granted=["Ops"]).admitted
    with pytest.raises(ApprovalRequired) as caught:
        session.call("turn", "run", [sink, "approved"])
    session.approve_ticket(caught.value.ticket["hash"])
    session.call("turn", "run", [sink, "approved"])
    assert _lines(sink) == ["announce:approved"]
    # single-use: the second identical call prompts again.
    with pytest.raises(ApprovalRequired):
        session.call("turn", "run", [sink, "approved"])
    assert _lines(sink) == ["announce:approved"]


@needs_cordis
def test_d1_turn_approval_is_bound_to_the_args_digest(tmp_path):
    """The negative guarantees hold on the newly gated surface too: a ticket
    approved for the turn's args does NOT cover a different call."""
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    sink = str(tmp_path / "digest.log")
    assert session.admit(_TURN_FORWARD, granted=["Ops"]).admitted
    with pytest.raises(ApprovalRequired) as first:
        session.call("turn", "run", [sink, "alpha"])
    session.approve_ticket(first.value.ticket["hash"])
    # a DIFFERENT argument vector is a different crossing: it prompts again.
    with pytest.raises(ApprovalRequired) as second:
        session.call("turn", "run", [sink, "beta"])
    assert second.value.ticket["hash"] != first.value.ticket["hash"]
    assert _lines(sink) == []
    # the approved one still fires exactly once.
    session.call("turn", "run", [sink, "alpha"])
    assert _lines(sink) == ["announce:alpha"]


@needs_cordis
def test_d1_class_map_spans_the_turn_after_wiring(tmp_path):
    """The mechanism, asserted directly: the per-generation class map is rebuilt
    over the widened surface, so the turn's key classifies."""
    session = _gated_session(tmp_path)
    assert session._class_map.classify_call("turn", "run") is None
    assert session.admit(_TURN_FORWARD, granted=["Ops"]).admitted
    reach = session._class_map.classify_call("turn", "run")
    assert reach is not None, "the class map was not rebuilt for the turn"
    assert reach["class"] == "c"
    assert reach["component"] == "TurnComp"


# --------------------------------------------------------------------------- #
# D2 — the crossing in the turn's ACTIVATION body, gated at WIRE time.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_d2_activation_body_crossing_is_gated_at_wire_time(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    sink = str(tmp_path / "d2.log")
    with pytest.raises(ApprovalRequired) as caught:
        session.admit(_TURN_ACTIVATION.format(sink=sink), granted=["Ops"])

    assert "announce" in caught.value.ticket["classCCapabilities"]
    assert _lines(sink) == [], "the activation body fired before any gate"
    assert session._owner.prompts["perCall"] == 1
    # refused BEFORE the plug: the running composition is untouched.
    assert "turn" not in session._driver._namespace()
    assert [c["name"] for c in session.ir["components"]].count("TurnComp") == 0


@needs_cordis
def test_d2_activation_body_wires_once_approved(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    sink = str(tmp_path / "d2ok.log")
    source = _TURN_ACTIVATION.format(sink=sink)
    with pytest.raises(ApprovalRequired) as caught:
        session.admit(source, granted=["Ops"])
    session.approve_ticket(caught.value.ticket["hash"])
    verdict = session.admit(source, granted=["Ops"])
    assert verdict.admitted, verdict.message
    assert _lines(sink) == ["announce:activation"]


@needs_cordis
def test_d2_does_not_re_prompt_for_the_already_booted_base(tmp_path):
    """The turn's activation gate is scoped to the turn. A base component whose
    own activation body already answered at load is not re-ticketed by an
    unrelated admit."""
    from revl.mcp.approval import ApprovalRequired
    from revl import compile_files
    from revl._paths import stdlib_root
    from revl.mcp.session import Session
    sink = str(tmp_path / "boot.log")
    base = _BASE + (
        "component Boot {\n"
        '  emit announce("%s", "boot")\n'
        "}\n" % sink)
    base_abs = os.path.abspath("base.rvl")
    ir = compile_files([base_abs, str(stdlib_root() / "admit.rvl")],
                       sources={base_abs: base})
    session = Session()
    session.approval_policy = "auto"
    session._wal_path = str(tmp_path / "s.wal")
    with pytest.raises(ApprovalRequired) as caught:
        session.load(copy.deepcopy(ir), record=True)
    session.approve_ticket(caught.value.ticket["hash"])
    session.load(copy.deepcopy(ir), record=True)
    assert _lines(sink) == ["announce:boot"]
    # the turn has no activation crossing of its own -> no new ticket.
    verdict = session.admit(_TURN_FORWARD, granted=["Ops"])
    assert verdict.admitted, verdict.message
    assert _lines(sink) == ["announce:boot"], "the base re-fired at admit"


# --------------------------------------------------------------------------- #
# D3 — the public `Gate` facade with a DENY-EVERYTHING approver.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_d3_gate_admit_consults_the_approver_and_nothing_fires(tmp_path):
    from revl._paths import stdlib_root
    from revl.gate import Gate, GateRefused
    consulted = []

    def deny(ticket):
        consulted.append(ticket)
        return False

    gate = Gate(approval_policy="auto", approver=deny,
                wal_path=str(tmp_path / "gate.wal"))
    try:
        gate.load({os.path.abspath("base.rvl"): _BASE,
                   str(stdlib_root() / "admit.rvl"): None})
        direct_sink = str(tmp_path / "direct.log")
        with pytest.raises(GateRefused):
            gate.call("ops", "shout", [direct_sink, "direct"])
        assert len(consulted) == 1
        assert _lines(direct_sink) == []

        result = gate.admit(_TURN_FORWARD, ["Ops"])
        assert result.admitted, result.message
        turn_sink = str(tmp_path / "turn.log")
        with pytest.raises(GateRefused):
            result.handle.call("turn", "run", [turn_sink, "via-admit"])
        # the operator's "no" was actually SOLICITED for the admitted turn.
        assert len(consulted) == 2, "the approver was never asked via admit"
        assert _lines(turn_sink) == [], "the crossing fired through the facade"
    finally:
        gate.close()


@needs_cordis
def test_d3_gate_admit_routes_an_activation_ticket_through_the_approver(tmp_path):
    from revl._paths import stdlib_root
    from revl.gate import Gate, GateRefused
    consulted = []

    def deny(ticket):
        consulted.append(ticket)
        return False

    sink = str(tmp_path / "act.log")
    gate = Gate(approval_policy="auto", approver=deny,
                wal_path=str(tmp_path / "gate2.wal"))
    try:
        gate.load({os.path.abspath("base.rvl"): _BASE,
                   str(stdlib_root() / "admit.rvl"): None})
        with pytest.raises(GateRefused):
            gate.admit(_TURN_ACTIVATION.format(sink=sink), ["Ops"])
        assert len(consulted) == 1
        assert _lines(sink) == []
    finally:
        gate.close()


# --------------------------------------------------------------------------- #
# D4 — an unresolved classification REFUSES; an ambiguous key is unresolved.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_d4_unresolved_classification_refuses(tmp_path, monkeypatch):
    """"I could not resolve this" is not "this is not a boundary call"."""
    from revl.mcp.session import SessionError
    session = _gated_session(tmp_path)
    sink = str(tmp_path / "unresolved.log")
    monkeypatch.setattr(session._class_map, "classify_call",
                        lambda key, method: None)
    with pytest.raises(SessionError) as caught:
        session.call("ops", "shout", [sink, "unresolved"])
    assert "unclassified" in str(caught.value)
    assert _lines(sink) == [], "an unclassifiable crossing fired"


def test_d4_provider_of_refuses_an_ambiguous_multi_realm_key():
    """`_provider_of` folded the per-(key, realm) map and returned the FIRST
    match. `classify_call('ops', 'go')` then answered for whichever provider won
    the dict order — reporting class none for a key whose other realm's provider
    is class (c). An ambiguous key has no single class: refuse."""
    from revl.mcp.approval import ClassMap
    from revl.query import SHARED_REALM

    ir = {"components": [], "manifest": {"components": []}}
    cmap = ClassMap.__new__(ClassMap)

    class _Index:
        provider_of = {("ops", "r1"): "Quiet", ("ops", "r2"): "Loud"}

    cmap.index = _Index()
    assert cmap._provider_of("ops") is None, "an ambiguous key picked a provider"
    assert cmap.classify_call("ops", "go") is None

    # one component across two realms is NOT ambiguous — it still resolves.
    _Index.provider_of = {("ops", "r1"): "Only", ("ops", "r2"): "Only"}
    assert cmap._provider_of("ops") == "Only"

    # a shared-realm provision resolves directly, exactly as before.
    _Index.provider_of = {("ops", SHARED_REALM): "Shared", ("ops", "r2"): "Other"}
    assert cmap._provider_of("ops") == "Shared"
    assert ir  # keep the fixture referenced


# --------------------------------------------------------------------------- #
# F6 — the durable spend is real: an enabled policy opens the WAL.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_f6_policy_session_opens_the_wal_and_records_granted_consumed(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    from revl.wal import read_wal
    wal_path = str(tmp_path / "approval.wal")
    session = _gated_session(tmp_path, wal=wal_path)
    # this composition carries NO typed-approval edge and no `requires approval`
    # rule — exactly the shape whose WAL used to stay closed.
    assert not session._typed_approval_active(session.ir)
    assert session.recorder.wal is not None, "the policy session opened no WAL"

    sink = str(tmp_path / "f6.log")
    for i in range(3):
        with pytest.raises(ApprovalRequired) as caught:
            session.call("ops", "shout", [sink, "m%d" % i])
        session.approve_ticket(caught.value.ticket["hash"])
        session.call("ops", "shout", [sink, "m%d" % i])

    records = read_wal(session.recorder.wal.path)["records"]
    granted = [r for r in records if r.get("record") == "approval-granted"]
    consumed = [r for r in records if r.get("record") == "approval-consumed"]
    assert len(granted) == 3, records
    assert len(consumed) == 3, records
    # the audit join: every consume names a request the WAL also granted.
    granted_ids = {r["requestId"] for r in granted}
    assert {r["requestId"] for r in consumed} == granted_ids
    assert _lines(sink) == ["announce:m0", "announce:m1", "announce:m2"]


@needs_cordis
def test_f6_no_policy_session_still_opens_no_wal(tmp_path):
    """Byte-identity for the ungated path: no policy, no WAL opened here."""
    from revl.mcp.session import Session
    session = Session()
    session.load(copy.deepcopy(_base_ir()), record=True)
    assert session.recorder.wal is None
