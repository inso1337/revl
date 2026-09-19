"""The A2 row of the formal differential harness (issue 1166).

`formal/harness/diff_corpus.py` exports one `AQ` fact row per activation-body
statement, each as the A2 rule sees it (`acquire` / `provide` / `other`), and
two sides fold the rule over them:

  * the Lean oracle prints `RevL.A2.a2B` — the checker's own fold
    (`lower._dispatch_action`: a flag set at the first `provide`, an
    acquisition refused while it is set), bridged by `a2B_iff` to the
    declarative rule that is the hypothesis of
    `RevL.A2.withdrawals_precede_releases`;
  * the Python reference recomputes the same fold from the same TSV, in
    `reference_from_tsv`, without reading either side's verdict.

The two are diffed by `make formal`, and the checker-alignment arm files the
fixture under `agree-A2` (or the fatal `missed-A2`). That job needs a Lean
toolchain, so on a machine without one nothing collected the Python half:
the step classifier, the reference fold, the alignment arm and the coverage
ratchet could all move with no test failing. This module runs in the plain
`pytest tests/` job and pins them, the way `test_formal_attenuation_namespace`
pins the `P` / `W` rows.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: The refused shape: an acquisition, a provision, then a second acquisition.
FIXTURE = "examples/rejections/a2_acquire_after_provide.rvl"
FIXTURE_COMP = "BadOrder"

#: An admitted body that exercises the rule — a `handoff` prelude, one
#: acquisition, then the `provide` block — so the fold's flag is set and the
#: acquisition is checked against it, rather than the vacuous `ok` of a body
#: with nothing to order.
ADMITTED = "examples/handoff_cache.rvl"
ADMITTED_COMP = "Cache"

#: The four statement forms the checker refuses after a provision, in one
#: body, so every arm of the classifier is exercised even though no corpus
#: file iterates a stream in its activation body.
FOUR_FORMS = """
service Sink { emission fn write(v: Str) }
component Iterate requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  effect Map.new() undo Map.new()
  every 5s { emit sink.write("tick") }
  let sub = subscribe src undo sub.close()
  every o in sub { emit sink.write(o) }
}
"""

LEAN = ROOT / "formal" / "RevL" / "Theorems" / "A2_NoAcquisitionAfterProvision.lean"
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"
CHECK = ROOT / "formal" / "CheckAxioms.lean"
REGISTRY = ROOT / "formal" / "scripts" / "nonvacuity.tsv"


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "formal_diff_corpus_a2", ROOT / "formal" / "harness" / "diff_corpus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tsv(harness):
    rows, _facts, _census = harness.export()
    return rows


@pytest.fixture(scope="module")
def verdicts(harness, tsv):
    return harness.reference_from_tsv(tsv)


def _aq(tsv, rel, comp):
    """The `(ord, kind)` body steps of one component, in body order."""
    out = []
    for row in tsv:
        r = row.split("\t")
        if r[0] == "AQ" and r[1] == rel and r[2] == comp:
            out.append((int(r[3]), r[4]))
    return sorted(out)


def _checker_code(rel: str) -> str:
    from revl.compiler import compile_source
    from revl.diagnostics import classify
    from revl.errors import RevlError

    try:
        compile_source((ROOT / rel).read_text(encoding="utf-8"), rel)
        return "accept"
    except RevlError as e:
        return classify(e).get("code") or "UNCODED"


# ------------------------------------------------------------- the premise

def test_the_checker_refuses_the_fixture_under_a2():
    """If this stops refusing, the rest of the module measures the wrong
    thing and should be read again, not deleted."""
    assert _checker_code(FIXTURE) == "A2"


def test_the_checker_accepts_the_admitted_file():
    assert _checker_code(ADMITTED) == "accept"


# ----------------------------------------------------------- the fact row

def test_the_fixture_exports_the_refused_shape(tsv):
    """`BadOrder`: `store` acquired, `cache` provided, `extra` acquired."""
    assert _aq(tsv, FIXTURE, FIXTURE_COMP) == [
        (0, "acquire"), (1, "provide"), (2, "acquire")]


def test_the_admitted_body_has_both_shapes_in_order(tsv):
    """Non-vacuity for the admitted pin: the flag is set (a provision) and an
    acquisition is checked against it (it sits above). A body with no
    provision, or none, would be an `ok` the fold never earned."""
    steps = _aq(tsv, ADMITTED, ADMITTED_COMP)
    kinds = [k for _o, k in steps]
    assert "acquire" in kinds and "provide" in kinds
    assert kinds.index("acquire") < kinds.index("provide")
    assert kinds[-1] == "provide"


def test_every_body_statement_is_one_row(tsv, harness):
    """The rows are the body, whole: one per statement, indices contiguous
    from 0, so a step cannot fall out between the exporter and the fold."""
    from revl.errors import RevlError
    from revl.parser import Parser

    bodies: dict[tuple[str, str], list[int]] = {}
    for row in tsv:
        r = row.split("\t")
        if r[0] == "AQ":
            bodies.setdefault((r[1], r[2]), []).append(int(r[3]))
    assert bodies, "no AQ rows exported"
    for (rel, comp), ords in bodies.items():
        assert sorted(ords) == list(range(len(ords))), (rel, comp)
    # ... and the count is the parser's own body length, on the two pins.
    for rel, comp in ((FIXTURE, FIXTURE_COMP), (ADMITTED, ADMITTED_COMP)):
        try:
            prog = Parser((ROOT / rel).read_text(encoding="utf-8"), rel).parse()
        except RevlError:  # pragma: no cover — both pins parse
            raise
        body = next(c.body for c in prog.components if c.name == comp)
        assert len(bodies[(rel, comp)]) == len(body)


def test_the_classifier_names_the_checkers_four_forms(harness):
    """The `acquire` class is exactly the four statement forms
    `lower._dispatch_action` refuses after a provision: `let … = effect`, a
    bare `effect`, a timer and an `every … in` iteration. A `subscribe` is
    a `LetEffect` too. Checked on parser nodes, because no corpus file puts
    a stream iteration in an activation body."""
    from revl.parser import (
        EffectStmt, LetEffect, Parser, StreamIterStmt, TimerStmt)

    prog = Parser(FOUR_FORMS, "<four-forms>").parse()
    body = prog.components[0].body
    classes = [type(s) for s in body]
    assert LetEffect in classes and EffectStmt in classes
    assert TimerStmt in classes and StreamIterStmt in classes
    assert [harness._a2_step(s) for s in body] == ["acquire"] * len(body)


# ------------------------------------------------------- the reference fold

def test_the_reference_refuses_the_fixture(verdicts):
    assert verdicts.a2[(FIXTURE, FIXTURE_COMP)] == "fail"


def test_the_reference_admits_the_admitted_body(verdicts):
    assert verdicts.a2[(ADMITTED, ADMITTED_COMP)] == "ok"


def test_the_fixture_is_the_only_refusal(verdicts):
    """The corpus refuses exactly the shape A2 names. A second `fail` here
    is either a new fixture (add it to this list) or the fold drifting."""
    assert [k for k, x in verdicts.a2.items() if x == "fail"] == [
        (FIXTURE, FIXTURE_COMP)]


def test_every_component_has_one_a2_verdict(tsv, verdicts):
    """One verdict per `M` row, an empty body included, so the Lean side and
    the reference count the same rows and none is `no formal row`."""
    comps = {(r[1], r[2]) for r in (row.split("\t") for row in tsv)
             if r[0] == "M"}
    assert set(verdicts.a2) == comps


def test_the_fold_is_order_sensitive(harness):
    """The rule is about ORDER, so the reference must read the `ord` column
    and not the row order: the fixture's rows shuffled still refuse, and
    the same three steps re-indexed with the provision last admit."""
    mrow = "\t".join(["M", "x.rvl", "C", "", "k", "", "member"])
    shuffled = [mrow,
                "\t".join(["AQ", "x.rvl", "C", "2", "acquire"]),
                "\t".join(["AQ", "x.rvl", "C", "0", "acquire"]),
                "\t".join(["AQ", "x.rvl", "C", "1", "provide"])]
    assert harness.reference_from_tsv(shuffled).a2[("x.rvl", "C")] == "fail"
    reindexed = [mrow,
                 "\t".join(["AQ", "x.rvl", "C", "2", "provide"]),
                 "\t".join(["AQ", "x.rvl", "C", "0", "acquire"]),
                 "\t".join(["AQ", "x.rvl", "C", "1", "acquire"])]
    assert harness.reference_from_tsv(reindexed).a2[("x.rvl", "C")] == "ok"
    # `other` steps move nothing, on either side of the provision.
    padded = [mrow,
              "\t".join(["AQ", "x.rvl", "C", "0", "other"]),
              "\t".join(["AQ", "x.rvl", "C", "1", "acquire"]),
              "\t".join(["AQ", "x.rvl", "C", "2", "provide"]),
              "\t".join(["AQ", "x.rvl", "C", "3", "other"])]
    assert harness.reference_from_tsv(padded).a2[("x.rvl", "C")] == "ok"


# -------------------------------------------------- the alignment arm

def test_missed_a2_is_a_gate_failure(harness):
    assert "missed-A2" in harness.FATAL_BUCKETS


@pytest.fixture
def quiet_alignment(harness, monkeypatch, tmp_path):
    """`checker_alignment` writes its no-manifest list under `FORMAL`;
    point that at a scratch directory so the test leaves the tree alone."""
    (tmp_path / "harness" / "out").mkdir(parents=True)
    monkeypatch.setattr(harness, "FORMAL", tmp_path)
    return harness


def test_the_fixture_is_filed_under_agree_a2(quiet_alignment, verdicts, capsys):
    fatal = quiet_alignment.checker_alignment({FIXTURE: {}}, [], verdicts)
    out = capsys.readouterr().out
    assert fatal == []
    assert re.search(r"^\s+agree-A2\s+1$", out, re.MULTILINE), out


def test_a_blind_row_is_filed_under_missed_a2(quiet_alignment, verdicts, capsys):
    """The arm bites: with the A2 row reading `ok` on the fixture, the
    checker's refusal has nothing to agree with and the bucket is the fatal
    one. This is the `missed-A2` the pre-change arm could not report."""
    blind = verdicts._replace(a2={k: "ok" for k in verdicts.a2})
    fatal = quiet_alignment.checker_alignment({FIXTURE: {}}, [], blind)
    out = capsys.readouterr().out
    assert fatal == [f"missed-A2: {FIXTURE}"]
    assert re.search(r"^\s+missed-A2\s+1\s+FATAL$", out, re.MULTILINE), out


def test_a_model_refusal_on_an_accepted_file_is_formal_strict(
        quiet_alignment, verdicts, capsys):
    """`formal_clean` reads the row: a model `fail` where the checker
    accepts is the informational `formal-strict`, not `agree-accept`."""
    strict = verdicts._replace(
        a2={**verdicts.a2, (ADMITTED, ADMITTED_COMP): "fail"})
    fatal = quiet_alignment.checker_alignment({ADMITTED: {}}, [], strict)
    out = capsys.readouterr().out
    assert fatal == []
    assert f"ALIGN formal-strict: {ADMITTED}" in out


# ------------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, tsv):
    """`a2_coverage` reads the module state the LAST `reference_from_tsv`
    filled, so recompute over the real corpus first (the order-sensitivity
    test above folds synthetic rows through the same function)."""
    harness.reference_from_tsv(tsv)
    assert harness.a2_coverage() == []


def test_the_ratchet_bites_without_the_refused_shape(harness, tsv):
    """Drop the fixture's rows and the corpus no longer exercises the
    refusal, which the ratchet must say rather than let the row agree over
    admitted bodies only. Then restore the module-level state."""
    without = [row for row in tsv if f"\t{FIXTURE}\t" not in row]
    harness.reference_from_tsv(without)
    findings = harness.a2_coverage()
    harness.reference_from_tsv(tsv)
    assert findings and "refused for an acquisition after a provision" in findings[0]


def test_the_ratchet_bites_without_an_admitted_body_with_both_shapes(harness, tsv):
    """The other witness: strip every provision out of the admitted bodies
    and the fold's flag is never set on an admitted body."""
    fixture_rows = [row for row in tsv if f"\t{FIXTURE}\t" in row]
    others = [row for row in tsv if f"\t{FIXTURE}\t" not in row
              and not (row.startswith("AQ\t") and row.endswith("\tprovide"))]
    harness.reference_from_tsv(others + fixture_rows)
    findings = harness.a2_coverage()
    harness.reference_from_tsv(tsv)
    assert findings and "admitted body with both a provision" in findings[0]


# ------------------------------------------------ the Lean side, as text

def test_the_lean_side_is_registered_and_gated():
    """Every `RevL.A2` theorem `CheckAxioms.lean` prints is in
    `run_gate.sh`'s argv, in the registry, and stated in the L2 file; the
    oracle decides the row with `a2OKB` and its bridge is in the second
    argv list. Text-level, because it is the statement that can drift
    silently without a toolchain."""
    lean = LEAN.read_text(encoding="utf-8")
    gate = GATE.read_text(encoding="utf-8")
    check = re.findall(r"^#print axioms (RevL\.A2\.[A-Za-z0-9_']+)\s*$",
                       CHECK.read_text(encoding="utf-8"), re.MULTILINE)
    assert check, "CheckAxioms.lean prints no RevL.A2 theorem"
    registry = {line.split("\t")[0]
                for line in REGISTRY.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")}
    for name in check:
        short = name.rsplit(".", 1)[1]
        assert re.search(rf"^theorem {short}\b", lean, re.MULTILINE), name
        assert f"  {name}" in gate, name
        assert name in registry, name
    for name in ("RevL.A2.a2B_iff", "RevL.A2.withdrawals_precede_releases",
                 "RevL.A2.fixture_opens_the_window"):
        assert name in check
    oracle = ORACLE.read_text(encoding="utf-8")
    assert "def a2OKB (steps : List RevL.A2.Step) : Bool := RevL.A2.a2B steps" in oracle
    assert 'if a2OKB steps then "ok" else "fail"' in oracle
    assert "#print axioms RevLOracle.a2OKB_iff" in oracle
    assert "  RevLOracle.a2OKB_iff" in gate
