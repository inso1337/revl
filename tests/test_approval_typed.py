"""The typed-approval LANGUAGE surface — roadmap item 246, Slice 2 (H3).

Design: docs/design/246-auto-approve.md (Decision 3, Slice 2/3, exit tests
5-9, 14-18). Slice 1 landed the operator-layer policy core (the class map, the
per-call ticket two-step, the ledger seam, the WAL records). Slice 2 adds the
POLICY-owned requirement (`capability C requires approval [ttl D]`), its
admission refusal, and the language surface (`Approval[C]`, `await approval[C]
{ fields }`, `emit … with a`) with the durable consume-before-fire runtime
binding and the five invariants.

This suite proves the deferred exit tests the design owed:

  * 5  hash-binding (drifted args, unknown hash, previous-generation ticket);
  * 6  candidate-invalidates (a swap changes the reach-closure hash);
  * 7  expiry (clock injected) and non-replay (deputy, single-use, cross-session);
  * 8  unreachable-without, static (admission refusal + declaration lowering);
  * 9  operator composition (the `approve` verb, subject-scoped);
  * 14 crash-cut WAL pair (consume-before-fire, joined on requestId);
  * 15 unreachable-without, runtime half (the frame check, no static run);
  * 16 non-persistence (checker refusal + the runtime session binding);
  * 17 the `*` row (unnameable reach is never approvable);
  * 18 recording required (the policy over a non-recording session refuses).
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the typed-approval runtime is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh`",
)

# The Slice-1 fixture: (a) witnessed rename, (b) deferred emission, (c) plain.
_SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py { return }\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
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

_BASE = compile_source(_SOURCE, "typed_approval.rvl")


def _ir():
    return copy.deepcopy(_BASE)


def _session(policy="auto", sandbox=None):
    from revl.mcp.session import Session
    s = Session()
    s.approval_policy = policy
    if sandbox is not None:
        from revl.policy import parse_policy
        s.sandbox = parse_policy(sandbox)
    return s


def _lines(sink):
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# ---------------------------------------------------------------------------
# 5. hash-binding
# ---------------------------------------------------------------------------

@needs_cordis
def test_hash_binding_drift_and_unknown_hash(sink):
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import SessionError
    session = _session()
    session.load(_ir(), record=True)

    with pytest.raises(ApprovalRequired) as e1:
        session.call("ops", "shout", [sink, "a"])
    session.approve_ticket(e1.value.ticket["hash"])
    # the identical call fires; a DRIFTED-args call recomputes a DIFFERENT hash
    session.call("ops", "shout", [sink, "a"])
    with pytest.raises(ApprovalRequired) as e2:
        session.call("ops", "shout", [sink, "b"])
    assert e2.value.ticket["hash"] != e1.value.ticket["hash"]

    # `revl_approve` on a hash the server never issued is refused
    with pytest.raises(SessionError, match="unknown ticket hash"):
        session.approve_ticket("sha256:deadbeef")


# ---------------------------------------------------------------------------
# 6. candidate-invalidates (a swap changes the reach-closure hash)
# ---------------------------------------------------------------------------

_SWAPPED = _SOURCE.replace("emit announce(sink, msg)",
                           "emit announce(sink, \"swapped\")")


@needs_cordis
def test_swap_invalidates_a_standing_approval(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(ApprovalRequired) as e1:
        session.call("ops", "shout", [sink, "hi"])
    session.approve_ticket(e1.value.ticket["hash"])
    # swap in a semantically-changed Agent: the reach-closure candidate hash
    # moves, so the standing approval no longer covers the re-issued call.
    session.swap(compile_source(_SWAPPED, "typed_approval.rvl"))
    with pytest.raises(ApprovalRequired) as e2:
        session.call("ops", "shout", [sink, "hi"])
    assert e2.value.ticket["candidateHash"] != e1.value.ticket["candidateHash"]
    assert _lines(sink) == []   # nothing fired on the stale approval


# ---------------------------------------------------------------------------
# 8. unreachable-without, static half
# ---------------------------------------------------------------------------

_DECL_NO_EDGE = (
    "extern emission fn charge(sink: Str, msg: Str) requires approval = @py "
    "{ return }\n"
    "service Ops { fn ping() -> Int }\n"
    "component Biller provides ops: Ops {\n"
    "  emit charge(\"s\", \"m\")\n"
    "  provide ops { fn ping() = 1 }\n"
    "}\n"
)

_DECL_WITH_EDGE = (
    "extern emission fn charge(sink: Str, msg: Str) requires approval = @py "
    "{ return }\n"
    "service Ops { fn ping() -> Int }\n"
    "component Biller provides ops: Ops {\n"
    "  let a = await approval[\"charge\"] { amount: 1 }\n"
    "  emit charge(\"s\", \"m\") with a\n"
    "  provide ops { fn ping() = 1 }\n"
    "}\n"
)


def test_declaration_owned_requirement_refuses_at_lowering():
    with pytest.raises(RevlError, match="requires approval"):
        compile_source(_DECL_NO_EDGE, "biller.rvl")
    # the covering edge admits it
    compile_source(_DECL_WITH_EDGE, "biller.rvl")


# a policy-required capability, reached with no `with` edge (no declaration).
_POLICY_NO_EDGE = (
    "extern emission fn announce(sink: Str, msg: Str) = @py { return }\n"
    "service Ops { fn ping() -> Int }\n"
    "component Notifier provides ops: Ops {\n"
    "  emit announce(\"s\", \"m\")\n"
    "  provide ops { fn ping() = 1 }\n"
    "}\n"
)


def test_policy_owned_requirement_refuses_at_admission():
    from revl.mcp.session import Session, SessionError
    from revl.policy import parse_policy
    ir = compile_source(_POLICY_NO_EDGE, "notifier.rvl")
    session = Session()
    session.sandbox = parse_policy("capability announce requires approval")
    with pytest.raises(SessionError, match="requires approval"):
        session.load(ir, record=True)


# ---------------------------------------------------------------------------
# 9. operator composition — the `approve` verb, subject-scoped
# ---------------------------------------------------------------------------

_REALMED = (
    "service Pay { fn ping() -> Int }\n"
    "component Payments provides pay: Pay {\n"
    "  isolate pay in realm(\"payments\")\n"
    "  provide pay { fn ping() = 1 }\n"
    "}\n"
    "component Other provides other: Pay {\n"
    "  isolate other in realm(\"other\")\n"
    "  provide other { fn ping() = 1 }\n"
    "}\n"
)


class _FakeApproveSession:
    def __init__(self, ir, operator, tickets):
        self.ir = ir
        self.operator = operator
        self._tickets = tickets


def test_operator_approve_verb_is_scoped():
    from revl.mcp.operator import decide, parse_profile
    ir = compile_source(_REALMED, "realmed.rvl")
    tickets = {
        "sha256:pay": {"component": "Payments", "candidateHash": "sha256:x"},
        "sha256:oth": {"component": "Other", "candidateHash": "sha256:y"},
    }

    # an operator with NO `approve` grant cannot mint an approval
    caller = parse_profile("operator caller may swap on *").get("caller")
    d = decide(_FakeApproveSession(ir, caller, tickets), "revl_approve",
               {"hash": "sha256:pay"})
    assert d.gated and d.allowed is False

    # a subject-scoped `may approve on payments` mints for a payments ticket
    human = parse_profile("operator human may approve on payments").get("human")
    ok = decide(_FakeApproveSession(ir, human, tickets), "revl_approve",
                {"hash": "sha256:pay"})
    assert ok.allowed is True
    # …and is refused for a ticket whose component sits outside the grant
    no = decide(_FakeApproveSession(ir, human, tickets), "revl_approve",
                {"hash": "sha256:oth"})
    assert no.allowed is False


# ---------------------------------------------------------------------------
# the language runtime binding — a `with a` crossing in the activation body
# ---------------------------------------------------------------------------

def _biller_src(sink, msg="hi", twice=False):
    second = f"  emit charge(\"{sink}\", \"{msg}\") with a\n" if twice else ""
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
        f"{second}"
        "  provide ops { fn ping() = 1 }\n"
        "}\n"
    )


def _component_state(session, name):
    for c in session.state()["components"]:
        if c["name"] == name:
            return c["state"]
    return None


# ---------------------------------------------------------------------------
# 15. unreachable-without, runtime half — the frame check, no token
# ---------------------------------------------------------------------------

@needs_cordis
def test_runtime_frame_check_refuses_a_crossing_with_no_token(sink):
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()             # no operator class-map gate, typed only
    session.load(ir, record=True)  # NO grant minted
    # the crossing is refused AT THE CROSSING: the component fails to activate
    # and the host body never runs.
    assert _component_state(session, "Biller") == "FAILED"
    assert _lines(sink) == []


@needs_cordis
def test_granted_crossing_fires_once_and_consumes(sink):
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    h = session._approval_candidate_hashes(ir)["Biller"]
    session.grant_language_approval("charge", "Biller", fields={"amount": 1},
                                    candidate_hash=h)
    session.load(ir, record=True)
    assert _lines(sink) == ["charge:hi"]
    assert session._owner.approval_ledger[0]["consumed"] is True


# ---------------------------------------------------------------------------
# 7. expiry (clock injected) and non-replay (single-use, deputy, cross-session)
# ---------------------------------------------------------------------------

@needs_cordis
def test_expiry_refuses_an_aged_out_token(sink):
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    session.sandbox = None
    clock = {"t": 1000}
    session._clock_ms = lambda: clock["t"]
    h = session._approval_candidate_hashes(ir)["Biller"]
    session.grant_language_approval("charge", "Biller", ttl_ms=100,
                                    candidate_hash=h)
    clock["t"] = 5000                        # well past grantedAt + ttl
    session.load(ir, record=True)
    assert _component_state(session, "Biller") == "FAILED"  # expired at crossing
    assert _lines(sink) == []


@needs_cordis
def test_single_use_refuses_the_second_crossing(sink):
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink, twice=True), "biller.rvl")
    session = Session()
    h = session._approval_candidate_hashes(ir)["Biller"]
    session.grant_language_approval("charge", "Biller", candidate_hash=h)
    session.load(ir, record=True)
    # first crossing fired once, the second (same token) is refused -> FAILED
    assert _lines(sink) == ["charge:hi"]
    assert _component_state(session, "Biller") == "FAILED"


@needs_cordis
def test_deputy_component_token_is_refused(sink):
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    h = session._approval_candidate_hashes(ir)["Biller"]
    # a token minted for another component name does not cover Biller's crossing
    session.grant_language_approval("charge", "SomeoneElse", candidate_hash=h)
    session.load(ir, record=True)
    assert _component_state(session, "Biller") == "FAILED"
    assert _lines(sink) == []


@needs_cordis
def test_cross_session_token_is_refused(sink):
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    h = session._approval_candidate_hashes(ir)["Biller"]
    grant = session.grant_language_approval("charge", "Biller",
                                            candidate_hash=h)
    # forge the grant into a DIFFERENT session over the same composition/WAL:
    # the session binding refuses it (invariant 5, cross-session replay).
    grant["session"] = "some-other-session"
    grant["consumed"] = False
    other = Session()
    other._approval_grants = [grant]
    other.load(ir, record=True)
    assert _component_state(other, "Biller") == "FAILED"
    assert _lines(sink) == []


# ---------------------------------------------------------------------------
# 14. crash-cut WAL pair — consume-before-fire, joined on requestId
# ---------------------------------------------------------------------------

@needs_cordis
def test_consume_before_fire_wal_ordering(sink, tmp_path):
    from revl.mcp.session import Session
    ir = compile_source(_biller_src(sink), "biller.rvl")
    session = Session()
    h = session._approval_candidate_hashes(ir)["Biller"]
    grant = session.grant_language_approval("charge", "Biller",
                                            candidate_hash=h)
    rid = grant["requestId"]
    session.load(ir, record=True)
    wal_path = session.recorder.wal.path
    session.unload()   # flush + close the WAL
    # read the WAL: the durable spend precedes the emission, both on requestId,
    # so no cut position exists where the token is valid while the emission is
    # out — a cut BEFORE the emission leaves consumed-but-unfired (owed).
    records = [__import__("json").loads(line)
               for line in Path(wal_path).read_text().splitlines()]
    consumed = [i for i, r in enumerate(records)
                if r.get("record") == "approval-consumed"
                and r.get("requestId") == rid]
    emitted = [i for i, r in enumerate(records)
               if r.get("record") == "approval-emission"
               and r.get("requestId") == rid]
    assert consumed and emitted
    assert consumed[0] < emitted[0]   # consume-before-fire, durably


# ---------------------------------------------------------------------------
# 16. non-persistence — the checker refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src", [
    "type R = { a: Approval[charge] }",                         # a record field
    "fn f(a: Approval[charge]) -> Unit { return }",             # a signature
    "service S { fn f(a: Approval[charge]) }",                  # a service method
    "service Kv { fn get(k: Str) -> Str }\n"
    "component C provides kv: Kv {\n"
    "  handoff kv: Approval[charge]\n"
    "  provide kv { fn get(k) { return k } }\n"
    "}\n",                                                       # a handoff shape
])
def test_approval_is_non_persistent(src):
    with pytest.raises(RevlError, match="Approval"):
        compile_source(src, "np.rvl")


# ---------------------------------------------------------------------------
# 17. the `*` row — an unnameable reach is never approvable
# ---------------------------------------------------------------------------

def test_star_is_class_c_and_never_approvable():
    from revl.mcp.approval import ClassMap
    # a bare `emission` service op reaches the unnameable boundary: class (c).
    star = (
        "service Raw { emission fn go(x: Str) }\n"
        "component Wild provides raw: Raw {\n"
        "  provide raw { fn go(x) { emit go_ext(x) } }\n"
        "}\n"
        "extern emission fn go_ext(x: Str) = @py { return }\n"
    )
    ir = compile_source(star, "star.rvl")
    assert ClassMap(ir).classify_call("raw", "go")["class"] == "c"
    # no approval shape can name `*`: neither the type nor the await form parses.
    for bad in ("fn f(a: Approval[\"*\"]) -> Unit { return }",
                "component C provides o: S {\n"
                "  let a = await approval[\"*\"] { }\n"
                "  provide o { fn ping() = 1 }\n"
                "}\nservice S { fn ping() -> Int }"):
        with pytest.raises(RevlError, match=r"\*"):
            compile_source(bad, "star2.rvl")


# ---------------------------------------------------------------------------
# 18. recording required
# ---------------------------------------------------------------------------

def test_policy_with_requires_approval_needs_recording():
    from revl.mcp.session import Session, SessionError
    from revl.policy import parse_policy
    ir = compile_source(_DECL_WITH_EDGE, "biller.rvl")
    session = Session()
    session.sandbox = parse_policy("capability charge requires approval")
    with pytest.raises(SessionError, match="requires recording"):
        session.load(ir, record=False)
