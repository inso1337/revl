"""The A5 row of the formal differential harness (issue #2114).

A5 is "compensation accompanies an emission". `docs/rejections.md` used to
enforce it "by construction" and argue that no refusing example *could* exist,
because `compensate` is an *optional* slot (`DESIGN.md` section 3.5: an
emission "may declare" one). The premise was right and the conclusion did not
follow: a guarantee whose subject is an optional clause is vacuous until
something states when the clause becomes REQUIRED, and the thing that states
it is the computer-use reversibility registry
(`src/revl/ui_family.py`, `REVERSIBILITY`), which `ui_family.teardown_refusal`
consults at extern-declaration time:

  * a verb whose class is `compensatable` MUST fill its `compensate` slot;
  * a verb whose class has NO INVERSE (`unknown`, `irreversible`) MUST NOT.

Both are raised with `code="G4"`, `category="reversibility"` — there is no A5
code, and the STATUS row credits the code rather than claiming A5 — so this
module pins the PYTHON half in the plain `pytest tests/` job (the #1141 /
#1164 pattern that `tests/test_formal_a9_row.py` also follows): the fixtures'
reported code, the facts the row reads, the reference's verdict in both
polarities, the `checker_alignment` arms in both directions, and the
non-vacuity ratchet. Nothing here runs Lean.

Two shapes make this row different from its siblings, and both are pinned
below. The violating half is raised at PARSE, so a violating document exports
NO FACTS and its row is read off the checker's own refusal sentence; and the
class is read off the DECLARED TOKEN through each side's own copy of the
registry rather than carried as a column, which is what makes the two sides'
agreement a check rather than a restatement.
"""

from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from _load_by_path import load_by_path  # noqa: E402

#: `compensatable` with the slot EMPTY: the obligation's violated direction.
MISSING = "examples/rejections/a5_compensatable_without_compensate.rvl"

#: A class with NO INVERSE that DECLARES one: the other violated direction.
DECLARED = "examples/rejections/a5_no_inverse_declares_compensate.rvl"

#: The admitted `compensatable` declaration that FILLS its slot. Without it
#: the corpus only ever says `fail` and agreement would prove nothing.
SATISFIED = "tests/fixtures/emit_py_corpus/ui_transaction_unit.rvl"

#: Admitted declarations whose class has NO INVERSE and which declare nothing.
NO_INVERSE_ADMITTED = sorted([
    ("tests/fixtures/emit_py_corpus/ui_transaction_unit.rvl", "actuate"),
    ("tests/fixtures/emit_py_corpus/ui_unit.rvl", "actuate"),
    ("tests/fixtures/emit_wasm_refusals/ui_transaction_unit.rvl", "actuate"),
])

LEAN = (ROOT / "formal" / "RevL" / "Theorems"
        / "A5_CompensationAccompaniesEmission.lean")
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"


@pytest.fixture(scope="module")
def harness():
    return load_by_path(
        "formal_diff_corpus",
        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def tsv(harness):
    rows, _facts, _census = harness.export()
    return [r.split("\t") for r in rows]


@pytest.fixture(scope="module")
def verdicts(harness, tsv):
    return harness.reference_from_tsv(["\t".join(r) for r in tsv])


def _rows(tsv, kind, rel=None):
    return [r for r in tsv
            if r and r[0] == kind and (rel is None or r[1] == rel)]


def _compile(rel: str):
    from revl import compile_files
    from revl.diagnostics import classify
    from revl.errors import RevlError

    try:
        compile_files([str(ROOT / rel)])
    except RevlError as e:
        info = classify(e)
        return info["code"], info["category"], info["message"]
    return "accept", "", ""


# ------------------------------------------------------------ the premise

def test_the_reference_refuses_the_missing_slot_under_g4_reversibility():
    """If this stops refusing, the rest of the module measures the wrong
    thing and should be read again, not deleted. The code is G4, NOT A5:
    that is the carrier claim the STATUS row makes."""
    code, category, message = _compile(MISSING)
    assert (code, category) == ("G4", "reversibility")
    assert message == ("`emission[ui.text]` is compensatable, so extern "
                       "`type_amount` must declare `compensate`")


def test_the_reference_refuses_a_declared_inverse_under_g4_reversibility():
    code, category, message = _compile(DECLARED)
    assert (code, category) == ("G4", "reversibility")
    assert message == ("`emission[ui.click]` is unknown, so extern `actuate` "
                       "may not declare `compensate`")


def test_the_reference_admits_the_satisfied_shape():
    assert _compile(SATISFIED) == ("accept", "", "")


def test_reversibility_is_the_only_category_that_raiser_uses():
    """The second `checker_alignment` pass discriminates on the reported
    (code, category) pair, so the pair has to be unique to this rule: the
    three pre-existing `REFUSED-AT-PARSE` G4 documents are `guarantee`, and
    nothing else in the tree raises G4/reversibility."""
    from revl import ui_family

    assert ui_family.reversibility("ui.text") == "compensatable"
    assert ui_family.reversibility("ui.click") == "unknown"
    assert ui_family.reversibility("ui.download") == "irreversible"
    assert ui_family.reversibility("db.insert") is None
    # A ladder rung resolves to its verb's class, never to a class of its own.
    assert ui_family.reversibility("ui.text(selector)") == "compensatable"


# --------------------------------------------------- the facts the row reads

def test_the_a5_row_carries_the_declared_token_and_the_compensate_column(tsv):
    """Five fields, and the CLASS is deliberately NOT one of them: each side
    reads it off the token through its own copy of the registry."""
    rows = _rows(tsv, "A5", SATISFIED)
    assert rows, "the satisfied fixture exported no A5 row at all"
    assert all(len(r) == 5 for r in rows), rows
    assert [r[2:] for r in rows if r[2] == "type_amount"] == [
        ["type_amount", "ui.text", "yes"]]


def test_the_violating_half_is_read_off_the_refusal_not_the_export(tsv):
    """A parse refusal exports NO facts — there is no `M` row and no
    `file_facts` entry to read — so the violating row cannot come from the
    per-file export path. It is appended by `export()` from the checker's own
    refusal sentence instead, beside the `X` row that names the reported code.
    If this ever stops being true the two-source design can be simplified."""
    for rel in (MISSING, DECLARED):
        assert _rows(tsv, "M", rel) == [], rel
        assert _rows(tsv, "X", rel) == [["X", rel, "G4"]], rel
        assert len(_rows(tsv, "A5", rel)) == 1, rel


def test_every_exported_a5_row_is_inside_the_rows_domain(tsv):
    """A row is emitted only for a token the SHIPPED registry classifies, so
    `db.insert` and a declaration with no capability token contribute
    nothing: the rule does not reach them."""
    from revl import ui_family

    rows = _rows(tsv, "A5")
    assert rows, "no A5 rows at all"
    for r in rows:
        assert ui_family.reversibility(r[3]) is not None, r
        assert r[4] in ("yes", "no"), r


# -------------------------------------------------- the reference verdict

def test_the_reference_fails_both_fixtures_and_admits_the_satisfied_shape(
        verdicts):
    assert verdicts.a5[(MISSING, "type_amount", "ui.text")] == "fail"
    assert verdicts.a5[(DECLARED, "actuate", "ui.click")] == "fail"
    assert verdicts.a5[(SATISFIED, "type_amount", "ui.text")] == "ok"
    for rel, name in NO_INVERSE_ADMITTED:
        assert verdicts.a5[(rel, name, "ui.click")] == "ok", (rel, name)


def test_the_only_reference_fails_are_the_two_fixtures(verdicts):
    """The corpus carries exactly the two refused shapes. A third `fail`
    here is either a new fixture (add it to this list) or a reference
    regression."""
    assert sorted(k for k, v in verdicts.a5.items() if v == "fail") == sorted([
        (MISSING, "type_amount", "ui.text"),
        (DECLARED, "actuate", "ui.click")])


def test_the_reference_is_the_rule_over_the_restated_table(harness, tsv):
    """Recompute the verdict from the raw rows with no harness code at all,
    so the reference cannot drift into reading the class twice."""
    want = {}
    for r in _rows(tsv, "A5"):
        rel, name, token, has = r[1], r[2], r[3], r[4] == "yes"
        cls = harness._a5_class(token)
        assert cls is not None, r
        want[(rel, name, token)] = "ok" if harness._a5_holds(cls, has) else "fail"
    got = harness.reference_from_tsv(["\t".join(r) for r in tsv]).a5
    assert got == want


def test_the_restated_table_is_the_shipped_registry(harness):
    """The reference restates `ui_family.REVERSIBILITY` so a registry the
    checker widened alone surfaces as a finding instead of being absorbed."""
    from revl import ui_family

    assert harness._A5_REVERSIBILITY, "the restated table is empty"
    for token, cls in harness._A5_REVERSIBILITY.items():
        assert ui_family.reversibility(token) == cls, token
    assert harness._A5_UNREADABLE == {}


# ---------------------------------------------------- the alignment arm

def _align(harness, verdicts, rel=None, refusals=()):
    """`refusals` is the parse-refusal list this call asks about. The two A5
    fixtures are PARSE refusals, so the census keeps them in its own
    `refusals` list and they are bucketed only when the caller names them —
    exactly as `main()` does. `rel` is the single admitted file whose facts
    this call models."""
    facts = {} if rel is None else {rel: {}}
    buf = io.StringIO()
    with redirect_stdout(buf):
        fatal = harness.checker_alignment(facts, [], verdicts, (), refusals)
    counts = {k: n for k, n in re.findall(
        r"^  ([a-zA-Z0-9-]+)\s+(\d+)(?:\s+FATAL)?$",
        buf.getvalue(), re.MULTILINE) if n != "0"}
    return counts, fatal


def test_missed_g4_is_fatal(harness):
    assert "missed-G4" in harness.FATAL_BUCKETS


def test_both_fixtures_land_in_agree_g4(harness, verdicts):
    """THE POINT OF THE SECOND PASS. Both files have no facts, so no arm of
    the per-file loop can see them; without the pass over the caller's
    refusals the violating direction would be unratcheted, which is the
    defect #2114 names. Modelled with no facts at all, the pass is the ONLY
    thing that runs — and it still reaches both."""
    counts, fatal = _align(harness, verdicts, refusals=(MISSING, DECLARED))
    assert counts == {"agree-G4": "2"}, counts
    assert fatal == []


def test_a_blind_row_lands_in_missed_g4(harness, verdicts):
    """Make the row blind (say `ok` where the checker refused) and the arm
    must fail the gate: the checker refusing where the model sees nothing is
    the direction a differential oracle cannot afford to report as
    agreement. The OTHER fixture stays in `agree-G4`, so the bucket is a
    per-file verdict and not a count of refusals."""
    for rel in (MISSING, DECLARED):
        blind = verdicts._replace(a5={**verdicts.a5,
                                      **{k: "ok" for k in verdicts.a5
                                         if k[0] == rel}})
        counts, fatal = _align(harness, blind, refusals=(MISSING, DECLARED))
        assert counts == {"agree-G4": "1", "missed-G4": "1"}, (rel, counts)
        assert fatal == [f"missed-G4: {rel}"], rel


def test_a_caller_that_models_no_refusal_sees_no_a5_bucket(harness, verdicts):
    """The scoping that keeps the rest of the suite honest. A one-file call
    that does not name the parse refusals must not be handed their
    `agree-G4`: the A5 refusal pass is a statement about the files the
    CALLER models, and `main()` names them via the census's `refusals`
    list. Without this the two fixtures leaked into every one-file
    alignment test in the suite."""
    counts, fatal = _align(harness, verdicts)
    assert counts == {}, counts
    assert fatal == []


def test_an_a5_fail_on_an_accepted_file_is_formal_strict(harness, verdicts):
    """`formal_clean` reads the A5 row: an accepted file whose row said `fail`
    would be `formal-strict`, not `agree-accept`, and fatal."""
    rel = SATISFIED
    counts, _ = _align(harness, verdicts, rel)
    assert counts["agree-accept"] == "1"
    strict = verdicts._replace(a5={**verdicts.a5,
                                   (rel, "type_amount", "ui.text"): "fail"})
    counts, fatal = _align(harness, strict, rel)
    assert counts["formal-strict"] == "1"
    assert fatal == [f"formal-strict: {rel}"]


# ------------------------------------------------------- the ratchet

def test_the_coverage_ratchet_is_satisfied(harness, verdicts):
    buf = io.StringIO()
    with redirect_stdout(buf):
        findings = harness.a5_coverage()
    assert findings == []
    out = buf.getvalue()
    assert "violating" in out and "admitting" in out
    for rel in (MISSING, DECLARED):
        assert rel not in out  # the verdict line names tokens, not files
    assert "ui.text" in out and "ui.click" in out


def test_the_coverage_ratchet_bites_without_each_witness(harness, verdicts):
    kept = dict(harness._A5_ROWS)

    def only(pred):
        harness._A5_ROWS.clear()
        harness._A5_ROWS.update({k: v for k, v in kept.items() if pred(v)})
        return harness.a5_coverage()

    try:
        found = only(lambda v: v[2])  # drop both violations
        assert len(found) == 1 and "vacuous" in found[0], found
        found = only(lambda v: not (v[0] == harness._A5_COMPENSATABLE and v[1]))
        assert any("satisfied side" in f for f in found), found
        found = only(lambda v: v[0] not in harness._A5_NO_INVERSE or not v[2])
        assert any("NO INVERSE" in f for f in found), found
        harness._A5_ROWS.clear()
        found = harness.a5_coverage()
        assert len(found) == 1 and "no A5 rows at all" in found[0], found
    finally:
        harness._A5_ROWS.clear()
        harness._A5_ROWS.update(kept)


def test_the_ratchet_refuses_a_violation_the_checker_did_not_report(harness):
    """The violating row is a PARSE-TIME G4/reversibility refusal; a violating
    row over a file the checker reports any other way is not this rule. This
    is the direction that keeps the row from claiming a shape it cannot
    read."""
    kept = dict(harness._A5_ROWS)
    try:
        harness._A5_ROWS[(SATISFIED, "type_amount", "ui.text")] = (
            harness._A5_COMPENSATABLE, False, False)
        found = harness.a5_coverage()
        assert any("the checker reports" in f for f in found), found
    finally:
        harness._A5_ROWS.clear()
        harness._A5_ROWS.update(kept)


def test_the_ratchet_refuses_a_row_outside_its_own_domain(harness):
    kept = dict(harness._A5_ROWS)
    try:
        harness._A5_ROWS[("tests/tenants.rvl", "x", "db.insert")] = (
            None, False, True)
        found = harness.a5_coverage()
        assert any("outside its own domain" in f for f in found), found
    finally:
        harness._A5_ROWS.clear()
        harness._A5_ROWS.update(kept)


def test_the_ratchet_refuses_a_drifted_restated_table(harness):
    kept = dict(harness._A5_REVERSIBILITY)
    try:
        harness._A5_REVERSIBILITY["ui.text"] = "reversible"
        found = harness.a5_coverage()
        assert any("has drifted from the checker" in f for f in found), found
    finally:
        harness._A5_REVERSIBILITY.clear()
        harness._A5_REVERSIBILITY.update(kept)


def test_an_unreadable_refusal_is_a_finding_never_a_silent_skip(harness):
    """A G4/reversibility refusal whose sentence is not one of the two the
    registry prints cannot be turned into a row; it must be recorded as a
    finding rather than skipped, or the row could shrink to vacuity by
    simply failing to read."""
    harness._A5_UNREADABLE["examples/rejections/a9_provide_key_not_declared.rvl"] = (
        "not one of the two sentences `ui_family.teardown_refusal` prints")
    try:
        found = harness.a5_coverage()
        assert any("a9_provide_key_not_declared" in f for f in found), found
    finally:
        harness._A5_UNREADABLE.clear()


# --------------------------------------------- the Lean side, as text

def test_the_oracle_decides_the_row_with_the_model_legal_cols():
    """The verdict row is the L2 file's own `legalCols` over the two STRING
    columns the row carries, bridged by `a5RowB_iff`, and that bridge is under
    the axioms gate."""
    src = ORACLE.read_text()
    assert re.search(
        r"def a5RowB .*:=\s*RevL\.A5\.legalCols r\.token r\.compensate", src)
    assert "#print axioms RevLOracle.a5RowB_iff" in src
    assert "#print axioms RevLOracle.a5RowBAll_iff" in src
    assert "RevLOracle.a5RowB_iff" in GATE.read_text()


def test_the_l2_file_states_both_directions_and_the_boundaries():
    """Two directions matching the registry's two rules, the three boundaries
    of reading the class off a token, and no `sorry`."""
    src = LEAN.read_text()
    assert re.search(r"def Registers \(d : Decl\) : Prop :=", src)
    assert re.search(r"def ClaimsNothing \(d : Decl\) : Prop :=", src)
    assert re.search(r"def Legal \(d : Decl\) : Prop := Registers d ∧ ClaimsNothing d", src)
    assert "theorem compensatable_without_compensate_refused" in src
    assert "theorem no_inverse_may_not_declare" in src
    assert "theorem reversible_untouched" in src
    assert "theorem rung_is_not_an_escape_hatch" in src
    assert "theorem valuation_does_not_move_the_class" in src
    assert "theorem outside_the_family_is_out_of_the_row" in src
    assert "theorem a5_not_vacuous" in src
    assert "theorem witness_bites" in src
    assert "sorry" not in src
