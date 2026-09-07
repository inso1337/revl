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


# --------------------------------------------------------------------------- #
# Issue #644: a FAILED admission must DISPOSE the turn's already-plugged fibers
# through the supported `fiber.dispose()` path BEFORE it records the terminal
# `admit-abandoned {plug-failed}`. The #619 helper looked up a non-existent
# `driver._dispose_fiber`, so disposal was a silent no-op — the abandonment was
# written on disk while every plugged fiber (and its live provisions/effects)
# leaked, unreachable to ordinary IR-ordered disposal since the un-adopted turn
# never entered the composition IR. This needs a live cordis composition (the
# fiber, its `dispose()`, and the driver's `_flush`), so it is cordis-gated.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_failed_admission_disposes_turn_fibers_before_recording_abandonment(
        tmp_path):
    from revl.wal import read_wal

    session = _gated_session(tmp_path)
    driver = session._driver
    runtime_mod = driver.runtime

    events: list = []          # ordered log: ("dispose", probe) / ("abandon",)
    probes: list = []          # every fiber the plug actually stored

    # 1. Wrap each fiber the plug stores so we can observe its REAL `dispose()`
    #    being invoked — the whole bug is that it never was.
    real_plug = runtime_mod.plug

    class _FiberProbe:
        def __init__(self, inner):
            self._inner = inner
            self.disposed = False

        @property
        def state(self):
            return self._inner.state

        def __await__(self):
            return self._inner.__await__()

        def dispose(self):
            self.disposed = True
            events.append(("dispose", self))
            return self._inner.dispose()

        def __getattr__(self, key):
            return getattr(self._inner, key)

    def _probing_plug(ctx, component, config=None):
        probe = _FiberProbe(real_plug(ctx, component, config))
        probes.append(probe)
        return probe

    # 2. Spy the terminal-abandonment write so we can assert it lands AFTER the
    #    fibers are disposed (same instance `_wire_turn`'s `wal` resolves to).
    wal = session._approval_wal()
    real_abandoned = wal.record_admit_abandoned

    def _spy_abandoned(*a, **k):
        events.append(("abandon",))
        return real_abandoned(*a, **k)

    # 3. Force the plug to FAIL right after the first fiber is stored: raise on
    #    the first flush that runs once a fiber has been plugged (the in-plug
    #    flush), then let teardown's own flushes run for real. The pre-plug gates
    #    flush with `probes` still empty, so only the in-plug flush trips.
    real_flush = driver._flush
    tripped = {"done": False}

    async def _flaky_flush():
        if probes and not tripped["done"]:
            tripped["done"] = True
            raise RuntimeError("injected plug/flush failure (issue #644 repro)")
        return await real_flush()

    runtime_mod.plug = _probing_plug
    wal.record_admit_abandoned = _spy_abandoned
    driver._flush = _flaky_flush
    try:
        with pytest.raises(RuntimeError, match="injected plug/flush failure"):
            session.admit(_TURN_FORWARD, granted=["Ops"])
    finally:
        runtime_mod.plug = real_plug
        driver._flush = real_flush
        try:
            del wal.record_admit_abandoned
        except AttributeError:
            pass

    # the repro actually reached the plug and stored a fiber.
    assert probes, "no fiber was plugged — the repro never reached the plug"

    # (a) the turn's already-plugged fiber was disposed through the supported
    #     path (with the bug, `dispose()` was never called at all).
    assert any(p.disposed for p in probes), \
        "a plugged turn fiber was never disposed (issue #644 leak)"

    # (b) disposal happened BEFORE the terminal abandonment was recorded.
    assert ("abandon",) in events, "admit-abandoned was never recorded"
    first_abandon = events.index(("abandon",))
    assert any(e[0] == "dispose" and i < first_abandon
               for i, e in enumerate(events)), \
        "admit-abandoned was recorded before the turn's fibers were disposed"

    # (c) the disposed fiber is gone from the driver — not stranded.
    assert "TurnComp" not in driver.fibers, \
        "a disposed turn fiber was left stranded in driver.fibers"

    # (d) the honest terminal record is on disk, with its reason.
    on_disk = read_wal(session._wal_path)["records"]
    assert [r for r in on_disk if r.get("record") == "admit-abandoned"
            and r.get("reason") == "plug-failed"], \
        "no admit-abandoned {plug-failed} record was written"


# --------------------------------------------------------------------------- #
# Issue #644, disposal helper in isolation. `_dispose_turn_fibers` is the seam
# the whole fix turns on, and its contract holds without a live cordis
# composition: driven over a fabricated driver whose fibers expose the runtime's
# real `dispose()`/`_flush` shape, it must invoke `dispose()` in reverse plug
# order, WITHDRAW each disposed fiber from `driver.fibers`, RETAIN (never drop) a
# fiber whose `dispose()` raises, and RETURN those retained names so `_wire_turn`
# can withhold the terminal settlement while cleanup is unresolved. These run
# everywhere (no cordis needed) — the end-to-end WAL honesty is proven by the
# cordis-gated cases below.
# --------------------------------------------------------------------------- #

class _FakeFiber:
    """A stand-in for a driver fiber: a real awaitable `dispose()` that records
    the order it is torn down in, and optionally raises to model a disposal that
    cannot resolve."""

    def __init__(self, name, log, fail=False):
        self.name = name
        self._log = log
        self._fail = fail
        self.disposed = False

    async def dispose(self):
        self._log.append(("dispose", self.name))
        if self._fail:
            raise RuntimeError(f"injected dispose failure for {self.name}")
        self.disposed = True
        self._log.append(("disposed", self.name))


class _FakeDriver:
    def __init__(self):
        self.fibers = {}
        self.flushes = 0
        self.events = []

    async def _flush(self):
        self.flushes += 1

    def _log(self, channel, subject, detail=""):
        self.events.append((channel, subject, detail))


def _session_over_driver(driver):
    from revl.mcp.session import Session
    s = Session()
    s._driver = driver
    return s


def test_dispose_turn_fibers_disposes_in_reverse_plug_order_and_withdraws():
    log = []
    driver = _FakeDriver()
    # plug (insertion) order: the provider is plugged before the consumer.
    driver.fibers["Provider"] = _FakeFiber("Provider", log)
    driver.fibers["Consumer"] = _FakeFiber("Consumer", log)
    s = _session_over_driver(driver)

    retained = s._dispose_turn_fibers({"Provider", "Consumer"})

    assert retained == set(), "clean disposal must report nothing retained"
    # every disposed fiber is withdrawn — none stranded in the driver.
    assert driver.fibers == {}, "disposed fibers were not withdrawn"
    # consumers before providers: reverse of plug order.
    disposed = [name for tag, name in log if tag == "disposed"]
    assert disposed == ["Consumer", "Provider"], \
        "fibers were not torn down in reverse plug (LIFO) order"
    assert driver.flushes >= 2, "each teardown must flush the driver"


def test_dispose_turn_fibers_retains_a_fiber_whose_dispose_raises():
    log = []
    driver = _FakeDriver()
    driver.fibers["Provider"] = _FakeFiber("Provider", log, fail=True)
    driver.fibers["Consumer"] = _FakeFiber("Consumer", log)
    s = _session_over_driver(driver)

    retained = s._dispose_turn_fibers({"Provider", "Consumer"})

    # the fiber that could not be torn down is reported AND kept inspectable.
    assert retained == {"Provider"}, "a failed disposal was not reported"
    assert "Provider" in driver.fibers, \
        "a fiber that failed to dispose was silently dropped (issue #644)"
    assert driver.fibers["Provider"]._fail, "the retained fiber was replaced"
    # the healthy sibling is still torn down and withdrawn.
    assert "Consumer" not in driver.fibers
    # retention is explicit, not silent.
    assert any(subject == "dispose-failed" for _c, subject, _d in driver.events), \
        "a retained undisposed fiber was not logged"


def test_dispose_turn_fibers_only_touches_the_turns_own_names():
    log = []
    driver = _FakeDriver()
    driver.fibers["Base"] = _FakeFiber("Base", log)          # not this turn's
    driver.fibers["TurnComp"] = _FakeFiber("TurnComp", log)  # this turn's
    s = _session_over_driver(driver)

    retained = s._dispose_turn_fibers({"TurnComp"})

    assert retained == set()
    assert "Base" in driver.fibers, "a non-turn fiber was disposed"
    assert "TurnComp" not in driver.fibers
    assert [name for tag, name in log if tag == "disposed"] == ["TurnComp"]


def test_dispose_turn_fibers_without_a_driver_is_a_noop():
    from revl.mcp.session import Session
    s = Session()
    assert s._driver is None
    assert s._dispose_turn_fibers({"X"}) == set()


# --------------------------------------------------------------------------- #
# Issue #644, end to end: the two failure shapes the report names. A partial
# plug whose DISPOSAL then fails must retain the fiber and keep the WAL honest
# (no terminal `admit-abandoned` while cleanup is unresolved); an activation that
# exceeds its explicit timeout must still be disposed through the supported path
# before the honest abandonment is recorded. Both need a live cordis composition.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_failed_admission_retains_fiber_and_withholds_abandonment_when_cleanup_fails(
        tmp_path):
    """Cleanup is UNRESOLVED: a turn fiber is plugged, the plug fails, and the
    fiber's own `dispose()` then raises. The fiber must be RETAINED (inspectable,
    not dropped as if disposed), NO terminal `admit-abandoned` may be written
    while it still leaks (recovery would skip a terminal decision), and the
    cleanup failure must be made explicit on the preserved admission error."""
    from revl.wal import read_wal

    session = _gated_session(tmp_path)
    driver = session._driver
    runtime_mod = driver.runtime

    probes: list = []
    real_plug = runtime_mod.plug

    class _FailingDisposeProbe:
        def __init__(self, inner):
            self._inner = inner
            self.dispose_attempted = False

        @property
        def state(self):
            return self._inner.state

        def __await__(self):
            return self._inner.__await__()

        def dispose(self):
            self.dispose_attempted = True

            async def _boom():
                # the supported path was reached, but the teardown itself cannot
                # resolve — the fiber stays live.
                raise RuntimeError("injected dispose failure (issue #644 repro)")

            return _boom()

        def __getattr__(self, key):
            return getattr(self._inner, key)

    def _probing_plug(ctx, component, config=None):
        probe = _FailingDisposeProbe(real_plug(ctx, component, config))
        probes.append(probe)
        return probe

    # fail the plug right after the first fiber is stored (same repro shape as the
    # test above): the pre-plug gates flush with `probes` empty; the in-plug flush
    # trips once a fiber has been plugged.
    real_flush = driver._flush
    tripped = {"done": False}

    async def _flaky_flush():
        if probes and not tripped["done"]:
            tripped["done"] = True
            raise RuntimeError("injected plug/flush failure (issue #644 repro)")
        return await real_flush()

    runtime_mod.plug = _probing_plug
    driver._flush = _flaky_flush
    try:
        # the ADMISSION failure (the plug/flush error) is preserved and re-raised.
        with pytest.raises(RuntimeError,
                           match="injected plug/flush failure") as caught:
            session.admit(_TURN_FORWARD, granted=["Ops"])
    finally:
        runtime_mod.plug = real_plug
        driver._flush = real_flush

    assert probes, "no fiber was plugged — the repro never reached the plug"
    # disposal WAS attempted through the supported path (it just could not resolve).
    assert any(p.dispose_attempted for p in probes), \
        "the supported dispose() path was never invoked"
    # the fiber that could not be disposed is RETAINED, not silently dropped.
    assert "TurnComp" in driver.fibers, \
        "a fiber that failed to dispose was dropped as if disposed (issue #644)"

    on_disk = read_wal(session._wal_path)["records"]
    # honest WAL: the decision is NOT terminally settled while a fiber still leaks.
    assert not [r for r in on_disk if r.get("record") == "admit-abandoned"], \
        "admit-abandoned was recorded while cleanup was unresolved (issue #644)"
    # the decision stays owed (its admit-decided stands) so recovery still sees it.
    assert [r for r in on_disk if r.get("record") == "admit-decided"], \
        "the decision's admit-decided record is missing"
    # the cleanup failure is explicit on the preserved admission error.
    notes = getattr(caught.value, "__notes__", [])
    assert any("cleanup is unresolved" in n for n in notes), \
        "the unresolved cleanup was not made explicit on the admission error"


@needs_cordis
def test_activation_timeout_disposes_then_records_honest_abandonment(tmp_path):
    """An activating fiber that exceeds the explicit settle timeout raises out of
    the plug. Its already-plugged fiber must be disposed through the supported
    path BEFORE the honest terminal `admit-abandoned {plug-failed}` is recorded,
    and the fiber withdrawn — not stranded."""
    import asyncio

    from revl.wal import read_wal

    session = _gated_session(tmp_path)
    driver = session._driver
    runtime_mod = driver.runtime

    events: list = []
    probes: list = []
    real_plug = runtime_mod.plug

    class _HangingProbe:
        """Reports LOADING so the plug enters its `wait_for(shield(fiber), 2)`
        settle, then never completes — the activation exceeds its deadline."""

        def __init__(self, inner):
            self._inner = inner
            self.dispose_attempted = False

        @property
        def state(self):
            return driver.FiberState.LOADING

        def __await__(self):
            async def _never():
                await asyncio.sleep(30)   # longer than the plug's 2s settle
            return _never().__await__()

        def dispose(self):
            self.dispose_attempted = True
            events.append(("dispose",))
            return self._inner.dispose()

        def __getattr__(self, key):
            return getattr(self._inner, key)

    def _probing_plug(ctx, component, config=None):
        probe = _HangingProbe(real_plug(ctx, component, config))
        probes.append(probe)
        return probe

    wal = session._approval_wal()
    real_abandoned = wal.record_admit_abandoned

    def _spy_abandoned(*a, **k):
        events.append(("abandon",))
        return real_abandoned(*a, **k)

    runtime_mod.plug = _probing_plug
    wal.record_admit_abandoned = _spy_abandoned
    try:
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            session.admit(_TURN_FORWARD, granted=["Ops"])
    finally:
        runtime_mod.plug = real_plug
        try:
            del wal.record_admit_abandoned
        except AttributeError:
            pass

    assert probes, "no fiber was plugged — the repro never reached the plug"
    # (a) the timed-out fiber was disposed through the supported path.
    assert any(p.dispose_attempted for p in probes), \
        "the activation-timeout fiber was never disposed (issue #644 leak)"
    # (b) disposal happened BEFORE the terminal abandonment was recorded.
    assert ("abandon",) in events, "admit-abandoned was never recorded"
    first_abandon = events.index(("abandon",))
    assert any(e == ("dispose",) and i < first_abandon
               for i, e in enumerate(events)), \
        "admit-abandoned was recorded before the timed-out fiber was disposed"
    # (c) the disposed fiber is withdrawn, and the honest record is on disk.
    assert "TurnComp" not in driver.fibers, \
        "a disposed turn fiber was left stranded in driver.fibers"
    on_disk = read_wal(session._wal_path)["records"]
    assert [r for r in on_disk if r.get("record") == "admit-abandoned"
            and r.get("reason") == "plug-failed"], \
        "no honest admit-abandoned {plug-failed} record was written"


# §4: the journal-served plug seam. A fenced crossing recorded complete under a
# decision is SERVED from the journal on a fresh-process re-apply and dispatches
# zero times ("no double-run of a fenced extern", §8); one left in flight at the
# cut refuses to finalize. Pure over the WAL records + the session seam — no
# live runtime, so it runs everywhere the Slice-3 tests do.
# --------------------------------------------------------------------------- #

def _crossing_begin(did, ordinal, receiver="fs", method="write", seq=6):
    return {"record": "admit-crossing", "seq": seq, "phase": "begin",
            "tier": "fenced", "decisionId": did, "ordinal": ordinal,
            "call": {"receiver": receiver, "method": method}}


def _crossing_complete(did, ordinal, outcome, seq=7):
    return {"record": "admit-crossing", "seq": seq, "phase": "complete",
            "tier": "fenced", "decisionId": did, "ordinal": ordinal,
            "outcome": outcome}


def test_fenced_crossing_records_carry_the_decision_and_ordinal(tmp_path):
    """The recording half (§4): a crossing the activation body journals inside a
    decision window carries the `decisionId`, a fenced one also carries an
    `ordinal` and, on completion, its `outcome`. A crossing OUTSIDE any window is
    byte-identical to today's — no `decisionId` key at all."""
    from revl.wal import read_wal
    wal = _wal(tmp_path)
    # outside any window: no decision tag (byte-identity).
    outside = wal.record_boundary(
        "Base", "acquire", resource="file:/tmp/base",
        inverse_op={"receiver": "fs", "method": "rm", "args": ["/tmp/base"]})
    assert "decisionId" not in outside
    # inside the window: tagged, and the fenced crossing gets an ordinal + outcome.
    wal.begin_decision("D1")
    tagged = wal.record_boundary(
        "TurnComp", "acquire", resource="file:/tmp/x",
        inverse_op={"receiver": "fs", "method": "rm", "args": ["/tmp/x"]})
    assert tagged["decisionId"] == "D1"
    o0 = wal.record_fenced_crossing_begin(receiver="fs", method="write")
    o1 = wal.record_fenced_crossing_begin(receiver="net", method="post")
    assert (o0, o1) == (0, 1)   # per-decision ordinals, independent of seq
    comp = wal.record_fenced_crossing_complete(ordinal=o0, outcome={"bytes": 3})
    assert comp["decisionId"] == "D1" and comp["ordinal"] == 0
    wal.end_decision()
    # after the window closes, tagging stops again.
    after = wal.record_boundary(
        "Base", "acquire", resource="file:/tmp/y",
        inverse_op={"receiver": "fs", "method": "rm", "args": ["/tmp/y"]})
    assert "decisionId" not in after
    wal.close()
    got = read_wal(wal.path)["records"]
    crossings = [r for r in got if r.get("record") == "admit-crossing"]
    assert len(crossings) == 3   # two begins, one complete, all readable


def test_fenced_crossing_recorders_refuse_outside_a_decision_window(tmp_path):
    """`record_fenced_crossing_*` require an open window: a fenced crossing has no
    meaning without the decision it is fenced under."""
    from replay import ReplayError
    wal = _wal(tmp_path)
    with pytest.raises(ReplayError):
        wal.record_fenced_crossing_begin(receiver="fs", method="write")
    with pytest.raises(ReplayError):
        wal.record_fenced_crossing_complete(ordinal=0, outcome=None)
    wal.close()


def test_completed_fenced_crossing_is_served_and_finalizes_forward(tmp_path):
    """§4 fenced row, completed: an advanced decision whose fenced crossing is
    recorded COMPLETE is served from the journal and finalized forward. The seam
    dispatches zero times — the guarantee the design's non-vacuity check reads."""
    from revl.recovery import recover_forward_admissions
    from revl.wal import read_wal
    s = _forward_session()
    decided = _decided_record(s)
    wal = {"records": [
        decided,
        _crossing_begin("D1", 0, seq=6),
        _crossing_complete("D1", 0, {"bytes": 3}, seq=7),
        {"record": "admit-applied", "seq": 8, "decisionId": "D1",
         "observed": {"generation": 1, "surfaceEpoch": 2}}]}
    path = str(tmp_path / "served.wal")
    import json as _json
    with open(path, "w", encoding="utf-8") as h:
        h.write(_json.dumps({"record": "header", "walVersion": 1}) + "\n")
    reports = recover_forward_admissions(wal, session=s, forward=True,
                                         wal_path=path)
    assert reports[0]["classification"] == "advanced"
    assert reports[0]["finalized"] is True
    assert reports[0]["served"] == [0]
    # the whole point: a completed fenced crossing dispatched ZERO times.
    assert reports[0]["dispatched"] == 0
    fin = [r for r in read_wal(path)["records"]
           if r.get("record") == "admit-finalized"]
    assert len(fin) == 1 and fin[0]["decisionId"] == "D1"


def test_in_flight_fenced_crossing_refuses_to_finalize(tmp_path):
    """§4 fenced row, in flight: a fenced crossing with a `begin` and no
    `complete` (a plain crash mid-crossing, not only the E-Stop's
    `estop-ambiguous`) reclassifies the advanced decision `ambiguous`. Forward
    recovery finalizes nothing and never re-dispatches the fenced extern."""
    from revl.recovery import recover_forward_admissions
    from revl.wal import read_wal
    s = _forward_session()
    decided = _decided_record(s)
    wal = {"records": [
        decided,
        _crossing_begin("D1", 0, seq=6),   # no matching complete
        {"record": "admit-applied", "seq": 8, "decisionId": "D1",
         "observed": {"generation": 1, "surfaceEpoch": 2}}]}
    path = str(tmp_path / "inflight.wal")
    import json as _json
    with open(path, "w", encoding="utf-8") as h:
        h.write(_json.dumps({"record": "header", "walVersion": 1}) + "\n")
    reports = recover_forward_admissions(wal, session=s, forward=True,
                                         wal_path=path)
    assert reports[0]["classification"] == "ambiguous"
    assert reports[0]["finalized"] is False
    assert reports[0]["inFlight"] == [0]
    assert not [r for r in read_wal(path)["records"]
                if r.get("record") == "admit-finalized"]


def test_journal_served_seam_serves_from_the_record_and_dispatches_zero():
    """The session seam directly (§4): in journal-served mode a fenced crossing
    with a recorded outcome is SERVED (returns the outcome, no dispatch); one with
    no record falls through to a first-run dispatch. The dispatch counter is the
    non-vacuity witness — with nothing served, the same crossing dispatches."""
    s = _forward_session()
    s.begin_journal_served("D1", {0: {"bytes": 3}, 1: {"ok": True}})
    served0, out0 = s.serve_fenced_crossing("fs", "write")
    served1, out1 = s.serve_fenced_crossing("net", "post")
    served2, out2 = s.serve_fenced_crossing("fs", "unknown")  # ordinal 2, unrecorded
    s.end_journal_served()
    assert (served0, out0) == (True, {"bytes": 3})
    assert (served1, out1) == (True, {"ok": True})
    assert served2 is False and out2 is None
    # exactly the one unrecorded crossing dispatched; the two served ones did not.
    assert s._fenced_dispatch_count == 1

    # non-vacuity: with the seam disabled (no served map), the SAME first crossing
    # would dispatch instead of being served.
    s.begin_journal_served("D1", {})
    served, _ = s.serve_fenced_crossing("fs", "write")
    s.end_journal_served()
    assert served is False and s._fenced_dispatch_count == 1


def test_advanced_with_no_fenced_records_finalizes_forward_unchanged(tmp_path):
    """A decision that journalled no fenced crossing finalizes forward exactly as
    before §4: served empty, dispatched zero, `admit-finalized` written."""
    from revl.recovery import recover_forward_admissions
    from revl.wal import read_wal
    s = _forward_session()
    decided = _decided_record(s)
    wal = {"records": [decided,
                       {"record": "admit-applied", "seq": 7, "decisionId": "D1",
                        "observed": {"generation": 1, "surfaceEpoch": 2}}]}
    path = str(tmp_path / "plain.wal")
    import json as _json
    with open(path, "w", encoding="utf-8") as h:
        h.write(_json.dumps({"record": "header", "walVersion": 1}) + "\n")
    reports = recover_forward_admissions(wal, session=s, forward=True,
                                         wal_path=path)
    assert reports[0]["classification"] == "advanced"
    assert reports[0]["served"] == [] and reports[0]["dispatched"] == 0
    assert reports[0]["finalized"] is True
    assert [r for r in read_wal(path)["records"]
            if r.get("record") == "admit-finalized"]