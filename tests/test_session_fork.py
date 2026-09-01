"""Session branching — roadmap item 250, Slice 1.

Design: docs/design/250-session-branching.md (the REVISED design that survived an
independent adversarial review).

A fork puts the workspace ACTUALLY in the step-k state and hands out a branch to
explore an alternative. The word "actually" is the whole design: the reversible
part of the tail above k is put back and truthfully is; the irreversible part is
enumerated and never pretended away. Two axes the review corrected, both pinned
here:

  * the non-emitting rewind is SCOPE-gated, not KIND-gated (CRITICAL 2): a
    host-confined `witnessed[fs]` inverse RUNS; an outbound `witnessed[net]` undo
    is enumerated in `wouldCrossOnRewind` and NEVER fired, forcing clean=false;
  * the partition is TOTAL over all seven kinds (HIGH 2): a `KIND_OPAQUE` tail is
    refused, `KIND_BOUNDARY`/`KIND_HINGE` are provably empty, an orphan
    compensation lands in `wouldCrossOnRewind`.

The timeline-level tests drive the classifier directly (no runtime needed); the
session-level tests (`@needs_cordis`) prove the parent freeze (HIGH 1), a clean
host-confined fork, and that recover retires a frozen parent at k.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import replay  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session fork is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh` and run under its venv",
)


# ---------------------------------------------------------------------------
# timeline builders (no runtime): a Step with an explicit kind/scope/inverse
# ---------------------------------------------------------------------------


def _mk(tl, kind, label, *, scope=None, detail=None, undo=None,
        compensation=None, undo_idempotent=None):
    step = replay.Step(len(tl.steps), kind, label, None, {"phase": "activation"},
                       detail=detail)
    step.scope = scope
    step.compensation = compensation
    step.undo_idempotent = undo_idempotent
    if undo is not None:
        step.undo = replay._once(step, undo)
    tl.steps.append(step)
    return step


def _run(coro):
    import asyncio  # noqa: PLC0415
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# CRITICAL 2 — the rewind is SCOPE-gated, not KIND-gated
# ---------------------------------------------------------------------------


def test_host_confined_fs_inverse_runs_outbound_net_inverse_is_enumerated():
    """The headline correctness point. Two `KIND_EFFECT` witnessed inverses in
    the span: one whose declared scope is `witnessed[fs]` (host-confined) and one
    whose scope is outbound (`witnessed[net]`, item 254's `undo` = 'PUT the
    preimage back'). The fs inverse RUNS and lands in `inversesRan`; the net
    inverse is NOT run — it is enumerated in `wouldCrossOnRewind` — and its scope
    being outbound is what keeps the rewind from PUT-ing to a remote endpoint
    mid-fork."""
    ran = {"fs": False, "net": False}
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", scope={"caps": ["fs"]},
        undo=lambda: ran.__setitem__("fs", True))
    _mk(tl, replay.KIND_EFFECT, "net.put", scope={"caps": ["net"]},
        undo=lambda: ran.__setitem__("net", True))

    report = _run(tl.step_back(-1, compensate=False))

    # the fs inverse ran (host-confined); the net inverse did NOT
    assert ran == {"fs": True, "net": False}
    assert [e["index"] for e in report["inversesRan"]] == [0]
    assert [e["index"] for e in report["wouldCrossOnRewind"]] == [1]
    assert report["wouldCrossOnRewind"][0]["scope"] == {"caps": ["net"]}
    # the net step's inverse was never marked run
    assert tl.steps[0].undone is True
    assert tl.steps[1].undone is False


def test_outbound_scoped_inverse_forces_clean_false_in_the_fork_report():
    """Because `wouldCrossOnRewind` is non-empty, the fork report can never claim
    a clean rewind while an outbound-scoped inverse was skipped (Decision 2/3)."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", scope={"caps": ["fs"]}, undo=lambda: None)
    _mk(tl, replay.KIND_EFFECT, "net.put", scope={"caps": ["net"]}, undo=lambda: None)
    part = tl.partition_tail(-1)
    would = part["wouldCrossOnRewind"]
    assert len(would) == 1 and would[0]["index"] == 1
    clean = not (part["emissionsCrossed"] or part["emissionsCompensated"]
                 or would or part["unrestored"])
    assert clean is False


def test_unknown_capability_token_reads_as_crossing():
    """Fail-safe: an inverse whose scope names a token that is not provably
    host-confined is enumerated, never run (the honest direction)."""
    assert replay.scope_host_confined({"caps": ["mail"]}) is False
    assert replay.scope_host_confined({"caps": ["fs", "net"]}) is False
    assert replay.scope_host_confined({"confined": True}) is True
    assert replay.scope_host_confined({"sandbox": True}) is True
    assert replay.scope_host_confined(None) is True


# ---------------------------------------------------------------------------
# HIGH 2 — the partition is TOTAL over all seven kinds
# ---------------------------------------------------------------------------


def test_boundary_and_hinge_are_provably_empty():
    """`KIND_BOUNDARY` and `KIND_HINGE` carry no undo and cross no boundary, so
    they contribute to NO bucket — not residue, not restored (Decision 3)."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_BOUNDARY, "iter/boundary")
    _mk(tl, replay.KIND_HINGE, "adopt/drain")
    part = tl.partition_tail(-1)
    for bucket in ("inversesRan", "provisionsWithdrawn", "emissionsCrossed",
                   "emissionsCompensated", "wouldCrossOnRewind", "unrestored"):
        assert part[bucket] == [], bucket


def test_opaque_step_lands_in_unrestored_with_its_repr():
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_OPAQUE, "buf.opaque", detail={"repr": "<Buffer 4 bytes>"})
    part = tl.partition_tail(-1)
    assert part["unrestored"] == [
        {"index": 0, "kind": "opaque", "label": "buf.opaque",
         "repr": "<Buffer 4 bytes>"}]
    # and it is surfaced as a fork hazard (Slice 1 refuses such a fork)
    assert tl.fork_hazards(-1)["opaque"][0]["index"] == 0


def test_orphan_compensation_lands_in_would_cross_with_its_referent():
    """A compensation whose referent emission lies BELOW k has no
    `emissionsCompensated` entry to attach to, so it lands in `wouldCrossOnRewind`
    as an orphan carrying `for` — it can never fall through the report."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EMISSION, "notify.ping", detail={"key": "notify"})  # index 0, below k
    _mk(tl, replay.KIND_COMPENSATION, "compensate notify.ping",
        detail={"for": 0}, undo=lambda: None)                               # index 1, in tail
    part = tl.partition_tail(0)   # k = 0: emission 0 is below k, compensation 1 is the tail
    would = part["wouldCrossOnRewind"]
    assert len(would) == 1
    assert would[0]["index"] == 1 and would[0]["for"] == 0
    # the emission below k is NOT in the tail at all
    assert part["emissionsCrossed"] == [] and part["emissionsCompensated"] == []


def test_every_kind_lands_in_exactly_one_bucket_or_empty():
    """Totality: one step of every kind, each assigned to exactly one bucket
    (BOUNDARY/HINGE assigned to none, by design)."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EMISSION, "e.bare")                                  # crossed
    _mk(tl, replay.KIND_EFFECT, "fs", scope={"caps": ["fs"]}, undo=lambda: None)  # rewound
    _mk(tl, replay.KIND_PROVISION, "p", undo=lambda: None)                   # withdrawn
    _mk(tl, replay.KIND_COMPENSATION, "c", detail={"for": 0}, undo=lambda: None)  # would-cross
    _mk(tl, replay.KIND_BOUNDARY, "b")                                       # empty
    _mk(tl, replay.KIND_HINGE, "h")                                          # empty
    _mk(tl, replay.KIND_OPAQUE, "o", detail={"repr": "x"})                   # unrestored
    part = tl.partition_tail(-1)
    counts = {k: len(part[k]) for k in (
        "inversesRan", "provisionsWithdrawn", "emissionsCrossed",
        "emissionsCompensated", "wouldCrossOnRewind", "unrestored")}
    assert counts == {"inversesRan": 1, "provisionsWithdrawn": 1,
                      "emissionsCrossed": 1, "emissionsCompensated": 0,
                      "wouldCrossOnRewind": 1, "unrestored": 1}
    # 7 kinds recorded, 5 accounted in a bucket, 2 (boundary/hinge) provably empty
    assert len(tl.steps) == 7
    assert sum(counts.values()) == 5


# ---------------------------------------------------------------------------
# MEDIUM — pin the compensate=False report shape against a golden
# ---------------------------------------------------------------------------


def _golden_timeline() -> replay.Timeline:
    """A fixed timeline with no file/lineno (site=None), so the compensate=False
    report is fully deterministic and comparable byte-for-byte to the golden."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EMISSION, "bare.send",
        detail={"key": "bare", "method": "send", "service": "Bus", "args": ["x"]})
    _mk(tl, replay.KIND_EMISSION, "notify.ping",
        detail={"key": "notify", "method": "ping", "service": "Ping",
                "args": ["y"]}, compensation=2)
    _mk(tl, replay.KIND_COMPENSATION, "compensate notify.ping",
        detail={"for": 1}, undo=lambda: None)
    _mk(tl, replay.KIND_EFFECT, "fs.write", scope={"caps": ["fs"]}, undo=lambda: None)
    _mk(tl, replay.KIND_EFFECT, "net.put", scope={"caps": ["net"]}, undo=lambda: None)
    _mk(tl, replay.KIND_PROVISION, "provide store", undo=lambda: None)
    _mk(tl, replay.KIND_BOUNDARY, "iter/boundary")
    _mk(tl, replay.KIND_HINGE, "adopt/drain")
    _mk(tl, replay.KIND_OPAQUE, "buf.opaque", detail={"repr": "<Buffer 4 bytes>"})
    return tl


def test_compensate_false_report_matches_the_golden():
    golden = json.loads((_BACKEND / "golden"
                         / "fork_report_compensate_false.json").read_text())
    report = _run(_golden_timeline().step_back(-1, compensate=False))
    assert report == golden


# ---------------------------------------------------------------------------
# the default (compensate=True) rewind is BYTE-IDENTICAL for existing callers
# ---------------------------------------------------------------------------


def test_default_step_back_is_the_legacy_shape():
    """`compensate=True` (the default) is the pre-250 unwind: it carries the
    legacy keys and NONE of the new fork fields, so every existing caller and
    golden is untouched."""
    tl = _golden_timeline()
    report = _run(tl.step_back(-1, force=True))    # default compensate=True
    assert set(report) >= {"component", "to", "toLabel", "inversesRan",
                           "compensationsRan", "emissionsCompensated",
                           "emissionsCrossed", "failed", "live", "guarantee"}
    # the new fork fields never appear on the default path
    assert "wouldCrossOnRewind" not in report
    assert "unrestored" not in report
    assert "compensate" not in report
    # the default path RAN the compensation (the pre-250 behaviour); the scoped
    # path would have enumerated it instead — this is the axis the review moved
    assert len(report["compensationsRan"]) == 1


def test_default_and_scoped_diverge_on_the_compensation():
    """Same span, two modes: the default runs the compensation, the fork mode
    enumerates it in `wouldCrossOnRewind` and never fires it."""
    default = _run(_golden_timeline().step_back(-1, force=True))
    scoped = _run(_golden_timeline().step_back(-1, compensate=False))
    assert len(default["compensationsRan"]) == 1
    assert [e["index"] for e in scoped["wouldCrossOnRewind"]] == [4, 2]


# ===========================================================================
# session-level: parent freeze (HIGH 1), a clean fork, recover retires parent
# ===========================================================================

# A component with a provide-method BRACKET fs effect (records a KIND_EFFECT
# timeline step, in-process/host-confined inverse) and a `deferred` emission
# (a class-(b) send the branch holds until its own commit).
_SOURCE = (
    "extern pure fn fs_write(p: Str, data: Str) -> Unit = @py {\n"
    "    with open(p, 'w') as _f:\n"
    "        _f.write(data)\n"
    "    return\n"
    "}\n"
    "extern pure fn fs_del(p: Str) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(p):\n"
    "        os.remove(p)\n"
    "    return\n"
    "}\n"
    "extern emission deferred fn deliver(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('deliver:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn write(p: Str, data: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn write(p, data) { effect fs_write(p, data) undo fs_del(p) }\n"
    "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_BASE = None


def _ir():
    global _BASE
    if _BASE is None:
        _BASE = compile_source(_SOURCE, "session_fork.rvl")
    return copy.deepcopy(_BASE)


def _session():
    from revl.mcp.session import Session
    return Session()


def _load(session, tmp_wal):
    session._wal_path = str(tmp_wal)
    session.load(_ir(), record=True, origin={"source": _SOURCE})


def _lines(sink):
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@needs_cordis
def test_parent_is_frozen_non_callable_after_fork_confirm(tmp_path):
    """HIGH 1: after `fork_confirm` the parent is retired at k and non-callable;
    the branch is the only live continuation over the shared workspace."""
    session = _session()
    _load(session, tmp_path / "parent.wal")
    filea = str(tmp_path / "a.txt")
    session.call("ops", "write", [filea, "v1"])            # records the fs effect
    assert os.path.exists(filea)

    # fork BEFORE the write effect (step 2 is the effect; provision/hinge are 0/1)
    report = session.fork(at=1)
    assert report["forked"] is False and "hash" in report
    result = session.fork_confirm(report["hash"])
    assert result["forked"] is True
    branch = result["branchSession"]

    # the parent is frozen: any op is refused
    from revl.mcp.session import SessionError
    with pytest.raises(SessionError, match="frozen"):
        session.call("ops", "write", [filea, "v2"])
    with pytest.raises(SessionError, match="frozen"):
        session.fork(at=0)

    # the branch is live and callable
    assert branch._frozen is False
    assert branch.state()["components"][0]["name"] == "Agent"


@needs_cordis
def test_clean_host_confined_fork_rewinds_fs_holds_sends_and_recover_retires(tmp_path):
    """A clean host-confined fork: the fs effect above k is rewound (the file is
    removed, back to its step-k absence), the branch holds its deferred send until
    its own commit (zero external residue), and recover treats the frozen parent
    as retired at k."""
    session = _session()
    parent_wal = tmp_path / "parent.wal"
    _load(session, parent_wal)
    filea = str(tmp_path / "a.txt")
    sink = str(tmp_path / "sink.log")

    session.call("ops", "write", [filea, "v1"])
    assert os.path.exists(filea)

    report = session.fork(at=1)                 # before the fs effect
    assert report["residue"]["clean"] is True   # only a host-confined effect above k
    assert [e["index"] for e in report["rewound"]["inversesRan"]] == [2]

    result = session.fork_confirm(report["hash"])
    assert result["forked"] is True
    branch = result["branchSession"]

    # the fs mutation above k was REWOUND: the file is gone (step-k state)
    assert not os.path.exists(filea)

    # the branch holds its deferred send until its OWN commit — zero external
    # residue while exploring
    branch.call("ops", "enqueue", [sink, "hello"])
    assert _lines(sink) == []                    # nothing crossed yet
    manifest = branch.commit()
    branch.commit_confirm(manifest["hash"])
    assert _lines(sink) == ["deliver:hello"]     # fired on the branch's own commit

    # recover on the PARENT WAL: the parent is retired at k, not re-admitted live
    from revl.recovery import recover
    verdict = recover(str(parent_wal))
    assert verdict["verdict"] == "fork-retired"
    assert verdict["at"] == 1
    assert verdict["branch"] == result["branch"]


@needs_cordis
def test_fork_confirm_refuses_a_stale_hash(tmp_path):
    """A drift since enumeration (a fresh effect) recomputes a different hash, so
    the confirm is refused with a fresh report — a result, not a crash."""
    session = _session()
    _load(session, tmp_path / "parent.wal")
    filea = str(tmp_path / "a.txt")
    session.call("ops", "write", [filea, "v1"])
    report = session.fork(at=1)
    # drift: another effect enters the timeline after enumeration
    session.call("ops", "write", [str(tmp_path / "b.txt"), "v2"])
    result = session.fork_confirm(report["hash"])
    assert result["forked"] is False and result["refused"] is True
    assert session._frozen is False              # nothing was rewound or frozen


@needs_cordis
def test_fork_refuses_a_kind_opaque_tail(tmp_path):
    """HIGH 2 at the session level: a `KIND_OPAQUE` step in the tail refuses the
    fork up front — the recorder cannot restore it, so it must not claim step-k
    state over it."""
    from revl.mcp.session import SessionError
    session = _session()
    _load(session, tmp_path / "parent.wal")
    filea = str(tmp_path / "a.txt")
    session.call("ops", "write", [filea, "v1"])
    # inject a KIND_OPAQUE step into the tail (a non-callable disposer the
    # recorder cannot run), exactly what `record_yield` records for one
    tl = session._timeline("Agent")
    _mk(tl, replay.KIND_OPAQUE, "buf.opaque", detail={"repr": "<socket>"})
    with pytest.raises(SessionError, match="KIND_OPAQUE"):
        session.fork(at=1)
    assert session._frozen is False


@needs_cordis
def test_fork_refuses_a_non_idempotent_span(tmp_path):
    """A declared non-idempotent-total inverse in the rewound span refuses the
    fork (Decision 5): a crash mid-fork could double-apply it."""
    from revl.mcp.session import SessionError
    session = _session()
    _load(session, tmp_path / "parent.wal")
    filea = str(tmp_path / "a.txt")
    session.call("ops", "write", [filea, "v1"])
    tl = session._timeline("Agent")
    tl.annotate_step(2, undo_idempotent=False)   # the fs effect, declared non-idempotent
    with pytest.raises(SessionError, match="non-idempotent"):
        session.fork(at=1)
