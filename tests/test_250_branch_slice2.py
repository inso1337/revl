"""Session branching, Slice 2 — durable lineage and the offline branch surface
(roadmap item 250).

Design: docs/design/250-session-branching.md.

Slice 1 shipped the fork as a LIVE primitive and knew everything it knew from the
in-process timeline. Slice 2 makes what the fork reasons about survive the
process, and builds the two read surfaces the roadmap item asks for on top:

  * the fork's three CLASSIFICATION INPUTS (capability `scope`, an emission's
    `compensated` offset, the `undoIdempotent` register entry) become durable WAL
    fields, ABSENT BY DEFAULT so no pre-250 record moves. The headline test is
    that the offline classifier then reproduces the LIVE partition exactly —
    without it the offline surface would be guessing;
  * `fork-branch` gives the branch side of the lineage, so a branch WAL read on
    its own names itself, its parent and its fork point, carries what it
    inherited and — explicitly — what it did not;
  * `revl branch` / `revl compare` read both, tier-agnostically (no backend on
    the path), and refuse to invent a divergence point they cannot read.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from revl import branch as branch_mod
from revl.wal import read_wal

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import replay  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the end-to-end fork is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh` and run under its venv",
)


# ---------------------------------------------------------------------------
# builders: a timeline of every kind, and the same timeline made durable
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


def _total_timeline():
    """One timeline touching every branch of the classifier: a host-confined fs
    inverse, an outbound net inverse, a provision, a bare emission, a compensated
    emission, the compensation itself, and the two provably empty kinds."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", scope={"caps": ["fs"]},
        undo=lambda: None)                                          # 0
    _mk(tl, replay.KIND_EFFECT, "net.put", scope={"caps": ["net"]},
        undo=lambda: None)                                          # 1
    _mk(tl, replay.KIND_PROVISION, "provide.cache", undo=lambda: None)  # 2
    _mk(tl, replay.KIND_EMISSION, "bus.publish")                    # 3
    _mk(tl, replay.KIND_EMISSION, "mail.send", compensation=5)      # 4
    _mk(tl, replay.KIND_COMPENSATION, "mail.retract", undo=lambda: None)  # 5
    _mk(tl, replay.KIND_BOUNDARY, "iteration")                      # 6
    _mk(tl, replay.KIND_HINGE, "frame.drain")                       # 7
    return tl


def _durable(tmp_path, timeline, name="run.wal", complete=False):
    """The same timeline written through the py WAL writer, so seq == step index."""
    path = str(tmp_path / name)
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.append_timeline(timeline)
    if complete:
        wal.commit_activation(components=[timeline.component])
    wal.close()
    return path


# ===========================================================================
# the classification inputs are durable, and absent by default
# ===========================================================================


def test_the_fork_classification_inputs_are_written_and_absent_by_default(tmp_path):
    """Scope, the compensated flag and the idempotency declaration reach the WAL —
    and a step declaring none of them writes none of them, so every pre-250
    record stays byte-identical and no WAL golden moves."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "plain", undo=lambda: None)
    _mk(tl, replay.KIND_EFFECT, "scoped", scope={"caps": ["net"]},
        undo=lambda: None, undo_idempotent=False)
    _mk(tl, replay.KIND_EMISSION, "offset", compensation=9)
    records = read_wal(_durable(tmp_path, tl))["records"]

    plain, scoped, offset = records
    assert "scope" not in plain and "compensated" not in plain
    assert "undoIdempotent" not in plain
    assert scoped["scope"] == {"caps": ["net"]}
    assert scoped["undoIdempotent"] is False
    assert offset["compensated"] is True


def test_the_offline_partition_reproduces_the_live_one(tmp_path):
    """The headline. The live classifier and the offline one must agree on every
    kind, or the offline surface is guessing at the partition it presents. Run the
    same timeline through both and require the same buckets."""
    timeline = _total_timeline()
    live = timeline.partition_tail(-1)
    offline = branch_mod.partition(_durable(tmp_path, timeline), -1)

    def seqs(entries):
        return sorted(e.get("seq", e.get("index")) for e in entries)

    assert seqs(offline["wouldRewind"]) == seqs(live["inversesRan"])
    assert seqs(offline["wouldWithdraw"]) == seqs(live["provisionsWithdrawn"])
    assert seqs(offline["emissionsCrossed"]) == seqs(live["emissionsCrossed"])
    assert seqs(offline["emissionsCompensated"]) == seqs(live["emissionsCompensated"])
    assert seqs(offline["wouldCrossOnRewind"]) == seqs(live["wouldCrossOnRewind"])
    assert seqs(offline["unrestored"]) == seqs(live["unrestored"])
    # and the partition is still TOTAL: boundary and hinge are provably empty,
    # every other recorded step landed in exactly one bucket
    classified = sum(len(offline[name]) for name in (
        "wouldRewind", "wouldWithdraw", "emissionsCrossed",
        "emissionsCompensated", "wouldCrossOnRewind", "unrestored"))
    assert classified == 6


def test_an_outbound_scoped_inverse_is_never_offered_as_rewindable_offline(tmp_path):
    """CRITICAL 2 held offline: the reader must not present an outbound-scoped
    witnessed inverse as something a fork would put back, or an operator would
    plan a rewind that PUTs to a remote endpoint."""
    doc = branch_mod.partition(_durable(tmp_path, _total_timeline()), -1)
    rewindable = [e["label"] for e in doc["wouldRewind"]]
    assert rewindable == ["fs.write"]
    would = {e["label"]: e["why"] for e in doc["wouldCrossOnRewind"]}
    assert "net.put" in would and "not provably host-confined" in would["net.put"]
    assert "mail.retract" in would          # a compensation is outbound by definition
    assert doc["residue"]["clean"] is False


def test_an_unknown_capability_token_reads_as_crossing_offline(tmp_path):
    """Fail-safe, mirrored: a scope naming a token the reader does not know is
    enumerated, never offered as rewindable."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "quantum.write", scope={"caps": ["quantum"]},
        undo=lambda: None)
    doc = branch_mod.partition(_durable(tmp_path, tl), -1)
    assert doc["wouldRewind"] == []
    assert [e["label"] for e in doc["wouldCrossOnRewind"]] == ["quantum.write"]


def test_a_scopeless_record_reads_as_host_confined_and_says_so(tmp_path):
    """An absent scope reads exactly as the live classifier reads `None`. That is
    the reader's one blind spot over an older WAL, so it is stated on the document
    rather than left for the reader to discover."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "legacy", undo=lambda: None)
    doc = branch_mod.partition(_durable(tmp_path, tl), -1)
    assert [e["label"] for e in doc["wouldRewind"]] == ["legacy"]
    assert "cannot distinguish" in doc["readerNote"]


def test_the_tail_is_bounded_by_at(tmp_path):
    """`--at` names a WAL position; only what is above it is in the partition."""
    path = _durable(tmp_path, _total_timeline())
    assert [e["label"] for e in branch_mod.partition(path, 4)["wouldCrossOnRewind"]] \
        == ["mail.retract"]
    assert branch_mod.partition(path, 7)["residue"]["clean"] is True


# ===========================================================================
# the Slice-1 refusals, reported offline as findings
# ===========================================================================


def test_every_refusal_is_reported_not_just_the_first(tmp_path):
    """The live fork refuses on the first hazard because it is about to touch a
    workspace. Offline nothing is touched, so all of them are reported — an
    operator planning a fork should see every reason at once."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", scope={"caps": ["fs"]},
        undo=lambda: None, undo_idempotent=False)
    _mk(tl, replay.KIND_OPAQUE, "buf.opaque", detail={"repr": "<socket>"})
    path = _durable(tmp_path, tl)
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_commit_approved("deadbeef")          # a committed boundary below
    wal.close()

    doc = branch_mod.partition(path, -1)
    assert {f["finding"] for f in doc["findings"]} == {
        "committed-boundary", "opaque-tail", "non-idempotent-span"}
    assert doc["forkable"] is False


def test_a_clean_recorded_tail_is_forkable(tmp_path):
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", scope={"caps": ["fs"]}, undo=lambda: None)
    doc = branch_mod.partition(_durable(tmp_path, tl), -1)
    assert doc["findings"] == [] and doc["forkable"] is True
    assert doc["residue"]["clean"] is True
    assert doc["performed"] is False        # an offline reader rewinds nothing


# ===========================================================================
# lineage: what one WAL IS
# ===========================================================================


def _fork_pair(tmp_path, *, parent="P", child="B", at=1, crossed=(),
               parent_name="parent.wal", branch_name="branch.wal"):
    """A parent WAL frozen at `at` and the branch WAL that diverged from it,
    written through the same records `fork_confirm` writes."""
    parent_path = str(tmp_path / parent_name)
    branch_path = str(tmp_path / branch_name)
    pwal = replay.WriteAheadLog(parent_path, ir={}, generation=1).open()
    pwal.record_fork_begin(parent=parent, at=at, crossed=list(crossed),
                           would_cross=[])
    pwal.record_fork_frozen(parent=parent, at=at)
    pwal.record_fork_complete(branch=child)
    pwal.close()
    bwal = replay.WriteAheadLog(branch_path, ir={}, generation=1).open()
    bwal.record_fork_branch(branch=child, parent=parent, at=at,
                            parent_wal=parent_path,
                            preserved={"at": at, "composition": ["C"],
                                       "capabilities": ["fs"]},
                            not_preserved=[{"axis": "seedsAndClock", "why": "..."}],
                            crossed=list(crossed), would_cross=[])
    bwal.close()
    return parent_path, branch_path


def test_a_branch_wal_names_itself_its_parent_and_its_fork_point(tmp_path):
    """Slice 1 recorded the edge only on the parent, so a branch WAL alone could
    not say it was a branch. It can now."""
    _, branch_path = _fork_pair(tmp_path)
    doc = branch_mod.lineage(branch_path)
    assert doc["role"] == "branch"
    assert doc["session"] == "B" and doc["parent"] == "P"
    assert doc["divergedAt"] == 1
    assert doc["preserved"]["capabilities"] == ["fs"]
    assert [e["axis"] for e in doc["notPreserved"]] == ["seedsAndClock"]


def test_a_frozen_parent_reads_as_a_forked_parent(tmp_path):
    parent_path, _ = _fork_pair(tmp_path)
    doc = branch_mod.lineage(parent_path)
    assert doc["role"] == "forked-parent"
    assert doc["session"] == "P" and doc["child"] == "B"
    assert doc["frozenAt"] == 1 and doc["forkComplete"] is True


def test_a_plain_wal_reads_as_standalone(tmp_path):
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", scope={"caps": ["fs"]}, undo=lambda: None)
    doc = branch_mod.lineage(_durable(tmp_path, tl))
    assert doc["role"] == "standalone" and doc["session"] is None
    assert "preserved" not in doc


def test_a_branch_forked_again_reads_as_a_forked_branch(tmp_path):
    """The serial N-branch exploration Decision 4 describes: each branch is forked
    again from its own fork point, so a WAL can be both."""
    _, branch_path = _fork_pair(tmp_path)
    wal = replay.WriteAheadLog(branch_path, ir={}, generation=1).open()
    wal.record_fork_frozen(parent="B", at=3)
    wal.record_fork_complete(branch="B2")
    wal.close()
    doc = branch_mod.lineage(branch_path)
    assert doc["role"] == "forked-branch"
    assert doc["parent"] == "P" and doc["child"] == "B2"


def test_an_unclosed_fork_bracket_is_reported_as_a_mid_fork_window(tmp_path):
    """`fork-begin` with no `fork-complete` is the dangerous window (Decision 5).
    The reader states it; it does not close it."""
    path = str(tmp_path / "half.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_fork_begin(parent="P", at=1, crossed=[], would_cross=[])
    wal.record_fork_frozen(parent="P", at=1)
    wal.close()
    doc = branch_mod.lineage(path)
    assert doc["forkComplete"] is False
    assert "half rewound" in doc["midFork"]


def test_inherited_residue_is_carried_on_the_branch_not_only_the_parent(tmp_path):
    """A branch explored without its parent's WAL to hand must still be able to
    say what irreversible residue it is standing on."""
    crossed = [{"index": 4, "label": "mail.send"}]
    _, branch_path = _fork_pair(tmp_path, crossed=crossed)
    residue = branch_mod.lineage(branch_path)["inheritedResidue"]
    assert residue["clean"] is False
    assert residue["outstanding"] == [4]
    assert "cannot be undone" in residue["proof"]


# ===========================================================================
# topology
# ===========================================================================


def test_topology_builds_the_tree_from_durable_edges(tmp_path):
    parent_path, branch_path = _fork_pair(tmp_path)
    doc = branch_mod.topology([parent_path, branch_path])
    assert doc["roots"] == ["P"]
    assert doc["children"] == {"P": ["B"]}
    assert doc["orphans"] == [] and doc["dangling"] == []


def test_topology_names_the_edges_it_cannot_close(tmp_path):
    """A tree that quietly omits the half it could not see is the one way this
    view could mislead, so a missing parent and a missing branch are both
    reported."""
    parent_path, branch_path = _fork_pair(tmp_path)
    orphan = branch_mod.topology([branch_path])
    assert [e["branch"] for e in orphan["orphans"]] == ["B"]
    dangling = branch_mod.topology([parent_path])
    assert [e["branch"] for e in dangling["dangling"]] == ["B"]


def test_topology_does_not_invent_an_identity_for_a_plain_wal(tmp_path):
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", undo=lambda: None)
    doc = branch_mod.topology([_durable(tmp_path, tl)])
    assert doc["sessions"] == [] and len(doc["unidentified"]) == 1


# ===========================================================================
# compare
# ===========================================================================


def test_compare_relates_a_parent_and_its_branch(tmp_path):
    parent_path, branch_path = _fork_pair(tmp_path)
    doc = branch_mod.compare(parent_path, branch_path)
    assert doc["relation"] == "parent-branch"
    assert doc["comparable"] is True and doc["divergedAt"] == 1
    assert [e["axis"] for e in doc["notComparable"]] == \
        ["decisionCause", "counterfactual"]


def test_compare_relates_two_siblings_of_the_same_fork_point(tmp_path):
    """Serial exploration: two branches forked from the same parent at the same
    step are the alternatives the primitive exists to compare."""
    _, first = _fork_pair(tmp_path, child="B1", branch_name="b1.wal")
    _, second = _fork_pair(tmp_path, child="B2", branch_name="b2.wal")
    doc = branch_mod.compare(first, second)
    assert doc["relation"] == "siblings" and doc["divergedAt"] == 1


def test_compare_refuses_two_unrelated_wals(tmp_path):
    """No invented common point: without a recorded relation a per-side tail is a
    diff of two unrelated runs dressed up as a branch comparison."""
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", undo=lambda: None)
    left = _durable(tmp_path, tl, name="left.wal")
    right = _durable(tmp_path, tl, name="right.wal")
    doc = branch_mod.compare(left, right)
    assert doc["relation"] == "unrelated" and doc["comparable"] is False
    assert "no divergence point" in doc["why"]
    assert "delta" not in doc


def test_compare_refuses_siblings_forked_at_different_steps(tmp_path):
    _, first = _fork_pair(tmp_path, child="B1", at=1, branch_name="b1.wal")
    _, second = _fork_pair(tmp_path, child="B2", at=4, branch_name="b2.wal")
    doc = branch_mod.compare(first, second)
    assert doc["relation"] == "siblings" and doc["divergedAt"] is None


def test_compare_reports_the_first_divergent_step_and_the_shared_prefix(tmp_path):
    """The two branches agree for a while and then differ; the report says where,
    keyed on the step identity a WAL carries and not on per-session seq numbers."""
    _, first = _fork_pair(tmp_path, child="B1", branch_name="b1.wal")
    _, second = _fork_pair(tmp_path, child="B2", branch_name="b2.wal")
    for path, tail in ((first, "fs.write_a"), (second, "fs.write_b")):
        tl = replay.Timeline("C")
        _mk(tl, replay.KIND_EFFECT, "shared.step", scope={"caps": ["fs"]},
            undo=lambda: None)
        _mk(tl, replay.KIND_EFFECT, tail, scope={"caps": ["fs"]}, undo=lambda: None)
        wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
        wal.append_timeline(tl)
        wal.close()

    delta = branch_mod.compare(first, second)["delta"]
    assert delta["sharedPrefix"] == 1
    assert delta["firstDifference"]["left"]["label"] == "fs.write_a"
    assert delta["firstDifference"]["right"]["label"] == "fs.write_b"
    assert [e["label"] for e in delta["onlyLeft"]] == ["fs.write_a"]
    assert [e["label"] for e in delta["onlyRight"]] == ["fs.write_b"]
    assert delta["identical"] is False


# ===========================================================================
# recovery is lineage-aware (the verdict itself is untouched)
# ===========================================================================


def test_recover_tells_an_operator_a_rollback_lands_at_a_fork_point(tmp_path):
    """A branch recovers exactly as any session does — over its own effects. What
    changes is that the report says the rollback lands at a fork point, not at an
    empty workspace, so nobody reads a branch's rollback as a fresh session's."""
    from revl.recovery import recover, render

    _, branch_path = _fork_pair(tmp_path)
    report = recover(branch_path)
    assert report["lineage"]["parent"] == "P"
    assert report["lineage"]["divergedAt"] == 1
    assert "fork point" in render(report)


def test_recover_is_untouched_for_a_wal_with_no_lineage(tmp_path):
    from revl.recovery import recover

    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", undo=lambda: None)
    assert "lineage" not in recover(_durable(tmp_path, tl))


def test_a_frozen_parent_still_recovers_as_retired(tmp_path):
    """Slice 1's verdict for the parent is unchanged by Slice 2."""
    from revl.recovery import recover

    parent_path, _ = _fork_pair(tmp_path)
    assert recover(parent_path)["verdict"] == "fork-retired"


# ===========================================================================
# the CLI
# ===========================================================================


def _cli(argv, capsys):
    from revl.__main__ import main
    code = main(argv)
    return code, capsys.readouterr().out


def test_revl_branch_prints_the_lineage_of_one_wal(tmp_path, capsys):
    _, branch_path = _fork_pair(tmp_path)
    code, out = _cli(["branch", "--wal", branch_path], capsys)
    assert code == 0
    assert "BRANCH" in out and "branch of  P at step 1" in out


def test_revl_branch_prints_the_tree_of_several_wals(tmp_path, capsys):
    parent_path, branch_path = _fork_pair(tmp_path)
    code, out = _cli(["branch", "--wal", parent_path, "--wal", branch_path], capsys)
    assert code == 0 and "branch topology" in out and "forked at step 1" in out


def test_revl_branch_exits_nonzero_on_an_unclosed_edge(tmp_path, capsys):
    _, branch_path = _fork_pair(tmp_path)
    code, out = _cli(["branch", "--wal", branch_path, "--wal", branch_path], capsys)
    assert code == 1 and "ORPHAN" in out


def test_revl_branch_at_enumerates_the_partition_as_json(tmp_path, capsys):
    path = _durable(tmp_path, _total_timeline())
    code, out = _cli(["branch", "--wal", path, "--at", "-1", "--json"], capsys)
    doc = json.loads(out)
    assert code == 1                              # honest residue: an emission crossed
    assert doc["kind"] == "revl.branch-partition"
    assert [e["label"] for e in doc["wouldRewind"]] == ["fs.write"]


def test_revl_compare_exits_nonzero_when_there_is_no_divergence_point(
        tmp_path, capsys):
    tl = replay.Timeline("C")
    _mk(tl, replay.KIND_EFFECT, "fs.write", undo=lambda: None)
    left = _durable(tmp_path, tl, name="left.wal")
    right = _durable(tmp_path, tl, name="right.wal")
    code, out = _cli(["compare", left, right], capsys)
    assert code == 1 and "UNRELATED" in out


def test_revl_branch_reports_a_missing_wal_as_an_error(tmp_path, capsys):
    from revl.__main__ import main
    assert main(["branch", "--wal", str(tmp_path / "gone.wal")]) == 1


# ===========================================================================
# end to end: a real fork writes the branch lineage
# ===========================================================================


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
    "service Ops { emission fn write(p: Str, data: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn write(p, data) { effect fs_write(p, data) undo fs_del(p) }\n"
    "  }\n"
    "}\n"
)


@needs_cordis
def test_fork_confirm_writes_a_self_describing_branch_wal(tmp_path):
    """End to end: fork a live session and read the branch's own WAL back through
    the offline surface. The branch names itself, its parent and its fork point,
    and the topology closes the edge from the two WALs alone."""
    from revl.compiler import compile_source
    from revl.mcp.session import Session

    session = Session()
    session._wal_path = str(tmp_path / "parent.wal")
    session.load(compile_source(_SOURCE, "branch_slice2.rvl"), record=True,
                 origin={"source": _SOURCE})
    session.call("ops", "write", [str(tmp_path / "a.txt"), "v1"])

    report = session.fork(at=1)
    result = session.fork_confirm(report["hash"])
    branch = result.pop("branchSession")
    assert result["lineage"]["preserved"]["composition"] == ["Agent"]
    assert [e["axis"] for e in result["lineage"]["notPreserved"]] == [
        "providerVersions", "seedsAndClock", "modelDecisions"]

    doc = branch_mod.lineage(branch._wal_path)
    assert doc["role"] == "branch"
    assert doc["session"] == result["branch"]
    assert doc["parent"] == result["parent"]
    assert doc["divergedAt"] == 1
    assert doc["parentWal"] == os.path.abspath(session._wal_path)

    tree = branch_mod.topology([session._wal_path, branch._wal_path])
    assert tree["children"] == {result["parent"]: [result["branch"]]}
    assert tree["orphans"] == [] and tree["dangling"] == []

    relation = branch_mod.compare(session._wal_path, branch._wal_path)
    assert relation["relation"] == "parent-branch" and relation["divergedAt"] == 1
