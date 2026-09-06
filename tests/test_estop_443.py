"""The operator E-Stop — roadmap item 443 (`docs/design/443-estop.md`).

Every other stop revl has is COOPERATIVE: teardown replays inverses LIFO under
the activation's verdict, faults route through residue records, withdrawal
propagates to dependents. That is right for a composition fault and wrong for
an operator emergency, where unwinding two hundred brackets first is not a safe
answer but a long one.

The hard part is the honest accounting, not the halt, so that is what this
suite pins:

  * the halt REFUSES new crossings, at every seam that dispatches or accepts
    one (`plug`, acquire, witnessed, compensate, deferred, approval);
  * it replays NOTHING and discharges NOTHING — the runtime halves of
    `RevL.G7.estop_replays_nothing` / `estop_discharges_nothing`;
  * every registered entry lands on the inventory as `estop-stranded`
    (registered, never attempted, still owed) rather than being dropped;
  * a crossing that was already DISPATCHED lands as `estop-ambiguous` with
    `outcome: "unknown"` — item 440's tier, created deliberately — and never
    as stranded, because stranding means "never attempted";
  * it is an OPERATOR authority: no token, no halt, and no in-language surface;
  * the cross-process latch engages the halt, and a malformed latch still
    halts (failing open on an emergency stop is the one failure mode this
    feature exists to prevent);
  * `revl estop` arms / reports / clears, is idempotent, and never reports
    clean.
"""

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import runtime as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_halt():
    """No halt leaks between tests.

    The halt IS process-global by design — an E-Stop that only stopped one
    activation would not be a stop — so the live-frame registry has to be
    reset here too, or one test's frames land on the next test's inventory."""
    def _reset():
        rt.clear_estop()
        rt.arm_estop_latch(None)
        rt._LIVE_FRAMES.clear()
    _reset()
    yield
    _reset()


class _Ctx:
    """The minimum a `Frame` reads off its context here: no timeline, so no
    WAL, so the entries carry `seq is None` — which is the shape a run without
    `--wal` really has."""


def _frame(name="Agent"):
    return rt.Frame(_Ctx(), name)


# -- the authority ---------------------------------------------------------


def test_estop_is_an_operator_authority_not_a_composition_verb():
    """A composition may not halt itself — that is the whole point (item 443).

    The primary enforcement is that there is no in-language surface at all;
    this refusal is the defensive twin for an embedding that reaches the module
    function directly."""
    with pytest.raises(rt.EstopRefused):
        rt.estop("I would like to stop")
    with pytest.raises(rt.EstopRefused):
        rt.estop("still no", operator="")
    assert rt.estop_state() is None
    assert not rt.estop_engaged()


def test_estop_is_not_reachable_from_the_language_surface():
    """No extern, no stdlib binding, nothing an `.rvl` body can name."""
    stdlib = _ROOT / "stdlib"
    hits = [p.name for p in stdlib.rglob("*.rvl")
            if "estop" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_estop_is_idempotent_and_keeps_the_first_reason():
    """Hitting the button twice is not two halts, and the second press must not
    overwrite the first one's accountability."""
    first = rt.estop("runaway loop", operator="alice")
    second = rt.estop("something else", operator="mallory")
    assert first["reason"] == second["reason"] == "runaway loop"
    assert second["operator"] == "alice"


# -- the guarantee: no new crossing dispatches ------------------------------


def test_halt_refuses_a_new_activation():
    rt.estop("class-(c) storm", operator="alice")
    with pytest.raises(rt.EstopHalted) as caught:
        rt.plug(None, {"name": "Payments"})
    assert "E-STOP engaged" in str(caught.value)
    assert "class-(c) storm" in str(caught.value)
    assert caught.value.halt["operator"] == "alice"


def test_halt_refuses_every_registration_seam():
    """Registration IS the crossing for a witnessed mutation and a
    compensation: the emitted call site yields them after the effect landed."""
    frame = _frame()
    rt.estop("halt", operator="alice")
    for call in (
        lambda: frame.acquire("open", lambda: object(), lambda h: None),
        lambda: frame.transactional(lambda w: None, {"path": "/tmp/x"}),
        lambda: frame.transactional_method(lambda w: None, {"path": "/tmp/x"}),
        lambda: frame.compensation(lambda: None),
        lambda: frame.compensation_method(lambda: None),
        lambda: frame.enqueue_deferred("mail", "send", [], lambda: None),
        lambda: frame.request_approval("payments", {}),
        lambda: frame.approval_crossing({}, "payments", lambda: None),
    ):
        with pytest.raises(rt.EstopHalted):
            call()


def test_a_deferred_emission_is_refused_rather_than_flushed():
    fired = []
    deferred = rt._Deferred("mail", "send", [], lambda: fired.append(1))
    rt.estop("halt", operator="alice")
    with pytest.raises(rt.EstopHalted):
        deferred.fire()
    assert fired == []


def test_nothing_is_armed_by_default():
    """A composition that never arms a latch is byte-identical to the pre-443
    runtime: no halt, no latch path, and the seams cost nothing."""
    assert rt.estop_latch_path() is None
    assert not rt.estop_engaged()
    frame = _frame()
    entry = frame.transactional(lambda w: None, {"ok": True})
    assert entry in frame._transactional


# -- the accounting --------------------------------------------------------


def test_registered_entries_are_stranded_not_discharged():
    """`RevL.G7.estop_strands_everything`, at the runtime: every registered
    entry is on the inventory, owed, and none of them is dropped."""
    frame = _frame("Ops")
    frame.transactional(lambda w: None, {"row": 1})
    frame.compensation(lambda: None)
    halt = rt.estop("runaway loop", operator="alice")

    kinds = {r["kind"] for r in halt["stranded"]}
    assert kinds == {"estop-stranded"}
    assert {r["entry"] for r in halt["stranded"]} == {"transactional",
                                                      "compensation"}
    for record in halt["stranded"]:
        assert record["outcome"] == "not-attempted"
        assert record["attemptedFlag"] is False
        assert record["attempted"] is None
        assert record["state"] == "unresolved"
        assert record["error"]["type"] == "estop"
        assert "runaway loop" in record["error"]["message"]
    assert halt["activations"] == [{"component": "Ops", "stranded": 2}]
    assert halt["verdict"] == "halted"
    assert halt["resumable"] is False


def test_the_halt_replays_no_inverse():
    """The runtime half of `RevL.G7.estop_replays_nothing`, at `_guard` — the
    one chokepoint every emitted disposer shape passes through."""
    ran = []
    frame = _frame()
    guarded = frame._guard(lambda: ran.append("undo"))

    guarded()                       # before the halt: the inverse runs
    assert ran == ["undo"]

    rt.estop("halt", operator="alice")
    guarded()                       # after: it does not
    assert ran == ["undo"]
    assert [r["kind"] for r in frame.estop_residue] == ["estop-stranded"]
    assert frame.estop_residue[0]["entry"] == "bracket"


def test_drain_under_a_halt_commits_nothing():
    """A halt is a third verdict, not a commit. `drain` must not flip
    `_committed` (which would DISCHARGE every witnessed mutation and drop the
    very descriptors `revl recover` reads back) and must dispose nothing."""
    frame = _frame()
    frame.transactional_method(lambda w: None, {"row": 1})
    rt.estop("halt", operator="alice")

    assert frame.drain() is None
    assert frame._committed is False
    assert frame._deferred_transactional == []


def test_phase2_strands_owed_compensations_instead_of_running_them():
    """Best effort under an operator halt is zero effort: a compensation is
    itself a boundary crossing, and the halt's first guarantee is that no new
    crossing is dispatched."""
    frame = _frame()
    fired = []
    entry = frame.compensation(lambda: fired.append(1))
    frame._pending_compensations.append(entry)
    rt.estop("halt", operator="alice")
    # the halt already named this entry; a second, unnamed one proves the
    # Phase-2 branch records rather than runs.
    fresh = rt._Compensation.__new__(rt._Compensation)
    fresh.frame, fresh.component, fresh.method, fresh.seq = frame, "Ops", "recall", 7
    frame._pending_compensations = [entry, fresh]

    frame._drain_phase2()
    assert fired == []
    assert frame.compensation_residue == []
    assert [r["method"] for r in frame.estop_residue] == ["recall"]
    assert frame.estop_residue[0]["kind"] == "estop-stranded"


def test_an_entry_is_never_counted_twice():
    """The inventory is built in two halves — named at the halt, completed as
    the unwind hands disposers over — so the dedup flag is load-bearing."""
    frame = _frame()
    entry = frame.transactional(lambda w: None, {"row": 1})
    halt = rt.estop("halt", operator="alice")
    assert len(halt["stranded"]) == 1

    frame._guard(entry)()          # cordis hands the same entry over later
    assert frame.estop_residue == []
    assert len(rt.estop_residue()) == 1


def test_an_in_flight_crossing_is_ambiguous_and_never_stranded():
    """Item 440's ambiguous tier, created deliberately: the halt lands between
    the journal entry and the completion record, so the crossing MAY have
    landed and the runtime says so rather than guessing."""
    with rt._InFlight(component="Payments", method="charge", seq=11,
                      entry="crossing"):
        halt = rt.estop("runaway loop", operator="alice")

    assert len(halt["inFlight"]) == 1
    record = halt["inFlight"][0]
    assert record["kind"] == "estop-ambiguous"
    assert record["outcome"] == "unknown"
    assert record["attemptedFlag"] is True
    assert record["attempted"]["call"] == "charge"
    assert record["component"] == "Payments"
    assert "idempotency key" in record["hint"]
    assert halt["stranded"] == []


def test_at_most_one_crossing_is_ambiguous_per_activation():
    """`RevL.G7.halt_ambiguity_is_at_most_one`: the halt creates exactly the
    ambiguity of the calls it interrupted, never a fog over the whole stack."""
    frame = _frame()
    frame.transactional(lambda w: None, {"row": 1})
    frame.compensation(lambda: None)
    with rt._InFlight(component="Ops", method="charge", seq=None,
                      entry="crossing"):
        halt = rt.estop("halt", operator="alice")
    assert len(halt["inFlight"]) == 1
    assert len(halt["stranded"]) == 2


def test_the_merged_inventory_covers_both_halves():
    frame = _frame()
    frame.transactional(lambda w: None, {"row": 1})
    rt.estop("halt", operator="alice")
    frame._guard(lambda: None)()          # a bare bracket, nameable only here
    merged = rt.estop_residue()
    assert len(merged) == 2
    assert {r["entry"] for r in merged} == {"transactional", "bracket"}


# -- the cross-process latch ------------------------------------------------


def test_the_latch_file_engages_the_halt(tmp_path):
    latch = tmp_path / "run.wal.estop"
    rt.arm_estop_latch(str(latch))
    assert not rt.estop_engaged()

    latch.write_text(json.dumps({"halted": True, "reason": "operator halt",
                                 "operator": "alice"}), encoding="utf-8")
    assert rt.estop_engaged()
    with pytest.raises(rt.EstopHalted):
        rt.plug(None, {"name": "Payments"})
    assert rt.estop_state()["operator"] == "alice"


def test_a_malformed_latch_still_halts(tmp_path):
    """Failing open on a malformed emergency stop is the one failure mode this
    feature exists to prevent."""
    latch = tmp_path / "run.estop"
    latch.write_text("{ this is not json", encoding="utf-8")
    rt.arm_estop_latch(str(latch))
    with pytest.raises(rt.EstopHalted):
        rt.plug(None, {"name": "Payments"})


def test_an_unreadable_latch_reads_as_halted_not_absent(tmp_path):
    """R-C11 (issue #538): a latch that EXISTS but cannot be READ must fail
    CLOSED. An EISDIR (a path an operator turned into a directory) or an EACCES
    (permissions changed) is an existing-but-unreadable latch, not an absent
    one — reading it as not-halted would be the fail-open this feature exists to
    prevent. Only a genuinely absent latch (FileNotFoundError) reads as absent.

    Pinned on all three readers that must agree: the shared vocabulary
    (`revl.estop.read_latch`), the py runtime seam (`runtime._latch_record`),
    and the crossing gate (`estop_engaged`)."""
    from revl import estop as estop_mod  # noqa: PLC0415

    # A path that exists as a DIRECTORY: open() raises IsADirectoryError (EISDIR).
    latch_dir = tmp_path / "run.estop"
    latch_dir.mkdir()

    record = estop_mod.read_latch(str(latch_dir))
    assert record is not None and record.get("halted") is True

    rt.arm_estop_latch(str(latch_dir))
    assert rt._latch_record() is not None
    assert rt.estop_engaged()
    with pytest.raises(rt.EstopHalted):
        rt.plug(None, {"name": "Payments"})

    # And a genuinely ABSENT latch still reads as not-halted (no false positive).
    absent = tmp_path / "nope.estop"
    assert estop_mod.read_latch(str(absent)) is None


def test_an_unreadable_latch_by_permission_fails_closed(tmp_path):
    """The EACCES half of R-C11: a latch whose permissions were changed so it
    can no longer be opened must still read as HALTED, not absent."""
    import os as _os  # noqa: PLC0415

    from revl import estop as estop_mod  # noqa: PLC0415

    latch = tmp_path / "locked.estop"
    latch.write_text(json.dumps({"halted": True, "reason": "operator halt",
                                 "operator": "alice"}), encoding="utf-8")
    _os.chmod(latch, 0)
    try:
        if _os.access(str(latch), _os.R_OK):
            pytest.skip("cannot revoke read permission (running as root)")
        record = estop_mod.read_latch(str(latch))
        assert record is not None and record.get("halted") is True
    finally:
        _os.chmod(latch, 0o600)


def test_the_latch_is_read_from_the_environment(tmp_path, monkeypatch):
    latch = tmp_path / "amb.estop"
    monkeypatch.setenv("REVL_ESTOP_LATCH", str(latch))
    assert rt.estop_latch_path() == str(latch)
    assert not rt.estop_engaged()
    latch.write_text(json.dumps({"reason": "ambient", "operator": "bob"}),
                     encoding="utf-8")
    assert rt.estop_engaged()


# -- the CLI ----------------------------------------------------------------


def _cli(*argv) -> int:
    from revl.__main__ import main
    return main(list(argv))


def test_revl_estop_arms_reports_and_clears(tmp_path, capsys):
    latch = tmp_path / "s.estop"

    assert _cli("estop", "--latch", str(latch), "--reason", "runaway loop",
                "--operator", "alice") == 1
    out = capsys.readouterr().out
    assert "E-STOP ENGAGED" in out
    assert "nothing was unwound" in out.lower()
    assert "no resume" in out
    record = json.loads(latch.read_text(encoding="utf-8"))
    assert record["reason"] == "runaway loop"
    assert record["operator"] == "alice"
    assert record["resumable"] is False

    # a second press is idempotent and never overwrites the first reason
    assert _cli("estop", "--latch", str(latch), "--reason", "oops",
                "--operator", "mallory", "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["alreadyHalted"] is True
    assert report["reason"] == "runaway loop"
    assert report["clean"] is False
    assert json.loads(latch.read_text(encoding="utf-8"))["operator"] == "alice"

    # --report touches nothing and is never clean
    assert _cli("estop", "--latch", str(latch), "--report", "--json") == 1
    assert json.loads(capsys.readouterr().out)["clean"] is False
    assert latch.exists()

    # --clear is not a resume
    assert _cli("estop", "--latch", str(latch), "--clear", "--json") == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["cleared"] is True
    assert cleared["resumed"] is False
    assert not latch.exists()

    assert _cli("estop", "--latch", str(latch), "--report", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "halted": False, "latch": str(latch), "clean": True}


def test_revl_estop_needs_a_latch_or_a_wal(capsys):
    assert _cli("estop", "--reason", "x") == 2
    assert "needs --latch" in capsys.readouterr().err


def test_the_wal_derives_the_latch_and_names_what_is_owed(tmp_path, capsys):
    """The WAL is the durable rendezvous the reconciliation path already uses,
    so a halt and its reconciliation name the same session with one argument.

    A `discharge-descriptor` with no `discharge` behind it is exactly an entry
    the halt stranded, and exactly what `revl recover` would replay."""
    wal = tmp_path / "run.wal"
    wal.write_text("\n".join(json.dumps(line) for line in [
        {"record": "header", "walVersion": 1},
        {"record": "discharge-descriptor", "seq": 1, "entry": "transactional",
         "call": {"receiver": "Ops", "method": "delete_row", "args": []}},
        {"record": "discharge-descriptor", "seq": 2, "entry": "compensation",
         "call": {"receiver": "Mail", "method": "recall", "args": []},
         "idempotency": "k-2"},
        {"record": "discharge-descriptor", "seq": 3, "entry": "transactional",
         "call": {"receiver": "Ops", "method": "delete_other", "args": []}},
        {"record": "discharge", "discharged": [3]},
    ]) + "\n", encoding="utf-8")

    assert _cli("estop", "--wal", str(wal), "--operator", "alice",
                "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert (tmp_path / "run.wal.estop").exists()
    assert report["reconcile"] == f"revl recover --wal {wal}"
    outstanding = report["outstanding"]
    assert outstanding["known"] is True
    assert outstanding["count"] == 2          # seq 3 was already discharged
    assert [e["seq"] for e in outstanding["entries"]] == [1, 2]
    assert outstanding["entries"][1]["idempotency"] == "k-2"


# -- the operator authority surface -----------------------------------------


def test_estop_is_gated_as_its_own_management_verb():
    from revl.mcp import operator as op
    assert op.TOOL_VERB["revl_estop"] == "estop"
    # read-only report is never gated
    assert "revl_estop_report" not in op.TOOL_VERB
    # and it is NOT an alias of an existing authority: an operator who may
    # unload is not thereby allowed to strand the world
    assert op.TOOL_VERB["revl_estop"] not in ("unload", "commit")


def test_a_profile_without_estop_refuses_the_halt():
    from revl.mcp import operator as op

    class _Session:
        ir = {"components": [{"name": "Payments"}]}
        operator = None

    session = _Session()
    session.operator = op.parse_profile(
        "operator alice may unload, swap on *").get("alice")
    decision = op.decide(session, "revl_estop", {})
    assert decision.gated and not decision.allowed
    assert "`estop`" in decision.message

    session.operator = op.parse_profile(
        "operator root may estop on *").get("root")
    assert op.decide(session, "revl_estop", {}).allowed


def test_a_subject_scoped_grant_does_not_authorize_a_whole_composition_halt():
    """A halt that stopped one component would not be a halt, so `estop`
    targets everything and a scoped grant must not cover it."""
    from revl.mcp import operator as op

    class _Session:
        ir = {"components": [{"name": "TenantACache"},
                             {"name": "TenantBCache"}]}
        operator = op.parse_profile(
            "operator alice may estop on TenantA*").get("alice")

    decision = op.decide(_Session(), "revl_estop", {})
    assert decision.gated and not decision.allowed


# -- the contract documents its own third column ----------------------------


def test_the_third_column_is_in_the_teardown_contract_and_the_model():
    """An E-Stop verdict is a third column in the contract's table, and adding
    it is a formal-layer change before it is a runtime change (item 443)."""
    contract = (_ROOT / "docs" / "design" / "teardown-contract.md").read_text(
        encoding="utf-8")
    assert "**on an E-Stop (443)**" in contract
    assert "estop-stranded" in contract and "estop-ambiguous" in contract

    semantics = (_ROOT / "formal" / "RevL" / "Semantics.lean").read_text(
        encoding="utf-8")
    assert "| halted" in semantics
    assert "def Verdict.settles" in semantics
    assert "def EntryKind.strandedUnder" in semantics

    g7 = (_ROOT / "formal" / "RevL" / "Theorems" / "G7_LifoComplete.lean"
          ).read_text(encoding="utf-8")
    for theorem in ("estop_replays_nothing", "estop_discharges_nothing",
                    "estop_strands_everything", "halt_ambiguity_is_at_most_one",
                    "halt_books_are_total"):
        assert f"theorem {theorem}" in g7
