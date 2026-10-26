"""The EV row of the formal differential harness: the G4 emission value rule
(issues #2842 and #2834).

An emission extern, or a `fn` that reaches one, may be reached only by a
call. The checker refuses it handed on as a function value in a `fn` body or
a component, and an arrow calling it in a `fn` body (code G4, category
`emission-value`). `formal/harness/diff_corpus.py` exports one `EVR` fact per
reach (scope, position), and two sides decide the rule over them:

  * the Lean oracle prints `RevL.G4EmissionValue.emissionValueB`, bridged by
    `emissionValueB_iff` to the declarative `EmissionValueOK`;
  * the Python reference recomputes it from the same TSV in
    `reference_from_tsv`.

The checker-alignment arm files each refused fixture under `agree-G4` (or
the fatal `missed-G4`). Before this row the rule's refusals had no model row
and fell to the marker row, which reported them `missed-G4`.

`make formal` diffs the two sides and needs a Lean toolchain; this module
runs in the plain `pytest tests/` job and pins the Python half.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from _load_by_path import load_by_path  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "emission_value"
REFUSED = sorted(str(p.relative_to(ROOT)) for p in FIXTURES.glob("g4_*.rvl"))
ADMITTED = sorted(str(p.relative_to(ROOT)) for p in FIXTURES.glob("ok_*.rvl"))
REJECTION = "examples/rejections/g4_first_class_emission_value.rvl"
LEAN = ROOT / "formal" / "RevL" / "Theorems" / "G4_EmissionValue.lean"
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"
CHECK = ROOT / "formal" / "CheckAxioms.lean"
NONVACUITY = ROOT / "formal" / "scripts" / "nonvacuity.tsv"
STATUS = ROOT / "formal" / "STATUS.md"


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_emission_value",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def exported(harness):
    return harness.export()


@pytest.fixture(scope="module")
def verdicts(harness, exported):
    return harness.reference_from_tsv(exported[0])


def _evr(tsv, rel):
    return sorted((r[2], r[3], r[4], r[5]) for r in (row.split("\t") for row in tsv)
                  if r[0] == "EVR" and r[1] == rel)


# ------------------------------------------------------------- the premise

def test_the_corpus_has_the_three_refused_and_one_admitted_file():
    assert len(REFUSED) == 3 and len(ADMITTED) == 1


@pytest.mark.parametrize("rel", REFUSED + [REJECTION])
def test_the_checker_refuses_each_fixture_under_the_rule(harness, rel):
    assert harness.checker_code(rel) == ("G4", "emission-value")


@pytest.mark.parametrize("rel", ADMITTED)
def test_the_checker_admits_the_admitted_fixture(harness, rel):
    assert harness.checker_code(rel) == ("accept", "")


# ------------------------------------------------------------- the fact row

def test_the_reaches_are_exported_by_scope_and_position(exported):
    tsv = exported[0]
    base = "tests/fixtures/emission_value/"
    assert _evr(tsv, base + "ok_emit_call.rvl") == [
        ("component", "C", "bill", "call"), ("component", "C", "send", "call"),
        ("fn", "bill", "send", "call")]
    # the `fn` reaching the emission is itself a reach, by name
    assert _evr(tsv, base + "g4_value_in_component.rvl") == [
        ("component", "C", "bill", "value"), ("fn", "bill", "send", "call")]
    # the budgeted extern of issue #2834, handed on by a `fn`
    assert _evr(tsv, base + "g4_value_in_fn_body.rvl") == [
        ("component", "C", "get", "call"), ("fn", "get", "call", "value")]
    assert _evr(tsv, base + "g4_arrow_in_fn_body.rvl") == [
        ("component", "C", "mk", "call"), ("fn", "mk", "send", "arrow")]
    assert ("component", "C", "send", "value") in _evr(tsv, REJECTION)


def test_a_pure_extern_value_is_not_a_reach(harness):
    from revl.parser import Parser
    prog = Parser(
        "extern pure fn shout(x: Str) -> Str = @py { return x }\n"
        "fn pick() -> (Str) -> Str { return shout }\n", "p.rvl").parse()
    assert harness.emission_value_rows(prog, "p.rvl") == []


def test_every_evr_row_names_a_known_scope_and_position(harness, exported):
    for row in exported[0]:
        r = row.split("\t")
        if r[0] == "EVR":
            assert r[2] in harness.EMISSION_VALUE_SCOPES
            assert r[5] in harness.EMISSION_VALUE_POSITIONS


# ----------------------------------------------------------- the verdicts

@pytest.mark.parametrize("rel", REFUSED + [REJECTION])
def test_each_refused_fixture_fails_the_ev_row(verdicts, rel):
    assert verdicts.ev[rel] == "fail"


@pytest.mark.parametrize("rel", ADMITTED)
def test_each_admitted_fixture_passes_the_ev_row(verdicts, rel):
    assert verdicts.ev[rel] == "ok"


def test_the_ev_row_is_compared_and_counted(harness, verdicts):
    assert "ev" in harness.Verdicts._fields
    rel = REFUSED[0]
    parsed = harness.parse_verdicts(f"EV\t{rel}\temission_value=fail\n")
    assert parsed.ev[rel] == "fail"
    assert verdicts.total() == sum(len(getattr(verdicts, f))
                                   for f in harness.Verdicts._fields)


# ----------------------------------------------------- checker alignment

def test_an_emission_value_refusal_needs_the_ev_row(harness, capsys):
    """Without an EV `fail` the refusal is the fatal `missed-G4`; with it,
    `agree-G4`."""
    rel = REFUSED[0]
    empty = harness.Verdicts(**{f: {} for f in harness.Verdicts._fields})
    assert harness.checker_alignment({rel: {}}, [], empty) == [f"missed-G4: {rel}"]
    capsys.readouterr()
    seen = empty._replace(ev={rel: "fail"})
    assert harness.checker_alignment({rel: {}}, [], seen) == []
    capsys.readouterr()


def test_an_ev_fail_on_an_accepted_file_is_formal_strict(harness, capsys):
    rel = ADMITTED[0]
    stricter = harness.Verdicts(**{f: {} for f in harness.Verdicts._fields})._replace(
        ev={rel: "fail"})
    assert harness.checker_alignment({rel: {}}, [], stricter) == [f"formal-strict: {rel}"]
    capsys.readouterr()


def test_the_coverage_ratchet_has_its_witnesses(harness, verdicts):
    assert harness.emission_value_coverage() == []


def test_the_coverage_ratchet_bites(harness, monkeypatch):
    monkeypatch.setattr(harness, "_EMISSION_VALUE_FILES", {})
    assert len(harness.emission_value_coverage()) == 4


# ------------------------------------------------------------ the Lean side

def test_the_model_states_the_rule_and_every_gate_registers_it():
    lean = LEAN.read_text(encoding="utf-8")
    check = CHECK.read_text(encoding="utf-8")
    gate = GATE.read_text(encoding="utf-8")
    registry = NONVACUITY.read_text(encoding="utf-8")
    status = STATUS.read_text(encoding="utf-8")
    for name in ("legalB_iff", "emissionValueB_iff", "value_never_legal",
                 "arrow_in_fn_refused", "call_always_legal", "refused_of_mem",
                 "fixtures_decided", "emission_value_not_vacuous"):
        assert f"theorem {name}" in lean
        full = f"RevL.G4EmissionValue.{name}"
        assert full in check and full in gate and full in status
        assert f"\n{full}\t" in registry
    assert "sorry" not in lean
    oracle = ORACLE.read_text(encoding="utf-8")
    assert "RevL.G4EmissionValue.emissionValueB" in oracle
    assert "emissionValueOKB_iff" in oracle
    assert "RevLOracle.emissionValueOKB_iff" in gate
