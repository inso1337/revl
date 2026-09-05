"""Non-vacuity and shape checks for the construct-reach report."""

import importlib.util
import sys
from pathlib import Path


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
