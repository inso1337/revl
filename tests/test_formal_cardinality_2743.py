"""The CARD row of the formal differential harness (issue #2743).

`src/revl/cardinality.py` reports, per component and capability, a finite
crossing count or `unbounded`. `formal/harness/diff_corpus.py` exports every
finite report as a crossing command (`CT` rows) beside the number the checker
reported (`CR` row); the Lean oracle decides `RevL.Cardinality.bound cmd =
reported` (`RevLOracle.cardRowB`, bridged by `cardRowB_iff`) and the Python
reference folds the same rows (`_card_fold`).

Both sides print `fail` for a disagreement, so they agree with each other and
the differential cannot see it. `cardinality_coverage` is what turns a
disagreement into a finding. That job needs a Lean toolchain, so on a machine
without one nothing collected the Python half; this module runs in the plain
`pytest tests/` job and pins it: a disagreeing row is a finding, the reference
derives the retry multiplier and the branch maximum itself (a change to the
checker's own arithmetic goes red), and the Lean side is registered.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from _load_by_path import load_by_path  # noqa: E402

from revl import cardinality as card_module  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

LEAN = ROOT / "formal" / "RevL" / "Theorems" / "Cardinality.lean"
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"
CHECK = ROOT / "formal" / "CheckAxioms.lean"
REGISTRY = ROOT / "formal" / "scripts" / "nonvacuity.tsv"

#: One capability crossed twice by a branch's worse arm and once by the other,
#: then once by a validated model call that may retry twice (3 crossings).
BRANCH_AND_RETRY = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
service DB { emission[db] fn exec(s: Str) -> Int }
service Model { emission[model] validated retry 2 fn complete(h: List[Str]) -> AgentTurn }
service Svc { emission fn run(flag: Bool) -> Int }
component C requires db: DB, model: Model provides svc: Svc {
  provide svc {
    fn run(flag) {
      if (flag) { emit db.exec("a"); emit db.exec("b") } else { emit db.exec("c") }
      let t = emit model.complete(["p"])
      return 0
    }
  }
}
"""


@pytest.fixture(scope="module")
def harness():
    return load_by_path(
        "formal_diff_corpus_cardinality_2743",
        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture()
def fresh(harness, monkeypatch):
    """The module-level state the row fills, replaced for one test."""
    monkeypatch.setattr(harness, "_CARD_ROWS", {})
    monkeypatch.setattr(harness, "_CARD_CENSUS", {})
    monkeypatch.setattr(harness, "_CARD_UNMODELLED", {})
    return harness


def _rows(harness):
    """The exported rows of the program above, as `{token: (tree, reported)}`."""
    doc = compile_source(BRANCH_AND_RETRY, "t.rvl")
    harness.cardinality_rows("t.rvl", doc)
    return {token: (tree, reported)
            for (_rel, _comp, token), (tree, reported)
            in harness._CARD_ROWS.items()}


def test_the_checker_and_the_fold_agree_on_a_branch_and_a_retry(fresh):
    rows = _rows(fresh)
    # the worse arm of the branch, not the sum of both
    assert rows["db"][1] == 2
    assert fresh._card_fold(rows["db"][0]) == 2
    # the validated retry 2 multiplier: the first attempt plus two retries
    assert rows["model"][1] == 3
    assert fresh._card_fold(rows["model"][0]) == 3
    findings = fresh.cardinality_coverage()
    assert not [f for f in findings if "disagree" in f], findings


def test_a_row_whose_fold_differs_from_the_report_is_a_finding(fresh):
    """The reference and the oracle both print `fail` for it and so agree; the
    finding is what makes the gate red, and it names file, component and
    capability."""
    fresh._CARD_ROWS[("f.rvl", "Comp", "db")] = (
        ("s", ("x",), ("x",)), 3)
    findings = fresh.cardinality_coverage()
    hit = [f for f in findings if "f.rvl: Comp: capability 'db'" in f]
    assert hit and "the model counts 2 and the checker reports 3" in hit[0]


def test_a_verdict_either_side_calls_fail_is_a_finding(fresh):
    key = ("f.rvl", "Comp", "db")
    fresh._CARD_ROWS[key] = (("s", ("x",), ("x",)), 2)
    assert not [f for f in fresh.cardinality_coverage() if "disagree" in f]
    for side in ("reference", "formal"):
        findings = fresh.cardinality_coverage(
            **{side: {key: "fail"}})
        hit = [f for f in findings if f"the {side} row is `fail`" in f]
        assert hit, (side, findings)


def test_the_reference_derives_the_retry_multiplier_itself(fresh, monkeypatch):
    """A change to the checker's `_retry_mult` must not be a silent agreement:
    the reference reads `retry` off the IR, so the row goes red."""
    monkeypatch.setattr(
        card_module, "_retry_mult", lambda spec: (spec.get("retry") or 0) + 2)
    rows = _rows(fresh)
    tree, reported = rows["model"]
    assert reported == 4
    assert fresh._card_fold(tree) == 3
    findings = fresh.cardinality_coverage()
    assert any("capability 'model'" in f and "disagree" in f
               for f in findings), findings


def test_the_reference_takes_the_branch_maximum_itself(fresh, monkeypatch):
    """The same for the branch: if the checker summed the arms the reference,
    which takes the worse arm, would disagree."""
    def summing(into, others):
        for other in others:
            card_module._merge_sum(into, other)

    monkeypatch.setattr(card_module, "_merge_max", summing)
    rows = _rows(fresh)
    tree, reported = rows["db"]
    assert reported == 3
    assert fresh._card_fold(tree) == 2
    assert any("capability 'db'" in f and "disagree" in f
               for f in fresh.cardinality_coverage())


def test_the_ratchet_needs_a_sequence_and_a_branch(fresh):
    fresh._CARD_ROWS[("f.rvl", "Comp", "db")] = (("x",), 1)
    findings = fresh.cardinality_coverage()
    assert any("no exported command has a sequence" in f for f in findings)
    assert any("no exported command has a branch" in f for f in findings)


def test_an_empty_row_is_a_finding_and_not_a_pass(fresh):
    findings = fresh.cardinality_coverage()
    assert findings and "no CARD row at all" in findings[0]


def test_a_bound_arrow_is_declined_and_not_counted_as_nothing(fresh):
    """The checker follows a bound arrow's body at each invocation and reports
    a finite count (issue #1757); the mirror does not follow it, so it exports
    NO row for that capability rather than a count of nothing."""
    source = """
service Model { emission[model] fn complete(m: List[Str]) -> Str }
service Loop { emission[model] fn run(s: Str) -> Str }
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(x) -> Str {
    let step = m => emit model.complete(m)
    let a = step([x])
    return step([x])
  } }
}
"""
    doc = compile_source(source, "t.rvl")
    reported = card_module.cardinality(doc)["Agent"]["per_capability"]["model"]
    assert reported == {"bound": 2, "kind": "bounded"}
    rows = fresh.cardinality_rows("t.rvl", doc)
    assert not [r for r in rows if r.startswith("CR\t")]
    assert fresh._CARD_CENSUS.get("unmodelled") == 1
    assert not fresh._CARD_ROWS


# ------------------------------------------------ the Lean side, as text

def test_the_lean_side_is_registered_and_gated():
    """Every `RevL.Cardinality` theorem `CheckAxioms.lean` prints is in
    `run_gate.sh`'s argv, in the registry, and stated in the L2 file; the
    oracle decides the row with `cardRowB` and its bridge is in the second
    argv list."""
    lean = LEAN.read_text(encoding="utf-8")
    gate = GATE.read_text(encoding="utf-8")
    check = re.findall(
        r"^#print axioms (RevL\.Cardinality\.[A-Za-z0-9_']+)\s*$",
        CHECK.read_text(encoding="utf-8"), re.MULTILINE)
    assert check, "CheckAxioms.lean prints no RevL.Cardinality theorem"
    registry = {line.split("\t")[0]
                for line in REGISTRY.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")}
    for name in check:
        short = name.rsplit(".", 1)[1]
        assert re.search(rf"^theorem {short}\b", lean, re.MULTILINE), name
        assert f"  {name}" in gate, name
        assert name in registry, name
    for name in ("RevL.Cardinality.runs_le_bound",
                 "RevL.Cardinality.cardinality_not_vacuous",
                 "RevL.Cardinality.body_bound_bounds_runs"):
        assert name in check
    oracle = ORACLE.read_text(encoding="utf-8")
    assert "def cardRowB (nodes : List CTRow) (root bound : Nat) : Bool" in oracle
    assert 'if cardRowB nodes c.root c.bound then "ok" else "fail"' in oracle
    assert "#print axioms RevLOracle.cardRowB_iff" in oracle
    assert "  RevLOracle.cardRowB_iff" in gate


def test_the_branch_witness_separates_a_maximum_from_a_sum():
    """Its arms cross twice and once: a branch that summed them would report
    3, and one that took the lesser arm 1. Equal-bound arms would not tell the
    three apart."""
    lean = LEAN.read_text(encoding="utf-8")
    assert re.search(
        r"def branchWitness : Cmd := \.branch \(\.seq \.cross \.cross\) \.cross",
        lean)
    assert "bound branchWitness = 2" in lean
    assert "¬ Runs branchWitness 3" in lean
