"""Focused differential survey contracts, independent of the live triage ledger."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def survey():
    spec = importlib.util.spec_from_file_location(
        "differential_survey", ROOT / "tools" / "selfhost_differential_survey.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmitError(Exception):
    pass


def reference(value):
    return SimpleNamespace(emit=lambda ir: value, EmitError=EmitError)


def raising(error):
    def call(ir):
        raise error
    return call


@pytest.mark.parametrize(("output", "status"), [
    ("wanted", "byte_agreement"),
    ("different", "byte_divergence"),
    ("<<UNSUPPORTED-EXPR:adt>>", "port_refusal"),
    (None, "port_error"),
])
def test_compare_distinguishes_outputs(survey, output, status):
    assert survey.compare({}, "py", reference("wanted"), lambda ir: output)["status"] == status


def test_compare_retains_error_and_byte_evidence(survey):
    ref = reference("\u00e9")
    row = survey.compare({}, "py", ref, lambda ir: "\u00e9x")
    assert row["reference"]["bytes"] == 2
    assert row["first_difference"]["byte"] == 2
    assert row["selfhost"]["sha256"] != row["reference"]["sha256"]
    ref.emit = raising(EmitError("named tier limit"))
    row = survey.compare({}, "py", ref, raising(AssertionError("must not call port")))
    assert row == {"status": "reference_rejection",
                   "error": {"type": "EmitError", "message": "named tier limit"}}
    row = survey.compare({}, "py", reference("ok"), raising(KeyError("missing")))
    assert row["status"] == "port_error" and row["error"]["type"] == "KeyError"


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(3), OSError("broken setup")])
def test_unexpected_errors_and_interrupts_are_not_survey_success(survey, error):
    with pytest.raises(type(error)):
        survey.compare({}, "py", reference("ok"), raising(error))
    ref = reference("ok")
    ref.emit = raising(error)
    with pytest.raises(type(error)):
        survey.compare({}, "py", ref, lambda ir: "ok")


def test_wasm_matches_the_oracle_projection(survey):
    row = survey.compare({}, "wasm", reference({"functions": "wat", "component": "other"}),
                         lambda ir: "wat")
    assert row["status"] == "byte_agreement"
    row = survey.compare({}, "wasm", reference({"component": "wat"}),
                         raising(AssertionError("must not run")))
    assert row["status"] == "reference_rejection"
    assert row["error"]["type"] == "OracleSlice"


def test_wasm_component_rows_are_not_selection_evidence(survey):
    assert survey.selection_exclusion("wasm", {"components": [{"name": "C"}]})
    assert survey.selection_exclusion("wasm", {"functions": {}}) is None
    baseline = [{"document": "mixed.rvl", "reached": {"reference": [1], "selfhost": [1]},
                 "status": "byte_agreement", "in_corpus": True,
                 "selection_excluded": "wasm_functions_projection_discards_component_output"}]
    candidate = [{"document": "mixed-extra.rvl", "reached": {"reference": [2], "selfhost": [2]},
                 "status": "byte_agreement", "in_corpus": False,
                 "selection_excluded": "wasm_functions_projection_discards_component_output"}]
    assert survey.select_cover(candidate, baseline) == {
        "baseline_lines": 0, "selected": [], "additional_lines": 0}


def test_tree_walk_excludes_junk_and_stabilizes_identity(survey, tmp_path):
    for name in ("b.rvl", "nested/a.rvl", ".git/a.rvl", ".venv/a.rvl", "venv/a.rvl",
                 "build/a.rvl", "node_modules/a.rvl", "target/a.rvl", "custom-env/a.rvl"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (tmp_path / "custom-env" / "pyvenv.cfg").write_text("")
    (tmp_path / "linked.rvl").symlink_to(tmp_path / "b.rvl")
    docs = survey.documents_in_tree(tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in docs] == ["b.rvl", "nested/a.rvl"]
    assert survey.document_paths(["nested/../b.rvl", "b.rvl"], tmp_path) == [tmp_path / "b.rvl"]
    with pytest.raises(ValueError):
        survey.document_paths([tmp_path.parent / "outside.rvl"], tmp_path)
    with pytest.raises(ValueError):
        survey.document_paths([], tmp_path)


def test_selection_uses_both_sides_only_agreements_and_lexical_ties(survey):
    def row(name, ref, port, status="byte_agreement", baseline=False):
        return {"document": name, "reached": {"reference": ref, "selfhost": port},
                "status": status, "in_corpus": baseline}
    baseline = [row("old.rvl", [1], [1], baseline=True)]
    candidates = [
        row("bad.rvl", list(range(100)), [], status="byte_divergence"),
        row("b.rvl", [1, 2], [1, 2]), row("a.rvl", [1, 2], [1, 2]),
        row("c.rvl", [1], [1, 3]), row("zero.rvl", [1], [1]), *baseline,
    ]
    assert survey.select_cover(candidates, baseline) == {
        "baseline_lines": 2, "additional_lines": 3, "selected": [
            {"document": "a.rvl", "new_reference_lines": 1, "new_selfhost_lines": 1},
            {"document": "c.rvl", "new_reference_lines": 0, "new_selfhost_lines": 1},
        ]}


def test_real_survey_bootstraps_outside_checkout(survey, tmp_path):
    output = tmp_path / "survey.json"
    result = subprocess.run([
        sys.executable, str(ROOT / "tools" / "selfhost_differential_survey.py"),
        "--tiers", "py", "rust", "--documents",
        "tests/fixtures/emit_rust_corpus/reserved_names.rvl", "--json", str(output),
    ], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text())
    assert len(data["results"]) == 2
    assert all(r["status"] == "byte_agreement" for r in data["results"])
    assert data["documents"] == ["tests/fixtures/emit_rust_corpus/reserved_names.rvl"]


def test_compile_rejection_is_recorded_for_each_selected_tier(survey, monkeypatch):
    monkeypatch.setattr(survey, "compile_files", raising(
        survey.RevlError("bad.rvl", 1, "named frontend rejection")))
    data = survey.survey(["py", "rust"], ["tests/fixtures/emit_rust_corpus/reserved_names.rvl"])
    assert len(data["results"]) == 2
    assert all(row["status"] == "compile_rejection" for row in data["results"])
    assert all(row["error"]["type"] == "RevlError" for row in data["results"])


def test_real_selection_measures_declared_port_functions(survey):
    data = survey.survey(["py"], ["tests/fixtures/emit_rust_corpus/reserved_names.rvl"], select=True)
    row = data["results"][0]
    assert row["status"] == "byte_agreement"
    assert row["reached"]["reference"] and row["reached"]["selfhost"]
    assert data["selection"]["py"]["baseline_lines"] > 500
    assert data["selection"]["py"]["baseline_failures"] == []
