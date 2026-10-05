"""The DF row of the formal differential harness: the G4 deferred-position
rule (issue #1742).

A `deferred` emission extern may be reached only by a call in a component.
The checker refuses it called in a `fn` or `test` body, inside an arrow
there, or passed as a function value anywhere (code G4, category
`deferred`). `formal/harness/diff_corpus.py` exports one `DR` fact per reach
(scope, position), and two sides decide the rule over them:

  * the Lean oracle prints `RevL.G4Deferred.deferredB`, bridged by
    `deferredB_iff` to the declarative `DeferredOK`;
  * the Python reference recomputes it from the same TSV in
    `reference_from_tsv`.

The checker-alignment arm files each refused fixture under `agree-G4` (or
the fatal `missed-G4`). Before this row the six refusals sat in the
ratcheted `out-of-fragment-deferred` bucket. That bucket is gone, and the
ledger no longer names them.

`make formal` diffs the two sides and needs a Lean toolchain; this module
runs in the plain `pytest tests/` job and pins the Python half.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from _load_by_path import load_by_path  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "deferred_reach"
REFUSED = sorted(str(p.relative_to(ROOT)) for p in FIXTURES.glob("g4_*.rvl"))
ADMITTED = sorted(str(p.relative_to(ROOT)) for p in FIXTURES.glob("ok_*.rvl"))
LEAN = ROOT / "formal" / "RevL" / "Theorems" / "G4_DeferredPosition.lean"
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"
CHECK = ROOT / "formal" / "CheckAxioms.lean"
LEDGER = ROOT / "formal" / "out_of_fragment_ledger.json"
STATUS = ROOT / "formal" / "STATUS.md"


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_deferred",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def exported(harness):
    return harness.export()


@pytest.fixture(scope="module")
def verdicts(harness, exported):
    return harness.reference_from_tsv(exported[0])


def _dr(tsv, rel):
    return sorted((r[2], r[3], r[4], r[5]) for r in (row.split("\t") for row in tsv)
                  if r[0] == "DR" and r[1] == rel)


# ------------------------------------------------------------- the premise

def test_the_corpus_has_the_six_refused_and_three_admitted_files():
    assert len(REFUSED) == 6 and len(ADMITTED) == 3


@pytest.mark.parametrize("rel", REFUSED)
def test_the_checker_refuses_each_fixture_under_the_rule(harness, rel):
    assert harness.checker_code(rel) == ("G4", "deferred")


# ------------------------------------------------------------- the fact row

def test_the_reaches_are_exported_by_scope_and_position(exported):
    tsv = exported[0]
    assert _dr(tsv, "tests/fixtures/deferred_reach/g4_call_in_fn_body.rvl") == [
        ("fn", "bill", "deliver", "call")]
    assert _dr(tsv, "tests/fixtures/deferred_reach/g4_call_in_arrow_in_fn_body.rvl") == [
        ("fn", "run", "deliver", "arrow")]
    assert ("component", "Agent", "deliver", "value") in _dr(
        tsv, "tests/fixtures/deferred_reach/g4_value_in_component.rvl")
    assert _dr(tsv, "tests/fixtures/deferred_reach/ok_emit_step.rvl") == [
        ("component", "Agent", "deliver", "call")]
    # a plain extern passed as a value is not a reach of a deferred one
    assert _dr(tsv, "tests/fixtures/deferred_reach/ok_plain_value_in_fn_body.rvl") == [
        ("component", "Agent", "deliver", "call")]


def test_every_dr_row_names_a_known_scope_and_position(harness, exported):
    for row in exported[0]:
        r = row.split("\t")
        if r[0] == "DR":
            assert r[2] in harness.DEFERRED_SCOPES and r[5] in harness.DEFERRED_POSITIONS


# ----------------------------------------------------------- the verdicts

@pytest.mark.parametrize("rel", REFUSED)
def test_each_refused_fixture_fails_the_df_row(verdicts, rel):
    assert verdicts.deferred[rel] == "fail"


@pytest.mark.parametrize("rel", ADMITTED)
def test_each_admitted_fixture_passes_the_df_row(verdicts, rel):
    assert verdicts.deferred[rel] == "ok"


def test_the_df_row_is_compared_and_counted(harness, verdicts):
    assert "deferred" in harness.Verdicts._fields
    rel = REFUSED[0]
    assert harness.parse_verdicts(f"DF\t{rel}\tdeferred=fail\n").deferred[rel] == "fail"
    assert verdicts.total() == sum(len(getattr(verdicts, f))
                                   for f in harness.Verdicts._fields)


# ----------------------------------------------------- checker alignment

def test_a_deferred_refusal_needs_the_df_row(harness, capsys):
    """Without a DF `fail` the refusal is the fatal `missed-G4`; with it,
    `agree-G4`. The ratcheted `out-of-fragment-deferred` bucket is gone."""
    rel = REFUSED[0]
    empty = harness.Verdicts(**{f: {} for f in harness.Verdicts._fields})
    assert harness.checker_alignment({rel: {}}, [], empty) == [f"missed-G4: {rel}"]
    capsys.readouterr()
    seen = empty._replace(deferred={rel: "fail"})
    assert harness.checker_alignment({rel: {}}, [], seen) == []
    assert "agree-G4" in harness._ALIGN
    capsys.readouterr()


def test_a_df_fail_on_an_accepted_file_is_formal_strict(harness, capsys):
    rel = ADMITTED[0]
    stricter = harness.Verdicts(**{f: {} for f in harness.Verdicts._fields})._replace(
        deferred={rel: "fail"})
    assert harness.checker_alignment({rel: {}}, [], stricter) == [f"formal-strict: {rel}"]
    capsys.readouterr()


def test_the_coverage_ratchet_has_its_witnesses(harness, verdicts):
    assert harness.deferred_coverage() == []


def test_the_coverage_ratchet_bites(harness, monkeypatch):
    monkeypatch.setattr(harness, "_DEFERRED_FILES", {})
    assert len(harness.deferred_coverage()) == 4


# ------------------------------------------------------- the ledger and docs

def test_the_ledger_no_longer_names_the_rule(harness):
    doc = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert "out-of-fragment-deferred" not in doc
    assert "out-of-fragment-deferred" not in harness.OOF_RATCHET_BUCKETS
    assert not any(rel in names for key, names in doc.items() if key != "_about"
                   for rel in REFUSED)


def test_the_model_states_the_rule_and_every_gate_registers_it():
    lean = LEAN.read_text(encoding="utf-8")
    for name in ("legalB_iff", "deferredB_iff", "deferred_not_vacuous"):
        assert f"theorem {name}" in lean
        assert f"RevL.G4Deferred.{name}" in CHECK.read_text(encoding="utf-8")
        assert f"RevL.G4Deferred.{name}" in GATE.read_text(encoding="utf-8")
    assert "sorry" not in lean
    oracle = ORACLE.read_text(encoding="utf-8")
    assert "RevL.G4Deferred.deferredB" in oracle and "deferredOKB_iff" in oracle
    assert "RevLOracle.deferredOKB_iff" in GATE.read_text(encoding="utf-8")
    assert "out-of-fragment-deferred" not in STATUS.read_text(encoding="utf-8")
