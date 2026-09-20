"""`revl erase-report --realm <r>` — the composed erasure artifact
(src/revl/erase_report.py, docs/erase-report.md, roadmap item 29).

Three claims, checked independently:

  * in-process state gone (the R4 no-residue proof over a real teardown),
  * every boundary crossing the realm's components make, compensated vs bare,
  * other realms provably untouched (the `survivors` set, EXACT).

The `erase_realms.rvl` fixture is two isolated realms `alpha` and `beta`
sharing a service type but never a provision; realm `alpha` makes one
compensated and one bare emission. The runtime no-residue proof needs the
cordis-py backend, so those assertions are guarded with `_has_runtime`.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl.compiler import compile_files  # noqa: E402
from revl import erase_report  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REALMS = os.path.join(ROOT, "tests", "fixtures", "erase_realms.rvl")
TENANTS = os.path.join(ROOT, "examples", "tenants.rvl")


def _has_runtime() -> bool:
    try:
        import cordis  # noqa: F401,PLC0415
        return True
    except ModuleNotFoundError:
        return False


@pytest.fixture(scope="module")
def realms_ir():
    return compile_files([REALMS])


@pytest.fixture(scope="module")
def tenants_ir():
    return compile_files([TENANTS])


# ---------------------------------------------------------- realm discovery

def test_realms_of_lists_named_realms(realms_ir):
    assert erase_report.realms_of(realms_ir) == ["alpha", "beta"]


def test_unknown_realm_is_a_clean_error(realms_ir):
    report = erase_report.build_report(realms_ir, "ghost", prove_residue=False)
    assert report["ok"] is False
    assert "ghost" in report["error"]
    assert report["knownRealms"] == ["alpha", "beta"]


# ---------------------------------------------------------- honest scope

def test_honest_scope_header_is_present(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    scope = report["honestScope"]
    # the header must state BOTH what it proves and what it explicitly does not
    assert scope["proves"] and scope["doesNotProve"]
    blob = " ".join(scope["doesNotProve"]).lower()
    assert "compensation is not inversion" in blob
    assert "§6.1" in " ".join(scope["doesNotProve"]) or "6.1" in scope["reference"]
    # enumerates exposure, does not undo it
    assert "enumerates" in blob and "does not undo" in blob


# ------------------------------- the note and the states are one definition
#
# Issue #1293. The header used to carry ONE sentence for every crossing it
# could not undo: "a bare crossing left the system with nothing done about it
# ... so it can be handled out of band". Item 522 split the per-crossing lines
# into five states and left that sentence alone, so the render printed it
# directly above the `[UNCOMPENSATED]` and `[untouched]` lines that refute it.
#
# These are the set-equality oracle, the same shape as the `layer_state.
# OUTCOMES` one in `tests/test_ui_transaction_phases_522.py`: the English note
# is DERIVED from the computed vocabulary, so neither can grow a word the
# other does not have.


def test_every_residue_state_has_a_clause_and_no_clause_invents_a_state():
    """`OUT_OF_BAND` is keyed by the states `ui_transaction` computes, exactly.
    A sixth state there with no clause here would print a tag the header never
    explains; a clause here for a state the report cannot tag would be the
    header describing something that never appears."""
    from revl import ui_transaction as uitx
    assert set(erase_report.OUT_OF_BAND) == set(uitx.WEAKEST_FIRST)


def test_the_note_is_built_from_the_state_order_not_written_out_again():
    """Weakest first, one line per state, in `WEAKEST_FIRST` order. Reading
    the clauses off that tuple is what makes the two sets unable to disagree;
    an equal-but-hand-kept list would pass the set check above and still drift
    in order or wording."""
    from revl import ui_transaction as uitx
    caveats = erase_report._residue_caveats()
    assert caveats == [erase_report.OUT_OF_BAND[s] for s in uitx.WEAKEST_FIRST]
    assert [line for line in erase_report.HONEST_SCOPE["doesNotProve"]
            if line in caveats] == caveats


def test_a_state_with_no_clause_fails_loudly_rather_than_going_unsaid(
        monkeypatch):
    """The failure direction. A state added to `ui_transaction` with nothing
    said about it here must break this module, not silently leave the header
    quiet about a tag the report prints."""
    from revl import ui_transaction as uitx
    monkeypatch.setattr(
        uitx, "WEAKEST_FIRST", uitx.WEAKEST_FIRST + ("invented",))
    with pytest.raises(AssertionError) as refused:
        erase_report._residue_caveats()
    assert "invented" in str(refused.value)


def test_the_note_no_longer_claims_an_uncompensated_crossing_can_be_handled(
        realms_ir):
    """The measured defect. `uncompensated` means no inverse EXISTS, so the
    one thing the old sentence promised an auditor - run it out of band - is
    the one thing that cannot be done."""
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    lines = report["honestScope"]["doesNotProve"]
    assert not any(line.startswith("a bare crossing left the system")
                   for line in lines)
    unc = erase_report.OUT_OF_BAND["uncompensated"]
    assert "no inverse exists" in unc.lower()
    assert "cannot be handled out of band" in unc


def test_the_note_says_a_read_needs_no_handling_at_all():
    """`untouched` is the other false reading: `read_pane` is `screen.observe`
    and changed nothing, so asking an auditor to handle it is the report
    over-reporting its own residue."""
    unt = erase_report.OUT_OF_BAND["untouched"]
    assert "nothing to handle out of band" in unt
    assert "changed no state the target owns" in unt


def test_emissions_keep_the_word_bare_because_it_is_still_correct_for_them():
    """Only computer-use crossings gained the finer vocabulary. An emission
    with no `compensate` clause is still `bare`, the headline counts still
    call it that, and the header still says what `bare` means."""
    blob = " ".join(erase_report.HONEST_SCOPE["doesNotProve"])
    assert "a bare emission or bare host extern left the system" in blob
    assert "`bare` is still the right word for those" in blob


def test_render_leads_with_scope_and_names_the_realm(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    text = erase_report.render(report)
    assert "REALM ERASURE REPORT — realm `alpha`" in text
    assert "DOES NOT PROVE" in text
    assert "compensation is NOT inversion" in text


# ---------------------------------------------------------- section 2: crossings

def test_bare_crossing_is_listed_as_bare(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    cross = report["boundaryCrossings"]
    by_method = {e["method"]: e for e in cross["emissions"]}
    # `sink.bare` is emitted with no compensate clause -> bare
    assert by_method["bare"]["compensated"] is False
    assert "emit:AlphaApp:sink.bare" in cross["bareTokens"]
    # `sink.commit` is emitted WITH a compensate clause -> compensated
    assert by_method["commit"]["compensated"] is True
    assert cross["bareCount"] >= 1 and cross["compensatedCount"] >= 1


def test_fully_revertible_realm_has_no_crossings(tenants_ir):
    report = erase_report.build_report(tenants_ir, "tenant_a", prove_residue=False)
    cross = report["boundaryCrossings"]
    assert cross["total"] == 0
    assert cross["bareCount"] == 0 and cross["compensatedCount"] == 0


# a realm member whose provide-method hands an emitting extern to a dispatcher
# in VALUE position. The reach is unnameable (`*`): `approval.ClassMap`
# classifies the call class (c), so the erase report must enumerate the same
# crossing or its completeness claim is a false-safe (item 414).
_WIDENING_SOURCE = """
extern emission fn ship(x: Str) -> Str = @py { return x }
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
service Gw { emission fn send(a: Str) -> Str }
component Widener provides gw: Gw {
  isolate gw in realm("alpha")
  provide gw { fn send(a) = indirect(ship, a) }
}
"""


def test_star_widening_crossing_is_enumerated(realms_ir):
    # the erase report must list the SAME class-(c) `*` crossing the
    # auto-approve ClassMap raises for a first-class emitting callable that
    # escapes in value position (item 414: the report was a second class fold
    # blind to the widening).
    from revl.compiler import compile_source
    from revl.mcp.approval import ClassMap

    ir = compile_source(_WIDENING_SOURCE, "erase_widening.rvl")
    # ClassMap sees the widening as a class-(c) crossing capability `*`.
    reach = ClassMap(ir).classify_call("gw", "send")
    assert reach["class"] == "c" and "*" in reach["capabilities"]

    cross = erase_report.build_report(ir, "alpha", prove_residue=False)[
        "boundaryCrossings"]
    # the report enumerates it, not the false-safe empty surface.
    assert cross["widenings"], "the `*` widening crossing must be enumerated"
    widen = cross["widenings"][0]
    assert widen["component"] == "Widener"
    assert widen["capability"] == "*" and widen["actionClass"] == "c"
    # a `*` widening is irreversible and bare, folded into the totals.
    assert widen["compensated"] is False
    assert cross["total"] == 1 and cross["bareCount"] == 1
    assert "widen:Widener:*" in cross["bareTokens"]
    # and it shows up in the human render as a bare crossing.
    assert "widen `*`" in erase_report.render(
        erase_report.build_report(ir, "alpha", prove_residue=False))


def test_report_without_widening_omits_the_star_crossing(realms_ir):
    # additive: a realm that widens nothing has an empty widenings bucket and
    # its crossing totals are untouched by the item-414 fix.
    cross = erase_report.build_report(realms_ir, "alpha", prove_residue=False)[
        "boundaryCrossings"]
    assert cross["widenings"] == []
    assert not any("widen:" in t for t in cross["bareTokens"])


# ---------------------------------------------------------- section 3: survivors

def test_other_realms_proven_untouched_via_survivors(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    others = report["otherRealmsUntouched"]
    assert others["untouched"] is True
    assert others["breached"] == []
    # withdrawing realm alpha withdraws exactly its two components
    assert others["withdrawnComponents"] == ["AlphaSink", "AlphaApp"]
    # and beta's components keep every provision — that IS the proof
    assert others["survivors"] == ["BetaApp", "BetaSink"]
    assert others["otherRealms"]["beta"] == ["BetaApp", "BetaSink"]


def test_survivors_on_tenants_example(tenants_ir):
    report = erase_report.build_report(tenants_ir, "tenant_a", prove_residue=False)
    others = report["otherRealmsUntouched"]
    assert others["untouched"] is True
    assert others["survivors"] == ["TenantBApp", "TenantBStore"]


# ---------------------------------------------------------- section 1: state gone

def test_state_gone_lists_the_realm_provisions(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    prov = report["inProcessStateGone"]["provisionsErased"]
    assert {p["provider"] for p in prov} == {"AlphaSink"}
    assert prov[0]["key"] == "sink" and prov[0]["realm"] == "alpha"


def test_static_report_skips_the_runtime_proof(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    residue = report["inProcessStateGone"]["noResidueProof"]
    assert residue["available"] is False
    assert residue["proven"] is None


@pytest.mark.skipif(not _has_runtime(), reason="cordis-py runtime not installed")
def test_no_residue_proof_holds_over_a_real_teardown(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=True)
    residue = report["inProcessStateGone"]["noResidueProof"]
    assert residue["available"] is True
    assert residue["proven"] is True
    # the four R4 checks: registry, provisions, effect disposables, listeners
    assert all(residue["checks"].values())
    assert set(residue["checks"]) == {"registry", "provisions", "effects", "listeners"}
    assert report["summary"]["stateGoneProven"] is True


# ---------------------------------------------------------- versioned document

def test_report_is_a_versioned_self_describing_document(realms_ir):
    report = erase_report.build_report(realms_ir, "beta", prove_residue=False)
    assert report["kind"] == "revl.erase-report"
    # 1.1: item 522 added the additive `boundaryCrossings.uiResidue`
    # member (a computer-use split), absent for a realm that crosses
    # no computer-use verb. MINOR, per the module's own rule.
    assert report["schema_version"] == "1.1"
    assert report["realm"] == "beta"
    # round-trips through JSON without loss (it is an interchange artifact)
    assert json.loads(json.dumps(report)) == report


# ---------------------------------------------------------- CLI wiring

def test_cli_static_report_json(realms_ir):
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "erase-report", REALMS,
         "--realm", "alpha", "--json", "--no-residue-proof"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["ok"] and doc["realm"] == "alpha"
    assert doc["kind"] == "revl.erase-report"
    assert doc["summary"]["bareCrossings"] >= 1


def test_cli_unknown_realm_exits_nonzero():
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "erase-report", REALMS,
         "--realm", "ghost", "--no-residue-proof"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )
    assert proc.returncode == 1
    assert "unknown realm" in proc.stdout + proc.stderr
