"""The auto-approve-unless-irreversible policy core — roadmap item 246, Slice 1.

Design: docs/design/246-auto-approve.md.

The policy reads the CHECKED effect class of a call and takes one of three
postures: class (a) witnessed-revertible auto-approves silently; class (b)
deferred auto-approves and enumerates at commit; class (c) an irreversible
emission with no checked inverse prompts per call via the hash-bound ticket
two-step. This suite proves the postures, the two bypasses the review sharpened
(the activation gate and the replay chokepoint), and byte-identity off-policy —
end to end through the live cordis-py runtime.

Reuses the 245 fixture: `stash` is class (a) (a witnessed rename), `enqueue` is
class (b) (a `deferred` emission), `shout` is class (c) (a plain emission).
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
    reason="the approval policy is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh` and run under its venv",
)

# (a) a per-call witnessed rename; (b) a deferred emission; (c) an immediate one.
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

_BASE = compile_source(_SOURCE, "approval_policy.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _session(policy="auto"):
    from revl.mcp.session import Session
    s = Session()
    s.approval_policy = policy
    return s


def _lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


def _mutated(path: str) -> bool:
    return not os.path.exists(path) and os.path.exists(path + ".bak")


@pytest.fixture
def files(tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"artifact_{i}.txt"
        p.write_text(f"deliverable {i}", encoding="utf-8")
        paths.append(str(p))
    return paths


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# ---------------------------------------------------------------------------
# The class map derives from the checked facts (no runtime needed)
# ---------------------------------------------------------------------------

def test_class_map_classifies_each_op_from_checked_facts():
    from revl.mcp.approval import ClassMap
    cm = ClassMap(_ir())
    assert cm.classify_call("ops", "stash")["class"] == "a"     # witnessed
    assert cm.classify_call("ops", "enqueue")["class"] == "b"   # deferred emission
    assert cm.classify_call("ops", "shout")["class"] == "c"     # plain emission
    assert cm.classify_call("ops", "pay")["class"] == "c"       # compensated == (c)


def test_class_map_classifies_activation_reach():
    # Fix 1: the map classifies the ACTIVATION body, not only provide-methods.
    from revl.mcp.approval import ClassMap
    src = (
        "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('announce:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        "component Boot provides ops: Ops {\n"
        "  emit announce(\"/tmp/x\", \"boot\")\n"   # class-(c) crossing in activation
        "  provide ops { fn ping() = 1 }\n"
        "}\n"
    )
    ir = compile_source(src, "boot.rvl")
    cm = ClassMap(ir)
    acts = {r["component"]: r["class"] for r in cm.activation_reaches()}
    assert acts.get("Boot") == "c"


# ---------------------------------------------------------------------------
# 1. a witnessed effect auto-approves silently
# ---------------------------------------------------------------------------

@needs_cordis
def test_witnessed_auto_approves_silently(files):
    session = _session()
    session.load(_ir(), record=True)
    out = session.call("ops", "stash", [files[0]])
    assert "result" in out and _mutated(files[0])
    owner = session._owner
    assert owner.prompts["perCall"] == 0
    assert owner.approvals == {"silent": 1, "atCommit": 0, "prompted": 0}


# ---------------------------------------------------------------------------
# 2. an all-(a)/(b) session ends perCall==0, 100% auto-approved
# ---------------------------------------------------------------------------

@needs_cordis
def test_all_ab_session_is_fully_auto_approved(files, sink):
    session = _session()
    session.load(_ir(), record=True)
    for i, path in enumerate(files):
        session.call("ops", "stash", [path])         # (a)
        session.call("ops", "enqueue", [sink, f"q{i}"])  # (b)
    owner = session._owner
    assert owner.prompts["perCall"] == 0
    assert owner.approvals == {"silent": 3, "atCommit": 3, "prompted": 0}
    assert owner.percent_auto_approved() == 100.0
    manifest = session.commit()
    result = session.commit_confirm(manifest["hash"])
    assert result["committed"]
    assert result["prompts"] == {"commit": 1, "perCall": 0, "residue": 0}


# ---------------------------------------------------------------------------
# 3. an irreversible emission prompts, then fires once post-approve
# ---------------------------------------------------------------------------

@needs_cordis
def test_immediate_emission_prompts_then_fires_once(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)

    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "shout", [sink, "hi"])
    ticket = exc.value.ticket
    assert _lines(sink) == []                       # the host body did NOT run
    assert session._owner.prompts["perCall"] == 1
    assert session._owner.approvals["prompted"] == 1

    # approve, then the IDENTICAL re-issue fires exactly once and consumes it
    session.approve_ticket(ticket["hash"])
    out = session.call("ops", "shout", [sink, "hi"])
    assert out["result"] is None
    assert _lines(sink) == ["announce:hi"]

    # a SECOND identical call is refused with a FRESH ticket (single-use)
    with pytest.raises(ApprovalRequired) as exc2:
        session.call("ops", "shout", [sink, "hi"])
    assert exc2.value.ticket["hash"] == ticket["hash"]  # same call, same hash
    assert _lines(sink) == ["announce:hi"]              # nothing new fired


# ---------------------------------------------------------------------------
# 4. a compensated emission still prompts (class (c) unchanged by 247)
# ---------------------------------------------------------------------------

@needs_cordis
def test_compensated_emission_still_prompts(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(ApprovalRequired):
        session.call("ops", "pay", [sink, "invoice"])
    assert _lines(sink) == []


# ---------------------------------------------------------------------------
# 12. the activation-body bypass is shut
# ---------------------------------------------------------------------------

_ACTIVATION_SOURCE = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops { fn ping() -> Int }\n"
    "component Quiet provides ops: Ops {\n"
    "  provide ops { fn ping() = 1 }\n"
    "}\n"
)


def _activation_emitter(sink: str) -> str:
    # the SAME emission, moved from a provide-method into the activation body.
    return (
        "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('announce:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        f"component Quiet provides ops: Ops {{\n"
        f"  emit announce(\"{sink}\", \"boot\")\n"
        "  provide ops { fn ping() = 1 }\n"
        "}\n"
    )


@needs_cordis
def test_activation_body_emission_cannot_dodge_the_prompt(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(compile_source(_ACTIVATION_SOURCE, "quiet.rvl"), record=True)

    candidate = compile_source(_activation_emitter(sink), "quiet2.rvl")
    with pytest.raises(ApprovalRequired) as exc:
        session.swap(candidate)
    # the swap did NOT boot: no activation effect ran
    assert _lines(sink) == []
    assert exc.value.ticket["kind"] == "activation"
    assert exc.value.ticket["component"] == "Quiet"

    # the approved re-issue boots and the activation emission fires exactly once
    session.approve_ticket(exc.value.ticket["hash"])
    session.swap(candidate)
    assert _lines(sink) == ["announce:boot"]


# ---------------------------------------------------------------------------
# 13. a replayed-forward class-(c) call is refused same as fresh
# ---------------------------------------------------------------------------

# a replayable fixture: `go` reaches `emit bus.send` (class (c)) AND records a
# Map effect step, so its call is on the forward-replay plan; `note` records a
# Map effect but reaches no crossing (class none), so it replays unhindered.
_REPLAY_SOURCE = """
service Bus { emission fn send(line: Str) }
service Ping { emission fn go(line: Str)  fn note(line: Str) }
component B provides bus: Bus {
  let out = effect Map.new() undo out.drop()
  provide bus { fn send(line) { effect out.insert(line, line) undo out.remove(line) } }
}
component P requires bus: Bus provides ping: Ping {
  let seen = effect Map.new() undo seen.drop()
  provide ping {
    fn go(line) {
      effect seen.insert(line, line) undo seen.remove(line)
      emit bus.send(line) compensate bus.send("compensated")
    }
    fn note(line) { effect seen.insert("n:" + line, line) undo seen.remove("n:" + line) }
  }
}
"""


@needs_cordis
def test_replay_forward_class_c_is_refused_like_a_fresh_call():
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(compile_source(_REPLAY_SOURCE, "replay.rvl"), record=True)

    # go is class (c): it prompts. approve once, it fires once.
    assert session._class_map.classify_call("ping", "go")["class"] == "c"
    assert session._class_map.classify_call("ping", "note")["class"] is None
    session.call("ping", "note", ["a"])              # class none: no prompt
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ping", "go", ["a"])
    session.approve_ticket(exc.value.ticket["hash"])
    session.call("ping", "go", ["a"])                # fires once, consumes approval

    # unwind the two calls (keeping the provision hinge at step 2), then replay
    # forward: the class-none `note` call replays unhindered, the class-(c) `go`
    # call is refused at the re-fired crossing with a ticket — the decision inside
    # Session.call sees it even though the tool is not revl_call and the standing
    # approval was single-use.
    session.step_back("P", 2, force=True)
    report = session.replay_forward("P", 2)
    outcomes = {(r["key"], r["method"]): r for r in report["replayed"]}
    assert "result" in outcomes[("ping", "note")]        # class none replays
    refused = outcomes[("ping", "go")]
    assert refused.get("approvalRequired") is True
    assert "ticket" in refused


# ---------------------------------------------------------------------------
# outstanding-ticket table refuses an unknown hash
# ---------------------------------------------------------------------------

@needs_cordis
def test_unknown_ticket_hash_is_refused(sink):
    from revl.mcp.session import SessionError
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(SessionError, match="unknown ticket hash"):
        session.approve_ticket("sha256:deadbeef")


# ---------------------------------------------------------------------------
# recording is required when the policy is enabled
# ---------------------------------------------------------------------------

@needs_cordis
def test_enabled_policy_requires_recording():
    from revl.mcp.session import SessionError
    session = _session()
    with pytest.raises(SessionError, match="requires recording"):
        session.load(_ir(), record=False)


# ---------------------------------------------------------------------------
# 10. no policy configured: byte-identical (no gate, no metrics surfaced)
# ---------------------------------------------------------------------------

@needs_cordis
def test_no_policy_is_byte_identical(sink, files):
    session = _session(policy=None)
    session.load(_ir(), record=True)
    # a class-(c) emission fires immediately, no ticket, no gate
    out = session.call("ops", "shout", [sink, "hi"])
    assert out["result"] is None
    assert _lines(sink) == ["announce:hi"]
    # no approval metrics surface off-policy
    assert "approval" not in session.state()
    assert session._class_map is None
