"""Item 309: idempotent inverse verification.

Covers the four surfaces the design (docs/design/309-idempotent-inverse.md) makes
first-class:

* the SURFACE + IR + register ledger (`undo idempotent`, `idempotent(key: p)`,
  the hard refusals, the `register` carried onto the IR);
* the VALUE-AWARE fault sweep (a delta/refund-shaped inverse falsely declared
  idempotent is caught by the double-undo round; a restore-to-recorded-value one
  passes; the value-blind fold is the negative control);
* the ABORT FENCE + recovery FREE vs FENCED replay (a declared-idempotent inverse
  replays freely across two recovery passes; an undeclared one is fenced and
  deferred, at-most-once across abort-then-crash);
* the register PARTIAL ORDER helper (keyed and shape-proven are peers, either
  satisfies a strong floor).

The 413 WAL integrity gates and the 247 compensation policy are untouched: the
existing test files for those pass unchanged (additivity).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import replay  # noqa: E402
from revl import fault as fault_mod  # noqa: E402
from revl.lower import (  # noqa: E402
    RevlError,
    _register_satisfies,
    check_and_lower,
)
from revl.parser import Parser  # noqa: E402
from revl.recovery import recover  # noqa: E402


def _lower(src: str) -> dict:
    return check_and_lower(Parser(src, "t.rvl").parse())


# ------------------------------------------------------------ surface + IR + register

_PRE = (
    "type W = { path: Str }\n"
    "type E = { msg: Str }\n"
    "extern pure fn restore(w: W) -> Unit = @py { pass }\n"
)


def test_undo_idempotent_parses_and_carries_the_register():
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
        "    undo idempotent restore(result)\n"
        "    = @py { pass }\n"))
    [rm] = [e for e in ir["externs"] if e["name"] == "rm"]
    assert rm["undo_idempotent"] is True
    assert rm["register"] == "declared"


def test_keyed_emission_parses_and_registers_keyed():
    ir = _lower(
        "extern emission[inv] idempotent(key: reservation_id) "
        "fn release(reservation_id: Str) -> Unit = @py { pass }\n")
    [rel] = [e for e in ir["externs"] if e["name"] == "release"]
    assert rel["idempotent"] is True
    assert rel["idempotency_key"] == "reservation_id"
    assert rel["register"] == "keyed"


def test_bare_idempotent_emission_registers_declared():
    ir = _lower(
        "extern emission[http] idempotent fn put(k: Str, v: Str) -> Unit "
        "= @py { pass }\n")
    [put] = [e for e in ir["externs"] if e["name"] == "put"]
    assert put["idempotent"] is True
    assert "idempotency_key" not in put
    assert put["register"] == "declared"


def test_a_keyed_inverse_is_refused_per_243_rule_3():
    # `idempotent(key:)` is emission-only; a witnessed reversal is a non-emission
    # INVERSE (243 rule 3), so the keyed form on it is refused.
    with pytest.raises(RevlError) as ei:
        _lower(_PRE + (
            "extern witnessed[fs] idempotent(key: path) fn rm(path: Str) "
            "-> Result[W, E]\n"
            "    undo restore(result)\n"
            "    = @py { pass }\n"))
    assert "cannot be declared `idempotent`" in str(ei.value)


def test_idempotent_key_must_name_a_parameter():
    with pytest.raises(RevlError) as ei:
        _lower("extern emission[x] idempotent(key: nope) fn f(a: Str) -> Unit "
               "= @py { pass }\n")
    assert "not\n  one of its parameters" in str(ei.value) or "is not" in str(ei.value)


def test_idempotent_key_must_be_scalar_serializable():
    with pytest.raises(RevlError) as ei:
        _lower("extern emission[x] idempotent(key: p) fn f(p: Pool) -> Unit "
               "= @py { pass }\n")
    assert "scalar-serializable" in str(ei.value)


def test_idempotent_on_a_non_emission_classification_is_refused():
    with pytest.raises(RevlError) as ei:
        _lower(_PRE + (
            "extern acquire idempotent fn open() -> Handle "
            "undo restore(result) = @py { pass }\n"))
    assert "cannot be declared `idempotent`" in str(ei.value)


def test_a_program_using_none_of_it_carries_no_new_ir_keys():
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
        "    undo restore(result)\n"
        "    = @py { pass }\n"))
    [rm] = [e for e in ir["externs"] if e["name"] == "rm"]
    for k in ("undo_idempotent", "idempotent", "idempotency_key", "register"):
        assert k not in rm


# ------------------------------------------------------------ value-aware sweep


def test_delta_shaped_inverse_falsely_declared_idempotent_is_caught():
    # a refund/increment inverse: re-applying moves the value again.
    delta_ops = [{"receiver": "ledger", "method": "credit_refund",
                  "args": ["acct#1"], "value": 100}]
    ok, detail, final, final2 = fault_mod._double_undo_round(delta_ops)
    assert ok is False
    assert final != final2
    assert "delta-shaped" in detail


def test_restore_to_recorded_value_inverse_passes_the_double_undo_round():
    restore_ops = [{"receiver": "fs", "method": "restore", "args": ["/tmp/x"],
                    "value": {"path": "/tmp/x", "bytes": 7}}]
    ok, detail, final, final2 = fault_mod._double_undo_round(restore_ops)
    assert ok is True
    assert final == final2


def test_value_blind_fold_is_the_negative_control():
    # the SAME delta fixture that the value-aware round catches passes the
    # referent-set fingerprint (the reason the digest extension exists).
    blind_once = fault_mod._outstanding(["l#1.new", "l#1.insert acct#1"])
    blind_twice = fault_mod._outstanding(
        ["l#1.new", "l#1.insert acct#1", "l#1.insert acct#1"])
    assert blind_once["keys"] == blind_twice["keys"]  # value-blind: no divergence


def test_valued_fold_carries_the_digest_and_is_backward_compatible():
    # value-carrying line -> digest recorded
    valued = fault_mod._outstanding_valued(["m#1.new", "m#1.insert k = deadbeef"])
    assert valued["digests"] == {"m#1": {"k": "deadbeef"}}
    # bare pre-309 line -> no digest, key still folded, value-blind fold intact
    assert fault_mod._outstanding(["m#1.new", "m#1.insert k = deadbeef"])["keys"] \
        == {"m#1": ["k"]}


def test_double_undo_facet_is_carried_in_the_roundtrip_dossier():
    dossier = fault_mod._roundtrip_dossier([], rounds=4)
    assert dossier["doubleUndo"]["available"] is True
    assert "external-emission divergence out of reach" in dossier["doubleUndo"]["scope"]


# ------------------------------------------------------------ recovery: free vs fenced


def _witnessed_wal(path: str, *, declared: bool, discharge: bool = False,
                   prefence: bool = False) -> None:
    """A witnessed `db.insert(row#1)` whose inverse is `db.delete(row#1)`,
    optionally declared idempotent, optionally with a durable discharge (commit)
    and/or a pre-pinned abort-Phase-1 fence. Never writes `activation-complete`
    (the process 'crashed')."""
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    rec = wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#1"],
        origin={"key": "db", "method": "insert", "args": ["row#1"],
                "site": "svc.rvl:9"},
        witness={"row": "row#1"},
        undo_idempotent=declared,
        register="declared" if declared else None)
    if prefence:
        # the in-process abort fenced-and-applied its Phase-1 undeclared inverse,
        # then the process died before discharge (the headline scenario).
        wal.record_fence(rec["seq"])
    if discharge:
        wal.record_discharge([rec["seq"]])
    wal.close()


def test_declared_idempotent_inverse_replays_freely_across_two_passes(tmp_path):
    path = str(tmp_path / "declared.wal")
    _witnessed_wal(path, declared=True)

    first = recover(path)
    assert first["verdict"] == "rolled-back"
    assert first["residue"]["clean"] is True
    [entry] = first["transactionalRolledBack"]
    assert entry["replay"] == "free"
    assert entry["referent"] == "db:row#1"
    # no fence was written for a declared inverse (a payoff of declaring)
    reread = replay.WriteAheadLog.read(path)
    assert not any(r.get("record") == "replay-fence" for r in reread["records"])

    # a SECOND recovery pass re-applies FREELY and the world is stable: recover
    # is idempotent over the declared subset.
    second = recover(path)
    assert second["residue"]["clean"] is True
    [entry2] = second["transactionalRolledBack"]
    assert entry2["replay"] == "free"


def test_abort_then_crash_undeclared_inverse_is_fenced_not_re_applied(tmp_path):
    # the headline: an in-process abort applied the undeclared inverse in Phase 1
    # (its fence written+fsync'd first), then the process died before discharge.
    path = str(tmp_path / "abort.wal")
    _witnessed_wal(path, declared=False, prefence=True)

    report = recover(path)
    assert report["verdict"] == "rolled-back"
    # NOT re-applied: no transactional roll-back for the fenced seq
    assert report["transactionalRolledBack"] == []
    [fenced] = report["fencedDeferred"]
    assert fenced["referent"] == "db:row#1"
    assert report["residue"]["clean"] is False
    [res] = report["residue"]["outstanding"]
    assert res["kind"] == "fenced-residue"
    assert "outcome unknown, will not re-run" in res["error"]["message"]


def test_undeclared_inverse_is_fenced_after_its_single_attempt(tmp_path):
    # no abort ran: the FIRST recovery run takes the single at-most-once attempt
    # (writing the fence first), the SECOND run finds the fence and refuses.
    path = str(tmp_path / "undeclared.wal")
    _witnessed_wal(path, declared=False)

    first = recover(path)
    [entry] = first["transactionalRolledBack"]
    assert entry["replay"] == "fenced"
    # the fence is now durable on the WAL
    reread = replay.WriteAheadLog.read(path)
    assert any(r.get("record") == "replay-fence" for r in reread["records"])

    second = recover(path)
    assert second["transactionalRolledBack"] == []
    [fenced] = second["fencedDeferred"]
    assert fenced["referent"] == "db:row#1"
    assert second["residue"]["clean"] is False


def test_a_committed_transaction_is_still_never_rolled_back(tmp_path):
    # the central pre-309 safety claim is untouched by the fence.
    path = str(tmp_path / "committed.wal")
    _witnessed_wal(path, declared=False, discharge=True)
    report = recover(path)
    assert report["transactionalRolledBack"] == []
    [skipped] = report["dischargedSkipped"]
    assert skipped["retained"] is True


# ------------------------------------------------------- recovery: legacy boundary family
#
# The same free-vs-fenced contract as above, but for the OTHER record family
# `_roll_back` walks: the reconstructible legacy-boundary path
# (`WriteAheadLog.record_boundary`, the `effect`/`inverse.reconstructible`
# records a durable-resource acquire writes). Before this fix `_roll_back`
# re-applied a `record_boundary` inverse on EVERY `recover()` run, unfenced,
# reporting `clean: true` each time — a HIGH-severity double-apply. These
# tests are the regression coverage: FAILING before the fence was extended to
# this family, PASSING after.


def _boundary_wal(path: str, *, declared: bool) -> None:
    """A durable `PaymentRow` acquire whose inverse is `billing.refund(order,
    amount)` — a non-idempotent (delta-shaped) inverse, optionally declared
    idempotent. Never writes `activation-complete` (the process 'crashed')."""
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_boundary(
        "Billing", "charge order-42", resource="PaymentRow",
        inverse_op={"receiver": "billing", "method": "refund",
                    "args": ["order-42", 100]},
        undo_idempotent=declared,
        register="declared" if declared else None)
    wal.close()


def test_undeclared_boundary_inverse_applies_once_then_fenced(tmp_path):
    # BEFORE the fix: three `recover()` runs each re-ran `billing.refund`,
    # every run reporting `clean: true` — an unbounded double-apply. AFTER:
    # the first run takes the single at-most-once attempt, every run after
    # that refuses to re-run it and reports fenced/deferred residue.
    path = str(tmp_path / "boundary-undeclared.wal")
    _boundary_wal(path, declared=False)

    first = recover(path)
    assert first["verdict"] == "rolled-back"
    [entry] = first["ran"]
    assert entry["op"]["method"] == "refund"
    assert entry["replay"] == "fenced"
    assert first["residue"]["clean"] is True
    assert first["fencedDeferred"] == []
    # the fence is now durable on the WAL
    reread = replay.WriteAheadLog.read(path)
    assert any(r.get("record") == "replay-fence" for r in reread["records"])

    for _ in range(2):
        again = recover(path)
        assert again["verdict"] == "rolled-back"
        assert again["ran"] == []  # NOT re-applied
        [fenced] = again["fencedDeferred"]
        assert fenced["referent"] is not None
        assert again["residue"]["clean"] is False
        [res] = again["residue"]["outstanding"]
        assert res["kind"] == "fenced-residue"
        assert "outcome unknown, will not re-run" in res["error"]["message"]


def test_declared_idempotent_boundary_inverse_replays_freely(tmp_path):
    path = str(tmp_path / "boundary-declared.wal")
    _boundary_wal(path, declared=True)

    for _ in range(3):
        report = recover(path)
        assert report["verdict"] == "rolled-back"
        [entry] = report["ran"]
        assert entry["op"]["method"] == "refund"
        assert entry["replay"] == "free"
        assert report["residue"]["clean"] is True
        assert report["fencedDeferred"] == []

    # a declared-idempotent inverse never needs a fence (a payoff of declaring)
    reread = replay.WriteAheadLog.read(path)
    assert not any(r.get("record") == "replay-fence" for r in reread["records"])


# ------------------------------------------------------------ register partial order


_WITNESSED = (
    "type Stash = { path: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py { pass }\n"
    "extern witnessed[fs] fn stash() -> Result[Stash, FsError] "
    "undo __KW__unstash(result) = @py { pass }\n"
    "component C { effect stash() }\n"
)


def _teardown_audit(declared: bool):
    from revl import compile_source
    from revl.audit_diff import audit_report
    src = _WITNESSED.replace("__KW__", "idempotent " if declared else "")
    return audit_report(compile_source(src))


def test_requires_idempotent_teardown_refuses_a_fenced_inverse():
    from revl.policy import evaluate, parse_policy
    audit = _teardown_audit(declared=False)
    assert audit["recovery_surface"] == [
        {"name": "stash", "kind": "inverse", "register": None}]
    violations = evaluate(parse_policy("requires idempotent-teardown"), audit)
    assert any(v.kind == "teardown" and v.token == "stash" for v in violations)


def test_requires_idempotent_teardown_admits_a_declared_inverse():
    from revl.policy import evaluate, parse_policy
    audit = _teardown_audit(declared=True)
    assert evaluate(parse_policy("requires idempotent-teardown"), audit) == []


def test_idempotent_teardown_strength_floor_refuses_declared_only():
    from revl.policy import evaluate, parse_policy
    audit = _teardown_audit(declared=True)  # register is only `declared`
    pol = parse_policy("requires idempotent-teardown(strength: keyed)")
    assert any(v.kind == "teardown" for v in evaluate(pol, audit))


def test_register_partial_order():
    # declared is the floor; keyed and shape-proven are strong peers.
    assert _register_satisfies("declared", "declared")
    assert _register_satisfies("keyed", "declared")
    assert _register_satisfies("shape-proven", "declared")
    # a strong floor is satisfied by either strong peer, never by declared.
    assert _register_satisfies("keyed", "keyed")
    assert _register_satisfies("shape-proven", "keyed")
    assert _register_satisfies("keyed", "shape-proven")
    assert not _register_satisfies("declared", "keyed")
    assert not _register_satisfies("declared", "shape-proven")
    # no claim never satisfies a floor.
    assert not _register_satisfies(None, "declared")


# ------------------------------------------------- slice 4: shape-proven is a
# ------------------------------------------------- tier NOTHING produces

def test_shape_proven_is_a_tier_no_declaration_can_reach_today():
    """The tripwire for 309 slice 4 (`lower.py::_idempotent_register`).

    `shape-proven` is in the partial order and in `REDISPATCH_FREE`, so the
    lattice ACCEPTS it — but the check that would produce it is a syntactic rule
    over a restore-to-recorded-value inverse BODY, and revl has no body to read:
    `parser.py::_extern` requires every extern to carry a `@backend` host body,
    so an inverse is G8-opaque by construction. Until an extern can carry a
    revl-expressed body, every native `undo idempotent` must lower to `declared`
    — the fail-closed direction.

    This test fails the day a declaration produces `shape-proven`. That is the
    point: it is the reminder that the promise now has to be kept for real
    (`REDISPATCH_FREE` lets recovery re-dispatch it with no fence), not a claim
    that the tier should stay empty forever.
    """
    from revl.lower import REDISPATCH_FREE, _REGISTER_RANK

    # the tier exists in the type ...
    assert "shape-proven" in _REGISTER_RANK
    assert "shape-proven" in REDISPATCH_FREE

    # ... and in the world, nothing reaches it. Every declaration surface that
    # produces a register at all, over the shapes 309 admits.
    surfaces = [
        # a native `undo idempotent` over a host body — the slice-4 candidate.
        _PRE + ("extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
                "    undo idempotent restore(result)\n"
                "    = @py { pass }\n"),
        # the same on an `acquire` extern (item 308 R0: an acquire returns an
        # opaque nominal handle, so the inverse takes THAT).
        ("type Handle = { fd: Int }\n"
         "extern pure fn shut(h: Handle) -> Unit = @py { pass }\n"
         "extern acquire fn open(path: Str) -> Handle\n"
         "    undo idempotent shut(result)\n"
         "    = @py { pass }\n"),
        # the read tier (item 440) — stronger than shape-proven, still not it.
        _PRE + ("extern witnessed[fs] fn probe(path: Str) -> Result[W, E]\n"
                "    undo pure restore(result)\n"
                "    = @py { pass }\n"),
        # the two emission surfaces.
        "extern emission[inv] idempotent(key: k) fn release(k: Str) -> Unit "
        "= @py { pass }\n",
        "extern emission[http] idempotent fn put(k: Str) -> Unit = @py { pass }\n",
    ]
    produced = set()
    for src in surfaces:
        for ext in _lower(src)["externs"]:
            if "register" in ext:
                produced.add(ext["register"])
    assert produced == {"declared", "read", "keyed"}
    assert "shape-proven" not in produced


def test_a_pure_classified_inverse_does_not_earn_shape_proven():
    """The unsoundness slice 4 must not take, stated as a test.

    `pure` is checked for SHAPE only — `stdlib/fs.rvl`'s `restore`, `unrm` and
    `rmdir_if_empty` are all `extern pure fn` and all unlink or rename real
    files. So "the inverse is classified pure" cannot be read as "the inverse
    restores a recorded value": deriving the tier from the classification would
    promote mutating inverses into the free-replay tier, which is unsound in the
    UNSAFE direction (item 440 found the same trap for the `read` tier and
    anchored it to an explicit `undo pure` word instead).
    """
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
        "    undo idempotent restore(result)\n"   # `restore` IS `extern pure`
        "    = @py { pass }\n"))
    [rm] = [e for e in ir["externs"] if e["name"] == "rm"]
    assert rm["register"] == "declared"
