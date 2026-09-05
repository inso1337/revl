"""The LINE-coverage gate for the self-host oracles (roadmap item 429).

`tests/test_selfhost_coverage.py` gates DISPATCH ARMS. This module gates
STATEMENTS, and it is the one the mirrored surface rests on, because an arm the
corpus reaches can have nearly all of its body unexercised. Measured: 2868 of
the 8233 unexecuted reference statements sit inside functions the corpus DOES
call — the mass a construct table structurally cannot see. The construct survey
reported 20% blind where statements report 53%.

Both sides are measured. The reference (`backends/<tier>/emit.py`) directly, it
being python; the port (`selfhost/emit_<tier>.rvl`) through the python module it
compiles to, which is how its own oracle runs it.

The mirrored pairs, spelled as paths so `tools/affected_tests.py::_tier_tests`
selects this module whenever one of them changes — new logic in a reference
emitter is precisely the event that opens a fresh uncovered region:
`backends/python/emit.py`, `backends/typescript/emit.py`, `backends/go/emit.py`,
`backends/java/emit.py`, `backends/rust/emit.py`, `backends/wasm/emit.py`.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TIERS = ("py", "ts", "go", "java", "rust", "wasm")


@pytest.fixture(scope="module")
def lines():
    spec = importlib.util.spec_from_file_location(
        "selfhost_line_coverage_tool", ROOT / "tools" / "selfhost_line_coverage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def line_data(lines):
    return {"reference": lines.measure(), "selfhost": lines.measure_selfhost()}


def test_reference_and_selfhost_line_coverage_match_the_ledger(lines, line_data):
    """The line gate, both halves.

    Failing here means an uncovered count moved. UP: logic arrived in a mirrored
    emitter that no corpus document reaches, which is how the item-429(d)
    `Secret[T]` gap opened and stayed green. DOWN: coverage improved and the
    ratchet has to be told, or it is not a ratchet. Either way the message names
    the function; go read it before touching the ledger.
    """
    problems = lines.check(line_data)
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("tier", TIERS)
def test_the_line_measurement_is_not_empty(lines, line_data, tier):
    """Non-vacuity again, and it matters more here than for the construct table:
    a coverage session that traced nothing reports every statement missing (a
    loud failure), but one that traced a file with no statements reports perfect
    coverage (a silent pass). Demand real statement counts on both sides."""
    reference = line_data["reference"][tier]
    port = line_data["selfhost"][tier]
    assert reference["statements"] > 500, (
        f"backends/*/emit.py for {tier} reported {reference['statements']} "
        f"statements: the coverage session is not tracing the reference")
    assert port["statements"] > 500, (
        f"the emitted selfhost/emit_{tier}.rvl reported {port['statements']} "
        f"statements: the coverage session is not tracing the port")
    assert 0 < reference["uncovered"] < reference["statements"]


def test_the_selfhost_side_has_real_declared_functions(lines, line_data):
    """Zero never-entered functions is valid when statement measurement is real."""
    for tier in TIERS:
        found = line_data["selfhost"][tier]
        assert found["declared"] > 30, (
            f"selfhost/emit_{tier}.rvl declared {found['declared']} functions: "
            f"the `.rvl` -> emitted `def` name mapping has broken")


def test_generic_baseline_cannot_masquerade_as_closure(lines, monkeypatch, tmp_path):
    ledger = {half: {tier: {"statements": 1,
                            "uncovered": {"NEVER ENTERED": {"f": 1}}}
                     for tier in TIERS}
              for half in ("reference", "selfhost")}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(lines, "LEDGER", path)
    data = {half: {tier: {"functions": {}, "sizes": {}}
                   for tier in TIERS}
            for half in ("reference", "selfhost")}
    problems = lines.check(data)
    assert any("generic reason" in problem for problem in problems)


def test_line_closure_rejects_duplicates_and_missing_sides(lines, monkeypatch, tmp_path):
    ledger = {"reference": {"py": {"uncovered": {
        "specific decision": {"f": 1}, "another decision": {"f": 1}}}}}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(lines, "LEDGER", path)
    problems = lines.check({"reference": {}, "selfhost": {}})
    assert any("appears in multiple reasons" in problem for problem in problems)
    assert any("missing line-coverage side" in problem for problem in problems)


def test_line_check_fails_closed_on_malformed_maps_and_counts(lines, monkeypatch, tmp_path):
    ledger = {half: {tier: {"uncovered": None} for tier in TIERS}
              for half in ("reference", "selfhost")}
    ledger["reference"]["py"]["uncovered"] = {"specific decision": {"f": "bad"}}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(lines, "LEDGER", path)
    data = {half: {tier: {"functions": {"f": "bad"}, "sizes": {}}
                   for tier in TIERS}
            for half in ("reference", "selfhost")}
    problems = lines.check(data)
    assert any("invalid uncovered count" in problem for problem in problems)
    assert any("invalid measured count" in problem for problem in problems)
