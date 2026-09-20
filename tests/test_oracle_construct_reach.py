"""Non-vacuity, shape and ratchet checks for the construct-reach report."""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "oracle_construct_reach", ROOT / "tools" / "oracle_construct_reach.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_oracle_has_named_constructs_and_a_corpus():
    data = _tool().survey()
    assert set(data) == {"emit_py", "emit_ts", "emit_go", "emit_java", "emit_rust",
                         "emit_wasm", "lower_ir", "compile", "gate_census"}
    for name, report in data.items():
        assert report["reference"], f"{name} reference construct table is empty"
        assert report["corpus"], f"{name} corpus is empty"
        assert set(report["reached"]) | set(report["unreached"]) == set(report["reference"])


def test_emitter_report_names_the_extern_reach():
    report = _tool().survey()["emit_rust"]
    assert any("externs.rvl" in document for document in report["corpus"])
    assert report["reached"]


def test_new_reference_construct_is_not_silent(monkeypatch):
    coverage = _tool()._load_coverage()
    original = coverage.reference_constructs

    def with_new_construct(path):
        found = original(path)
        found["kind=issue_301_probe"] = 0
        return found

    monkeypatch.setattr(coverage, "reference_constructs", with_new_construct)
    problems = coverage.check(coverage.survey())
    assert any("kind=issue_301_probe" in problem for problem in problems)


# ------------------------------------------------- the shrink-only ratchet
# Issue #1203: the report printed 249 UNREACHED rows and `--check` could not
# fail on any of them, so these four tests are the gate. Each one is written
# to FAIL on a tree without the ledger: they assert an exit code and a named
# problem, not the presence of a printout.


@pytest.fixture(scope="module")
def data():
    return _tool().survey()


def test_the_committed_ledger_matches_this_tree(data):
    assert _tool().check(data) == []


def test_a_newly_unreached_construct_fails_the_check(data):
    """The regression this gate exists for: a reference dispatch nothing in the
    corpus spells any more. Take a construct the corpus really does reach and
    move it to `unreached`, exactly as deleting its only corpus document would."""
    tool = _tool()
    oracle = "emit_py"
    construct = next(iter(sorted(data[oracle]["reached"])))
    perturbed = copy.deepcopy(data)
    perturbed[oracle]["reached"].pop(construct)
    perturbed[oracle]["unreached"] = sorted(
        [*perturbed[oracle]["unreached"], construct])

    problems = tool.check(perturbed)
    assert any(construct in p and "NEWLY UNREACHED" in p for p in problems), problems
    assert tool.main(["--check"]) == 0  # and the real tree is still green


def test_a_stale_ledger_entry_fails_the_check(data, monkeypatch):
    """The other direction, which is what makes it shrink-only: a construct the
    corpus now reaches, still listed. The fix is to delete the line."""
    tool = _tool()
    oracle = "emit_py"
    reached = next(iter(sorted(data[oracle]["reached"])))
    ledger = tool._ledger()
    ledger[oracle] = sorted([*ledger[oracle], reached])
    monkeypatch.setattr(tool, "_ledger", lambda: ledger)

    problems = tool.check(data)
    assert any(reached in p and "only shrinks" in p for p in problems), problems


def test_a_vacuous_report_fails_even_when_the_ledger_matches(data):
    """Why `--check` keeps its vacuity condition. The ratchet compares unreached
    SETS, so an oracle whose unreached set is empty is outside it entirely: empty
    the reference table and the empty set still matches the empty ledger entry.
    Vacuity is the one failure the ratchet structurally cannot see."""
    tool = _tool()
    perturbed = copy.deepcopy(data)
    perturbed["gate_census"]["reference"] = []
    perturbed["gate_census"]["reached"] = {}
    perturbed["gate_census"]["unreached"] = []

    problems = tool.check(perturbed)
    assert [p for p in problems if "vacuous" in p], problems
    assert not [p for p in problems if "UNREACHED" in p or "shrinks" in p], problems


def test_the_ledger_records_names_only():
    """CI runs python 3.11 and the developer venv is 3.14. The ledger holds
    construct NAMES and no counts, totals or line numbers, so a `--write` from
    either interpreter is the same file -- the exposure that corrupted the
    self-host coverage ledger's ungated totals does not exist here."""
    raw = json.loads(
        (ROOT / "tests" / "fixtures" / "oracle_construct_reach_ledger.json").read_text())
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        assert isinstance(value, list)
        assert all(isinstance(name, str) for name in value), key


def test_the_compile_row_measures_the_corpus_its_oracle_runs_on(data):
    """The compile row read 5 of 5 unreached because it compared IR section
    names against `field=value` reach keys over a corpus scraped from every
    quoted `.rvl` literal in the oracle's source. Both halves are wired to the
    same vocabulary now, so the row is a measurement."""
    tool = _tool()
    report = data["compile"]
    assert set(report["reached"]) >= {"functions", "components", "externs", "types"}

    spec = importlib.util.spec_from_file_location(
        "compile_oracle_corpus", ROOT / "tests" / "test_selfhost_compile.py")
    oracle = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = oracle
    spec.loader.exec_module(oracle)
    expected = {str((ROOT / "tests" / "fixtures" / subdir / name).relative_to(ROOT))
                for _tier, subdir, name in
                [*oracle.NATIVE_CORPUS, *oracle.COMPONENT_CORPUS]}
    assert set(report["corpus"]) == expected
    assert tool._compile_corpus()
