"""Two-phase admission, Slice 0: the surface epoch and the content digests.

`docs/design/460-two-phase-admission-forward-recovery.md` §3, §7. Slice 0 lands
the CAS primitives the later slices will decide on, COMPUTED but not yet DRIVING
any recovery (the compute-but-do-not-yet-decide discipline): a per-session
`_surface_epoch` that moves on every class-map install, the `(baseManifestHash,
classMapDigest)` content digests recomputed from the live composition, and a
`_cas_surface` helper that refuses on drift. No WAL record and no decision is
written yet — Slices 1-3 add those.

The exit test the slice plan names:

  * the epoch increments once per `load`, `swap`, `undo`, `rollback` and
    `_wire_turn`, and NOT on `call`;
  * the digest is stable across two builds of the same class map and differs
    when a provider's class changes — INCLUDING the §3 load-bearing case where
    the manifest hash is unchanged but a granted provider's crossing class moved;
  * the helper refuses on either half moving (the in-process `(generation,
    surfaceEpoch)` pair or the across-restart content digests).

The epoch/`_wire_turn`/`swap` half needs a live cordis composition and is gated
on it; the digest and CAS-helper half is pure over the compiler + the class map
and runs everywhere.
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
    reason="the epoch moves are proven against a live cordis-py composition "
           "(load/swap/undo/wire) — install it with `sh backends/python/setup.sh`",
)


# --------------------------------------------------------------------------- #
# Sources. `_SRC_C` is a granted tool whose sole crossing is an immediate
# emission — class (c). `_SRC_NOOP` is the SAME composition (same component,
# same key, same manifest) whose provider no longer crosses — class folds away.
# `_SRC_OTHER` renames the component, so the manifest itself moves.
# --------------------------------------------------------------------------- #

_SRC_C = (
    "extern emission fn announce(sink: Str, msg: Str) = @py { return }\n"
    "service Ops { emission fn shout(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }\n"
    "}\n"
)

_SRC_NOOP = (
    "extern emission fn announce(sink: Str, msg: Str) = @py { return }\n"
    "service Ops { fn shout(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { } }\n"
    "}\n"
)

_SRC_OTHER = (
    "extern emission fn announce(sink: Str, msg: Str) = @py { return }\n"
    "service Ops { emission fn shout(sink: Str, msg: Str) }\n"
    "component Herald provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }\n"
    "}\n"
)

# An untrusted per-turn source with NO host code of its own — it only forwards
# to the granted `ops`, the shape `test_admit_approval_gate` gates.
_TURN_FORWARD = (
    "service Turn { emission fn run(sink: Str, msg: Str) }\n"
    "component TurnComp requires ops: Ops provides turn: Turn {\n"
    "  provide turn {\n"
    '    fn run(sink, msg) { emit ops.shout(sink, msg) }\n'
    "  }\n"
    "}\n"
)


def _compile(src):
    from revl import compile_files
    p = os.path.abspath("base.rvl")
    return compile_files([p], sources={p: src})


def _class_map(src):
    from revl.mcp.approval import ClassMap
    return ClassMap(_compile(src))


# --------------------------------------------------------------------------- #
# classMapDigest — stable across two builds, moves on a class change.
# --------------------------------------------------------------------------- #

def test_class_map_digest_is_stable_across_two_builds_of_the_same_map():
    from revl.mcp.session import _class_map_digest_of
    a = _class_map_digest_of(_class_map(_SRC_C))
    b = _class_map_digest_of(_class_map(_SRC_C))
    assert a is not None
    assert a == b, "the digest is not stable over two builds of the same map"


def test_class_map_digest_moves_when_a_providers_class_changes_manifest_held():
    """§3's load-bearing case: the manifest hash is UNCHANGED, but a granted
    provider's crossing class moved (class (c) -> folds away), so the class-map
    digest is the only half that catches it. This is exactly the surface a
    decision must not carry across."""
    from revl.mcp.session import _base_manifest_hash_of, _class_map_digest_of
    ir_c = _compile(_SRC_C)
    ir_n = _compile(_SRC_NOOP)
    # the manifest is identical: same component, same key, same load order.
    assert _base_manifest_hash_of(ir_c) == _base_manifest_hash_of(ir_n)
    # the class map is NOT: the provider's fold moved from (c) to none.
    from revl.mcp.approval import ClassMap
    assert ClassMap(ir_c)._reach["Agent:ops.shout"]["class"] == "c"
    assert ClassMap(ir_n)._reach["Agent:ops.shout"]["class"] is None
    assert _class_map_digest_of(ClassMap(ir_c)) \
        != _class_map_digest_of(ClassMap(ir_n)), \
        "the class-map digest did not move on a provider reclassification"


# --------------------------------------------------------------------------- #
# baseManifestHash — stable across two builds, moves when the manifest moves.
# --------------------------------------------------------------------------- #

def test_base_manifest_hash_is_stable_and_moves_with_the_manifest():
    from revl.mcp.session import _base_manifest_hash_of
    a = _base_manifest_hash_of(_compile(_SRC_C))
    b = _base_manifest_hash_of(_compile(_SRC_C))
    assert a is not None and a == b, "manifest hash unstable over two builds"
    # a different component name is a different manifest.
    assert _base_manifest_hash_of(_compile(_SRC_OTHER)) != a


def test_digests_are_none_without_a_manifest_or_class_map():
    from revl.mcp.session import _base_manifest_hash_of, _class_map_digest_of
    assert _base_manifest_hash_of(None) is None
    assert _base_manifest_hash_of({}) is None
    assert _class_map_digest_of(None) is None


# --------------------------------------------------------------------------- #
# _cas_surface — refuses on either half moving.
# --------------------------------------------------------------------------- #

def _surface_session(src):
    """A Session with the surface fields set by hand — no runtime, so the CAS
    helper is exercised without a live cordis composition."""
    from revl.mcp.session import Session
    from revl.mcp.approval import ClassMap
    s = Session()
    s.ir = _compile(src)
    s._class_map = ClassMap(s.ir)
    s._generation = 3
    s._surface_epoch = 5
    return s


def test_cas_surface_passes_when_nothing_moved():
    s = _surface_session(_SRC_C)
    # the expected block a decision would record, then an immediate re-check.
    s._cas_surface(s._surface_expected())


def test_cas_surface_refuses_when_the_surface_epoch_moved():
    from revl.mcp.session import SessionError
    s = _surface_session(_SRC_C)
    expected = s._surface_expected()
    s._surface_epoch += 1
    with pytest.raises(SessionError) as caught:
        s._cas_surface(expected)
    assert "surfaceEpoch" in str(caught.value)


def test_cas_surface_refuses_when_the_generation_moved():
    from revl.mcp.session import SessionError
    s = _surface_session(_SRC_C)
    expected = s._surface_expected()
    s._generation += 1
    with pytest.raises(SessionError):
        s._cas_surface(expected)


def test_cas_surface_refuses_when_the_class_map_digest_moved():
    """The across-restart half: the in-process pair is held fixed, but the class
    map was rebuilt over a reclassified provider (manifest held), so the content
    digest moved and the CAS must refuse — a decision never finalizes onto a
    surface it did not see."""
    from revl.mcp.session import SessionError
    from revl.mcp.approval import ClassMap
    s = _surface_session(_SRC_C)
    expected = s._surface_expected()
    # same generation and epoch, same manifest, but the class map moved.
    s._class_map = ClassMap(_compile(_SRC_NOOP))
    assert s._surface_expected()["baseManifestHash"] \
        == expected["baseManifestHash"], "guard: the manifest must be held fixed"
    with pytest.raises(SessionError) as caught:
        s._cas_surface(expected)
    assert "classMapDigest" in str(caught.value)


# --------------------------------------------------------------------------- #
# The epoch moves — the cordis-gated half.
# --------------------------------------------------------------------------- #

def _base_path(tmp_path):
    """A REAL on-disk base source. `_record_generation` builds a generation's
    re-admittable snapshot by materializing the recorded `origin` files off disk
    (`persist._materialize`), so the base cannot be a purely in-memory virtual
    path if the generation is to survive an `undo` (item 597). Idempotent: the
    content is constant, so re-writing across calls is harmless."""
    p = tmp_path / "base.rvl"
    p.write_text(_SRC_C)
    return str(p)


def _base_ir(base_path):
    from revl import compile_files
    from revl._paths import stdlib_root
    admit_path = str(stdlib_root() / "admit.rvl")
    return compile_files([base_path, admit_path], sources={base_path: _SRC_C})


def _base_origin(base_path):
    """The admission inputs that let a loaded/swapped generation record a
    re-admittable snapshot: the co-root files (the on-disk base plus the stdlib
    `admit.rvl`), materialized from disk at snapshot time exactly as a live
    `revl_load` records them. Without this the generation snapshots to None and
    `Session.undo()` correctly refuses it (item 597)."""
    from revl._paths import stdlib_root
    return {"files": [base_path, str(stdlib_root() / "admit.rvl")]}


def _gated_session(tmp_path):
    from revl.mcp.session import Session
    base_path = _base_path(tmp_path)
    session = Session()
    session.approval_policy = "auto"
    session._wal_path = str(tmp_path / "session.wal")
    session.load(copy.deepcopy(_base_ir(base_path)), record=True,
                 origin=_base_origin(base_path))
    return session


@needs_cordis
def test_surface_epoch_moves_on_every_install_and_not_on_call(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    base_path = _base_path(tmp_path)

    # load installed the class map: epoch 0 -> 1, alongside generation 1.
    assert session._surface_epoch == 1
    assert session._generation == 1
    st = session.state()
    assert st["surfaceEpoch"] == 1
    assert st["baseManifestHash"] is not None
    assert st["classMapDigest"] is not None

    # a call decides against the live surface; it never installs one. The
    # class-(c) crossing prompts, but the epoch does not move either way.
    before = session._surface_epoch
    digests_before = session._surface_digests()
    with pytest.raises(ApprovalRequired):
        session.call("ops", "shout", [str(tmp_path / "c.log"), "x"])
    assert session._surface_epoch == before, "a call moved the surface epoch"
    assert session._surface_digests() == digests_before

    # wiring an admitted turn REBUILDS the class map but does NOT move the
    # generation (426 §5.2) — the epoch is the only counter that catches it.
    assert session.admit(_TURN_FORWARD, granted=["Ops"]).admitted
    assert session._surface_epoch == 2, "wiring a turn did not move the epoch"
    assert session._generation == 1, "wiring a turn must not move the generation"
    # the turn widened the surface, so both digests moved.
    assert session._surface_digests() != digests_before

    # a swap installs a new generation's class map: epoch and generation both move.
    session.swap(copy.deepcopy(_base_ir(base_path)), origin=_base_origin(base_path))
    assert session._generation == 2
    assert session._surface_epoch == 3

    # undo routes through swap: one more install, one more of each. Generation 1
    # was source-backed (see `_gated_session`), so its snapshot re-admits through
    # the gate rather than being refused for missing sources (item 597).
    session.undo()
    assert session._generation == 3
    assert session._surface_epoch == 4


@needs_cordis
def test_undo_refuses_a_generation_loaded_without_recorded_sources(tmp_path):
    """Item 597 guard, retained coverage. The success path above now source-backs
    its generations so the undo/epoch assertions are exercised; keep the
    COMPLEMENTARY guarantee that `Session.undo()` still REFUSES a generation
    loaded without re-admittable sources (`snapshot=None`) rather than bypassing
    the admission gate. This must not be weakened."""
    from revl.mcp.session import Session, SessionError
    base_path = _base_path(tmp_path)
    session = Session()
    session.approval_policy = "auto"
    session._wal_path = str(tmp_path / "session.wal")
    # loaded WITHOUT `origin`: generation 1 records no re-admittable snapshot.
    session.load(copy.deepcopy(_base_ir(base_path)), record=True)
    session.swap(copy.deepcopy(_base_ir(base_path)))
    with pytest.raises(SessionError) as caught:
        session.undo()
    assert "without recorded sources" in str(caught.value)


# --------------------------------------------------------------------------- #
# Slice 1: the durable stage records (design 460 §2). Written through the WAL's
# single seq space, so they order against the crossings the activation body
# journals. These exercise the record methods directly — no live runtime.
# --------------------------------------------------------------------------- #

_TURN_BUNDLE = {"sources": {"<turn>.rvl": _TURN_FORWARD},
                "granted": ["Ops"], "modules": {}}


def _wal(tmp_path):
    import sys as _sys
    _sys.path.insert(0, str(_BACKEND))
    from replay import WriteAheadLog
    return WriteAheadLog(str(tmp_path / "s.wal")).open()


def test_stage_records_share_the_seq_space_and_order_decided_then_finalized(tmp_path):
    """§2: the three stages are ordered events on the session's single seq
    space, so `decided < every crossing the body journals < applied <
    finalized`. Here decided, one effect, applied, finalized are written in that
    order and the seqs come out strictly increasing."""
    wal = _wal(tmp_path)
    d = wal.record_admit_decided(
        decision_id="D1", turn=_TURN_BUNDLE,
        expected={"generation": 1, "surfaceEpoch": 2,
                  "baseManifestHash": "sha256:aa", "classMapDigest": "sha256:bb"},
        spends=["r1"], components=["TurnComp"], keys=["turn"])
    # a crossing the activation body journals, between decided and applied.
    mid = wal.record_boundary(
        "TurnComp", "shout", resource="file:/tmp/x",
        inverse_op={"receiver": "fs", "method": "rm", "args": ["/tmp/x"]})
    a = wal.record_admit_applied(decision_id="D1", generation=1, surface_epoch=2)
    f = wal.record_admit_finalized(decision_id="D1", generation=1, surface_epoch=2)
    wal.close()
    assert d["seq"] < mid["seq"] < a["seq"] < f["seq"]
    assert d["record"] == "admit-decided" and d["spends"] == ["r1"]
    assert d["turn"] == _TURN_BUNDLE
    assert a["observed"] == {"generation": 1, "surfaceEpoch": 2}


def test_abandoned_is_a_terminal_record_with_a_reason(tmp_path):
    wal = _wal(tmp_path)
    wal.record_admit_decided(
        decision_id="D2", turn=_TURN_BUNDLE, expected={}, spends=[],
        components=["TurnComp"], keys=["turn"])
    ab = wal.record_admit_abandoned(decision_id="D2", reason="plug-failed")
    wal.close()
    assert ab["record"] == "admit-abandoned"
    assert ab["decisionId"] == "D2" and ab["reason"] == "plug-failed"


def test_a_session_that_never_admits_writes_no_admit_records(tmp_path):
    """§7 non-vacuity: the record methods are the ONLY source of `admit-*`
    records, so a WAL that never calls them carries none — the byte-identical
    guarantee for a composition that never admits."""
    from revl.wal import read_wal
    wal = _wal(tmp_path)
    wal.record_boundary("C", "x", resource="file:/tmp/y",
                        inverse_op={"receiver": "fs", "method": "rm",
                                    "args": ["/tmp/y"]})
    wal.commit_activation(components=["C"])
    wal.close()
    got = read_wal(str(tmp_path / "s.wal"))
    assert not [r for r in got["records"]
                if str(r.get("record", "")).startswith("admit-")]


# --------------------------------------------------------------------------- #
# decisionId (design 460 §2.1): bound to source + granted + base + seq.
# --------------------------------------------------------------------------- #

def test_decision_id_is_stable_and_moves_with_each_input():
    from revl.mcp.session import _decision_id_of
    base = _decision_id_of({"t.rvl": _TURN_FORWARD}, ["Ops"], "sha256:aa", 5)
    assert base == _decision_id_of({"t.rvl": _TURN_FORWARD}, ["Ops"],
                                   "sha256:aa", 5), "not stable over two derivations"
    # each input moves it.
    assert base != _decision_id_of({"t.rvl": _TURN_FORWARD + "\n"}, ["Ops"],
                                   "sha256:aa", 5)
    assert base != _decision_id_of({"t.rvl": _TURN_FORWARD}, ["Ops", "Other"],
                                   "sha256:aa", 5)
    assert base != _decision_id_of({"t.rvl": _TURN_FORWARD}, ["Ops"],
                                   "sha256:cc", 5)
    assert base != _decision_id_of({"t.rvl": _TURN_FORWARD}, ["Ops"],
                                   "sha256:aa", 6)


# --------------------------------------------------------------------------- #
# Slice 3: recover_forward_admissions classification (design 460 §5). Pure over
# the WAL records + the content CAS, so it runs without a live runtime.
# --------------------------------------------------------------------------- #

def _forward_session(src=_SRC_C):
    """A restored-base session with the surface set by hand (no runtime), so the
    content CAS `_forward_surface_for_turn` performs is exercised without cordis:
    it recompiles the recorded turn against this base and digests the merged map,
    which is pure over the compiler."""
    from revl.mcp.session import Session
    from revl.mcp.approval import ClassMap
    s = Session()
    s.approval_policy = "auto"
    s.ir = _compile(src)
    s._class_map = ClassMap(s.ir)
    s._generation = 1
    s._surface_epoch = 1
    return s


def _decided_record(session, decision_id="D1", seq=5):
    """An `admit-decided` record whose `expected` surface is exactly what
    `_forward_surface_for_turn` recomputes for `session` — so the content CAS
    MATCHES when recovery runs against this same base."""
    live = session._forward_surface_for_turn(_TURN_BUNDLE)
    return {"record": "admit-decided", "seq": seq, "decisionId": decision_id,
            "turn": _TURN_BUNDLE,
            "expected": {"generation": 1, "surfaceEpoch": 2, **live},
            "spends": ["r1"], "components": ["TurnComp"], "keys": ["turn"]}


def test_owed_when_the_runtime_did_not_advance_past_the_decision():
    from revl.recovery import recover_forward_admissions
    s = _forward_session()
    wal = {"records": [_decided_record(s)]}
    reports = recover_forward_admissions(wal, session=s)
    assert len(reports) == 1
    assert reports[0]["classification"] == "owed"
    # the report names the re-admittable turn and the spends — never a silent re-run.
    assert reports[0]["turn"] == _TURN_BUNDLE
    assert reports[0]["spends"] == ["r1"]
    assert reports[0]["finalized"] is False


def test_advanced_and_surface_matches_finalizes_forward(tmp_path):
    from revl.recovery import recover_forward_admissions
    from revl.wal import read_wal
    s = _forward_session()
    decided = _decided_record(s)
    wal = {"records": [decided,
                       {"record": "admit-applied", "seq": 7, "decisionId": "D1",
                        "observed": {"generation": 1, "surfaceEpoch": 2}}]}
    # forward=False: classify only, change nothing.
    dry = recover_forward_admissions(wal, session=s)
    assert dry[0]["classification"] == "advanced" and dry[0]["finalized"] is False

    # forward=True with a WAL path: append `admit-finalized`.
    path = str(tmp_path / "fwd.wal")
    # a minimal valid WAL so a later read parses (header + the same records).
    import json as _json
    with open(path, "w", encoding="utf-8") as h:
        h.write(_json.dumps({"record": "header", "walVersion": 1}) + "\n")
        for r in wal["records"]:
            h.write(_json.dumps(r, sort_keys=True) + "\n")
    reports = recover_forward_admissions(wal, session=s, forward=True,
                                         wal_path=path)
    assert reports[0]["classification"] == "advanced"
    assert reports[0]["finalized"] is True
    got = read_wal(path)["records"]
    fin = [r for r in got if r.get("record") == "admit-finalized"]
    assert len(fin) == 1 and fin[0]["decisionId"] == "D1"


def test_advanced_but_surface_drifted_is_stale_and_abandons(tmp_path):
    """§5: the runtime advanced, but the content CAS fails — the class-map digest
    the decision was checked against moved. The decision is abandoned, finalizes
    nothing, and the report names the drift."""
    from revl.recovery import recover_forward_admissions
    from revl.wal import read_wal
    s = _forward_session()
    decided = _decided_record(s)
    # a decision whose recorded digest does not match the restored surface.
    decided["expected"]["classMapDigest"] = "sha256:staleaaaa"
    wal = {"records": [decided,
                       {"record": "admit-applied", "seq": 7, "decisionId": "D1",
                        "observed": {}}]}
    path = str(tmp_path / "stale.wal")
    import json as _json
    with open(path, "w", encoding="utf-8") as h:
        h.write(_json.dumps({"record": "header", "walVersion": 1}) + "\n")
    reports = recover_forward_admissions(wal, session=s, forward=True,
                                         wal_path=path)
    assert reports[0]["classification"] == "stale"
    assert reports[0]["finalized"] is False and reports[0]["abandoned"] is True
    assert "classMapDigest" in (reports[0]["drift"] or "")
    got = read_wal(path)["records"]
    assert [r for r in got if r.get("record") == "admit-abandoned"
            and r.get("reason") == "stale"]


def test_advanced_but_turn_no_longer_compiles_is_stale():
    """§2.1: forward recovery re-runs the checker over the recorded turn. A turn
    the current checker now refuses (here: granted an empty set, so the forward
    to `Ops` is out of the allowlist) is classified stale, never resumed on stale
    authority."""
    from revl.recovery import recover_forward_admissions
    s = _forward_session()
    decided = _decided_record(s)
    # rewrite the bundle to one that will not compile against the base: no grant.
    decided["turn"] = {"sources": {"<turn>.rvl": _TURN_FORWARD},
                       "granted": [], "modules": {}}
    wal = {"records": [decided,
                       {"record": "admit-applied", "seq": 7, "decisionId": "D1",
                        "observed": {}}]}
    reports = recover_forward_admissions(wal, session=s)
    assert reports[0]["classification"] == "stale"


def test_finalized_decision_is_a_no_op_for_the_scan():
    from revl.recovery import recover_forward_admissions
    s = _forward_session()
    decided = _decided_record(s)
    wal = {"records": [decided,
                       {"record": "admit-applied", "seq": 7, "decisionId": "D1"},
                       {"record": "admit-finalized", "seq": 9, "decisionId": "D1"}]}
    assert recover_forward_admissions(wal, session=s) == []


def test_abandoned_decision_is_settled_and_skipped():
    from revl.recovery import recover_forward_admissions
    s = _forward_session()
    decided = _decided_record(s)
    wal = {"records": [decided,
                       {"record": "admit-abandoned", "seq": 7, "decisionId": "D1",
                        "reason": "plug-failed"}]}
    assert recover_forward_admissions(wal, session=s) == []


def test_estop_ambiguous_after_decided_refuses_to_finalize():
    """§4/§6: an in-flight fenced crossing at the cut leaves the E-Stop's
    `estop-ambiguous` record; forward recovery reads it as the in-flight row,
    reports `ambiguous` and finalizes nothing — one ambiguity vocabulary with the
    E-Stop."""
    from revl.recovery import recover_forward_admissions
    s = _forward_session()
    decided = _decided_record(s)
    wal = {"records": [decided,
                       {"record": "admit-applied", "seq": 7, "decisionId": "D1"},
                       {"record": "estop-ambiguous", "seq": 8}]}
    reports = recover_forward_admissions(wal, session=s, forward=True)
    assert reports[0]["classification"] == "ambiguous"
    assert reports[0]["finalized"] is False


def test_recover_reports_admissions_and_scan_is_noop_without_admits(tmp_path):
    """End to end through `recover()`: an activation-complete WAL rolls forward,
    and the forward-admission scan rides both branches. A WAL with no
    `admit-decided` gets no `admissions` key (byte-identical to today)."""
    import sys as _sys
    _sys.path.insert(0, str(_BACKEND))
    from replay import WriteAheadLog
    from revl.recovery import recover
    # no admits: report carries no `admissions` key.
    plain = str(tmp_path / "plain.wal")
    w = WriteAheadLog(plain).open()
    w.record_boundary("C", "x", resource="file:/tmp/z",
                      inverse_op={"receiver": "fs", "method": "rm",
                                  "args": ["/tmp/z"]})
    w.commit_activation(components=["C"])
    w.close()
    report = recover(plain)
    assert "admissions" not in report

    # with an un-finalized decision: recover surfaces the classification.
    s = _forward_session()
    withadmit = str(tmp_path / "admit.wal")
    w = WriteAheadLog(withadmit).open()
    w.record_admit_decided(
        decision_id="D1", turn=_TURN_BUNDLE,
        expected=_decided_record(s)["expected"], spends=["r1"],
        components=["TurnComp"], keys=["turn"])
    w.commit_activation(components=["Agent"])
    w.close()
    report = recover(withadmit, session=s, forward_admissions=False)
    assert report["admissions"][0]["classification"] == "owed"
