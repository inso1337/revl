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
# F5: one ticket mints AT MOST one approval (the double-approve over-mint)
#
# `approve_ticket` used to neither retire nor mark the outstanding ticket, so
# N calls against one still-outstanding ticket appended N unconsumed ledger
# entries: one human "yes" bought N crossings. The fix makes a duplicate
# `approve_ticket` call an idempotent no-op — it returns the SAME entry rather
# than minting a second, so the per-entry single-use guarantee
# (`_find_standing_approval`/`_consume_approval`) becomes a per-ticket
# guarantee too.
# ---------------------------------------------------------------------------

@needs_cordis
def test_double_approve_does_not_over_mint(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)

    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "shout", [sink, "hi"])
    ticket_hash = exc.value.ticket["hash"]

    # BEFORE the fix: two calls -> two unconsumed ledger entries for one
    # ticket ("ledger entries for one ticket after double-approve: 2").
    first = session.approve_ticket(ticket_hash)
    second = session.approve_ticket(ticket_hash)          # the duplicate mint
    assert first == second                                # idempotent no-op

    entries = [e for e in session._ledger if e["hash"] == ticket_hash]
    assert len(entries) == 1                              # AFTER the fix: 1
    assert entries[0]["consumed"] is False

    # fire 1: the one live entry fires and is consumed ("fire 1 ok")
    out = session.call("ops", "shout", [sink, "hi"])
    assert out["result"] is None
    assert _lines(sink) == ["announce:hi"]
    assert entries[0]["consumed"] is True

    # fire 2: BEFORE the fix, the over-minted second entry let this replay
    # through silently ("fire 2 FIRED -> ONE human yes bought TWO crossings").
    # AFTER the fix, no unconsumed entry remains, so it is refused with a
    # FRESH ticket exactly like any other unapproved class-(c) crossing.
    with pytest.raises(ApprovalRequired) as exc2:
        session.call("ops", "shout", [sink, "hi"])
    assert exc2.value.ticket["hash"] == ticket_hash
    assert _lines(sink) == ["announce:hi"]                # nothing new fired


@needs_cordis
def test_double_approve_before_a_swap_still_re_prompts_with_a_new_hash(sink):
    # false positive: the F5 idempotent-mint path must not interfere with
    # candidate-hash invalidation (F5 and the swap/replay chokepoint are
    # orthogonal — a duplicate mint on the OLD ticket must not somehow keep it
    # alive across a swap that moves the reach-closure candidate hash).
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)
    with pytest.raises(ApprovalRequired) as e1:
        session.call("ops", "shout", [sink, "hi"])
    session.approve_ticket(e1.value.ticket["hash"])
    session.approve_ticket(e1.value.ticket["hash"])       # duplicate mint

    swapped = _SOURCE.replace("emit announce(sink, msg)",
                              "emit announce(sink, \"swapped\")")
    session.swap(compile_source(swapped, "double_approve_swap.rvl"))
    with pytest.raises(ApprovalRequired) as e2:
        session.call("ops", "shout", [sink, "hi"])
    assert e2.value.ticket["hash"] != e1.value.ticket["hash"]
    assert e2.value.ticket["candidateHash"] != e1.value.ticket["candidateHash"]
    assert _lines(sink) == []                             # nothing fired


# ---------------------------------------------------------------------------
# F5, the other half: the SAME question, asked twice, is answerable twice.
#
# A ticket hash is the identity of a QUESTION — component, reach-closure
# candidate hash, kind, args digest — so the identical class-(c) crossing
# attempted a second time re-raises the SAME hash. Scoping the "one ticket mints
# at most one approval" rule to that hash for the life of the session made the
# second asking permanently unanswerable: `approve_ticket` reported the SPENT
# entry back as already-approved, minted nothing, and the re-issued call raised
# the same ticket forever. Every repeat-a-crossing shape in the lighthouse
# workload — a shell escape run twice, an edit re-applied, a session verb
# re-issued — dead-ended there.
#
# The unit is the answer ROUND: a new round opens only once the previous answer
# has been spent, so the duplicate-mint guarantee above is untouched and a
# repeated crossing stays approvable.
# ---------------------------------------------------------------------------

@needs_cordis
def test_the_same_crossing_can_be_approved_a_second_time(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)

    with pytest.raises(ApprovalRequired) as e1:
        session.call("ops", "shout", [sink, "hi"])
    session.approve_ticket(e1.value.ticket["hash"])
    session.call("ops", "shout", [sink, "hi"])
    assert _lines(sink) == ["announce:hi"]

    # the identical crossing again: same question, same hash, and a fresh yes
    # has to buy it. BEFORE the fix this `approve_ticket` was an idempotent
    # no-op against the already-spent entry and the re-issue below raised
    # `ApprovalRequired` again, with no way for any operator to get past it.
    with pytest.raises(ApprovalRequired) as e2:
        session.call("ops", "shout", [sink, "hi"])
    assert e2.value.ticket["hash"] == e1.value.ticket["hash"]
    session.approve_ticket(e2.value.ticket["hash"])
    session.call("ops", "shout", [sink, "hi"])
    assert _lines(sink) == ["announce:hi", "announce:hi"]

    # two yeses, two crossings, two DISTINCT ledger rows — so the audit join
    # (`approval-granted` -> `approval-consumed` -> emission, on `requestId`)
    # can still say which decision authorized which fire.
    entries = [e for e in session._ledger if e["hash"] == e1.value.ticket["hash"]]
    assert len(entries) == 2
    assert all(e["consumed"] for e in entries)
    assert len({e["requestId"] for e in entries}) == 2
    assert entries[0]["requestId"] == e1.value.ticket["hash"]   # round 1 unchanged


@needs_cordis
def test_duplicate_approves_never_over_mint_in_any_round(sink):
    # the guard on the fix above: re-opening a round must NOT reopen the
    # over-mint. In EVERY round, N duplicate `approve_ticket` calls still buy
    # exactly ONE crossing.
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(_ir(), record=True)

    for expected in (1, 2):
        with pytest.raises(ApprovalRequired) as exc:
            session.call("ops", "shout", [sink, "hi"])
        h = exc.value.ticket["hash"]
        first = session.approve_ticket(h)
        assert session.approve_ticket(h) == first     # idempotent within a round
        assert session.approve_ticket(h) == first
        live = [e for e in session._ledger
                if e["hash"] == h and not e["consumed"]]
        assert len(live) == 1                         # three yeses, one token
        session.call("ops", "shout", [sink, "hi"])
        assert _lines(sink) == ["announce:hi"] * expected

    # and the crossing after the last spend is still refused, not replayed
    with pytest.raises(ApprovalRequired):
        session.call("ops", "shout", [sink, "hi"])
    assert _lines(sink) == ["announce:hi", "announce:hi"]


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
# 12b. a MULTI-COMPONENT gated load converges in N prompts — driven end to end,
# the way an operator drives it.
#
# The gate walks every activation body in load order. It used to CONSUME each
# standing approval as it went, so a load that raised on the SECOND class-(c)
# activation had already spent the FIRST one on an attempt that never booted,
# and the retry re-raised the first component's ticket verbatim. Re-asking is
# self-similar, so the cost is not linear in N: the ask sequence is the ruler
# sequence and a clean N-body load cost 2**N - 1 prompts (measured: 1, 3, 7, 15
# for N = 1..4) where N would do. Item 204.
#
# The gate now reserves what covers each body and commits the whole set only
# once every body is covered, so an attempt that cannot boot spends nothing and
# each question is asked exactly ONCE. The counts asserted below are the
# regression test: they fail on the pre-fix behaviour, and they are what a
# downstream consumer asserting on exact prompt counts must be updated to.
#
# The revl suite had no test that drove a composition with more than one gated
# activation body through `load` to a boot, which is why a hash-scoped
# idempotency rule could turn this loop into a non-terminating one and stay
# green here while it broke every route-chain check downstream.
# ---------------------------------------------------------------------------

def _two_gated_activations(sink: str) -> str:
    return (
        "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('announce:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        "service Aux { fn pong() -> Int }\n"
        f"component First provides ops: Ops {{\n"
        f"  emit announce(\"{sink}\", \"first\")\n"
        "  provide ops { fn ping() = 1 }\n"
        "}\n"
        f"component Second provides aux: Aux {{\n"
        f"  emit announce(\"{sink}\", \"second\")\n"
        "  provide aux { fn pong() = 2 }\n"
        "}\n"
    )


@needs_cordis
def test_a_multi_component_gated_load_converges(sink):
    from revl.mcp.approval import ApprovalRequired
    ir = compile_source(_two_gated_activations(sink), "two_activations.rvl")
    session = _session()

    asked = []
    attempts = 0
    for _attempt in range(12):            # bounded: the bug made this unbounded
        try:
            attempts += 1
            session.load(ir, record=True)
            break
        except ApprovalRequired as exc:
            asked.append(exc.ticket["component"])
            session.approve_ticket(exc.ticket["hash"])
    else:                                  # pragma: no cover — the regression
        pytest.fail(f"the gated load never converged; asked={asked}")

    assert session.loaded
    # every gated activation body ran, exactly once each
    assert sorted(_lines(sink)) == ["announce:first", "announce:second"]
    # THE COUNT (item 204). N gated activation bodies cost exactly N prompts and
    # N + 1 `load` calls: each question is asked once, in walk order, and the
    # last attempt is the one that boots. Pre-fix this was `["First", "Second",
    # "First"]` over 4 attempts — 2**N - 1 prompts, because the attempt that
    # raised on `Second` had already eaten the yes given for `First`.
    assert asked == ["First", "Second"]
    assert len(asked) == 2                 # N, not 2**N - 1
    assert attempts == 3                   # N + 1


@needs_cordis
def test_a_gated_load_that_does_not_boot_spends_nothing(sink):
    """An activation-gate walk that refuses is all-or-nothing (item 204).

    The counts in 12b are a consequence of this: an attempt that cannot boot
    leaves every approval it matched UNSPENT, so the operator is never asked
    again for a question already answered. The complement — that a released
    reservation is not a leak — is asserted at the end: each yes is still spent
    exactly once, at the one crossing it authorized, and nothing survives the
    boot."""
    from revl.mcp.approval import ApprovalRequired
    ir = compile_source(_two_gated_activations(sink), "two_activations.rvl")
    session = _session()

    def consumed():
        return [e for e in session._ledger if e["consumed"]]

    # attempt 1 raises on `First`; nothing was covered, nothing was spent.
    with pytest.raises(ApprovalRequired) as first:
        session.load(ir, record=True)
    assert first.value.ticket["component"] == "First"
    assert consumed() == []
    session.approve_ticket(first.value.ticket["hash"])
    assert len(session._ledger) == 1

    # attempt 2 covers `First` from that yes and then refuses on `Second`. The
    # walk RESERVED First's approval and released it when the walk could not
    # clear, so it is still standing — this is the whole fix.
    with pytest.raises(ApprovalRequired) as second:
        session.load(ir, record=True)
    assert second.value.ticket["component"] == "Second"
    assert consumed() == [], "an attempt that never booted spent an approval"
    assert _lines(sink) == [], "nothing booted, so no activation body ran"
    session.approve_ticket(second.value.ticket["hash"])

    # attempt 3 covers both and boots, and NOW both are spent — once each. A
    # released reservation is un-spent, never double-spendable: two yeses, two
    # ledger entries, two consumptions, two crossings.
    session.load(ir, record=True)
    assert session.loaded
    assert sorted(_lines(sink)) == ["announce:first", "announce:second"]
    assert len(session._ledger) == 2
    assert len(consumed()) == 2

    # and nothing outlives the boot: re-running the same gate over the same
    # generation finds no standing authority and prompts again.
    with pytest.raises(ApprovalRequired):
        session._enforce_activation_gate(ir)


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


# ---------------------------------------------------------------------------
# 11. the spawn/instance seam does not evade the gate (item 246)
#
# The worst-class fold used to follow only the requires-wired service seam. An
# emission reached through a supervised spawn handle (`emit w.inner.charge(x)`)
# carries no `req` target, so it folded as class none at the caller and fired
# with no human approval prompt. In the untrusted-author model the gated party
# writes the composition, so it can deliberately route a granted-service
# emission through a spawned worker to evade the class-(c) prompt.
# ---------------------------------------------------------------------------

# a supervisor whose ONLY crossing is a class-(c) emission reached through a
# spawn handle (`emit w.inner.charge(...)`). The spawn is in the activation body
# and the handle is used from the provide-method, the shape phase-1 instance
# access supports at runtime (docs/design-v2-instances.md).
_SPAWN_ROUTED = (
    "extern emission fn charge(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('charge:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Inner { emission fn charge(sink: Str, msg: Str) }\n"
    "service Svc { emission fn serve(sink: Str, msg: Str) -> Int }\n"
    "component Worker provides inner: Inner {\n"
    "  provide inner { fn charge(sink, msg) { emit charge(sink, msg) return 1 } }\n"
    "}\n"
    "component C provides svc: Svc {\n"
    "  let w = effect spawn Worker with { } undo w.dispose()\n"
    "  provide svc { fn serve(sink, msg) { emit w.inner.charge(sink, msg) return 1 } }\n"
    "}\n"
)


def test_spawn_routed_emission_folds_to_class_c():
    # the class map must see the crossing through the spawn handle: without the
    # fix `classify_call` returns class none and the gate proceeds silently.
    from revl.mcp.approval import ClassMap
    cm = ClassMap(compile_source(_SPAWN_ROUTED, "spawn_routed.rvl"))
    reach = cm.classify_call("svc", "serve")
    assert reach is not None and reach["class"] == "c"
    assert "charge" in reach["capabilities"]


@needs_cordis
def test_spawn_routed_class_c_emission_prompts_and_holds_until_approved(sink):
    from revl.mcp.approval import ApprovalRequired
    session = _session()
    session.load(compile_source(_SPAWN_ROUTED, "spawn_routed.rvl"), record=True)

    with pytest.raises(ApprovalRequired) as exc:
        session.call("svc", "serve", [sink, "hi"])
    ticket = exc.value.ticket
    assert _lines(sink) == []                        # the host body did NOT run
    assert session._owner.prompts["perCall"] == 1
    assert session._owner.approvals["prompted"] == 1

    # approve, then the identical re-issue fires exactly once
    session.approve_ticket(ticket["hash"])
    session.call("svc", "serve", [sink, "hi"])
    assert _lines(sink) == ["charge:hi"]


def test_spawn_routed_emission_is_attributed_to_the_caller_audit():
    # the G8 boundary / `policy.component_reach` must attribute the spawned
    # emission's capability to the supervisor. The crossing routes a granted
    # `payment` service through the worker, so C reaches `payment`.
    from revl.__main__ import _boundary
    from revl import policy
    src = (
        "extern emission fn wire(x: Str) = @py { return }\n"
        "service Wire { emission fn send(x: Str) -> Int }\n"
        "service Inner { emission[payment] fn charge(x: Str) -> Int }\n"
        "service Svc { emission fn serve(x: Str) -> Int }\n"
        "component Worker requires payment: Wire provides inner: Inner {\n"
        "  provide inner { fn charge(x) { emit payment.send(x) return 1 } }\n"
        "}\n"
        "component C provides svc: Svc {\n"
        "  provide svc {\n"
        "    fn serve(x) {\n"
        "      let w = effect spawn Worker with { } undo w.dispose()\n"
        "      emit w.inner.charge(x)\n"
        "      return 1\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    ir = compile_source(src, "spawn_audit.rvl")
    reach = policy.component_reach({"boundary": _boundary(ir)}, "C")
    tokens = {(r.token, r.kind) for r in reach}
    assert ("payment", "emission") in tokens


def test_spawn_routed_class_a_worker_does_not_over_prompt():
    # a spawn whose provide-method reaches only class-(a) work must still fold to
    # (a): no spurious per-call prompt. Only a spawned class-(c) crossing raises
    # the caller to (c).
    from revl.mcp.approval import ClassMap
    src = (
        "type Stash = { path: Str, bak: Str }\n"
        "type FsError = { code: Str }\n"
        "extern pure fn unstash(w: Stash) -> Unit = @py { return }\n"
        "extern witnessed[fs] fn stash_path(p: Str)"
        " -> Result[Stash, FsError] undo unstash(result) = @py {\n"
        "    return Ok({'path': p, 'bak': p})\n"
        "}\n"
        "service Inner { emission fn touch(x: Str) }\n"
        "service Svc { emission fn serve(x: Str) -> Int }\n"
        "component Worker provides inner: Inner {\n"
        "  provide inner { fn touch(x) { effect stash_path(x) } }\n"
        "}\n"
        "component D provides svc: Svc {\n"
        "  let w = effect spawn Worker with { } undo w.dispose()\n"
        "  provide svc { fn serve(x) { emit w.inner.touch(x) return 1 } }\n"
        "}\n"
    )
    cm = ClassMap(compile_source(src, "spawn_safe.rvl"))
    assert cm.classify_call("svc", "serve")["class"] == "a"
