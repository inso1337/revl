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

Both halves are also driven over `tests/fixtures/emit_<tier>_refusals/`, the
documents a tier's reference REFUSES BY NAME (issue #1419). A refusal is logic
both halves carry and no `CORPUS` document can reach, because a corpus document
is one the reference emits. Five tiers have one: `emit_ts_refusals/`,
`emit_go_refusals/`, `emit_java_refusals/`, `emit_rust_refusals/` and
`emit_wasm_refusals/`.
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


@pytest.mark.parametrize("functions", [None, [], {"f": True}])
def test_line_check_rejects_malformed_function_population(lines, monkeypatch, tmp_path, functions):
    ledger = {half: {tier: {"uncovered": {"specific decision": {}}}
                     for tier in TIERS} for half in ("reference", "selfhost")}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(lines, "LEDGER", path)
    data = {half: {tier: {"functions": functions, "sizes": {}}
                   for tier in TIERS} for half in ("reference", "selfhost")}
    problems = lines.check(data)
    assert any("survey functions are not a map" in problem
               for problem in problems) or any("invalid measured count" in problem
                                                for problem in problems)


# --------------------------------------------------------------------------
# issue #1419: the third option, and the budget.
#
# The gate had two responses to an uncovered statement: reach it with a corpus
# document, or record it. For every refusal a tier states by name, only the
# second was ever possible, because a corpus document is one the reference EMITS
# and a refusal path runs only where it does not. So that population could only
# accumulate, and it did. The tests below hold both halves of the fix: refusal
# documents really are refused (otherwise the directory buys coverage with no
# agreement assertion behind it), and `_budget` really does survive `--write`
# (otherwise the fastest green is still the complete green).


def test_every_refusal_document_is_refused_by_its_tiers_reference(lines):
    """The non-vacuity guard on the refusal corpus.

    A document in `emit_<tier>_refusals/` that the reference EMITS is a
    byte-agreement case: it belongs in that tier's `CORPUS`, where the oracle
    holds its bytes. Left in the refusal directory it would buy line coverage
    with nothing asserting the output is right, which is the shape item 429
    exists to rule out. The frontend has to accept it too: a document that does
    not compile reaches no emitter statement at all and would be a silent
    no-op here.
    """
    from revl import compile_files  # noqa: PLC0415

    found = 0
    for tier in TIERS:
        reference = lines.load_reference(tier)
        for document in lines.refusal_documents(tier):
            found += 1
            ir = compile_files([str(document)])
            with pytest.raises(reference.EmitError):
                reference.emit(ir)
    assert found > 0, (
        "no refusal documents at all: tests/fixtures/emit_<tier>_refusals/ is "
        "the only way a refusal's two mirrored halves ever get reached, and an "
        "empty one makes this gate's third option a claim rather than a fact")


def test_the_rust_required_stream_refusal_is_reached_rather_than_recorded(lines):
    """Issue #1419's own red, held at the fix rather than at the symptom.

    `selfhost/emit_rust.rvl::require_ty` renders a requirement's type and marks a
    `Stream[T]` one instead of splicing it in verbatim; `backends/rust/emit.py::
    _refuse_required_stream` is the reference half that raises. Both were
    recorded as unreachable. Neither is in the ledger now, and this asserts the
    reason: the document that reaches them exists, and it is not a corpus
    document because it cannot be one.
    """
    recorded = json.loads(lines.LEDGER.read_text())
    for half, tier, name in (("reference", "rust", "_refuse_required_stream"),
                             ("selfhost", "rust", "require_ty")):
        entry = recorded[half][tier]["uncovered"]
        assert not any(name in functions for functions in entry.values()), (
            f"{half}/{tier} records `{name}` again. It is reached by "
            f"tests/fixtures/emit_rust_refusals/, so recording it is recording "
            f"something that is not true (issue #1419)")
    assert "required_stream_coeffect.rvl" in [
        p.name for p in lines.refusal_documents("rust")]


def test_the_budget_fires_in_both_directions(lines, monkeypatch, tmp_path):
    """Recording is not free, and an improvement is not re-spendable.

    Above the budget the gate names the overrun and asks for the raise by hand.
    Below it the gate asks for the ceiling to come down, which is the half that
    makes the direction stick: headroom left behind by a fix is exactly what the
    next unreached region spends without anyone deciding to.
    """
    def ledger_with(budget):
        path = tmp_path / f"ledger-{budget}.json"
        path.write_text(json.dumps({
            "_budget": {half: {tier: budget for tier in TIERS}
                        for half in ("reference", "selfhost")},
            **{half: {tier: {"uncovered": {"a specific decision": {"f": 10}}}
                      for tier in TIERS}
               for half in ("reference", "selfhost")}}))
        return path

    monkeypatch.setattr(lines, "LEDGER", ledger_with(4))
    over = lines.check({half: {tier: {"functions": {"f": 10}, "sizes": {"f": 10}}
                               for tier in TIERS}
                        for half in ("reference", "selfhost")})
    assert any("`_budget` allows 4" in problem for problem in over)

    monkeypatch.setattr(lines, "LEDGER", ledger_with(40))
    under = lines.check({half: {tier: {"functions": {"f": 10}, "sizes": {"f": 10}}
                                for tier in TIERS}
                         for half in ("reference", "selfhost")})
    assert any("still allows 40" in problem for problem in under)

    monkeypatch.setattr(lines, "LEDGER", ledger_with(10))
    exact = lines.check({half: {tier: {"functions": {"f": 10}, "sizes": {"f": 10}}
                                for tier in TIERS}
                         for half in ("reference", "selfhost")})
    assert not any("_budget" in problem for problem in exact)


def test_a_missing_budget_is_itself_a_failure(lines, monkeypatch, tmp_path):
    """Fail closed. A ledger with no budget is the state this gate was in
    before #1419, and it has to be a failure rather than a default, or deleting
    four lines is a way to make recording free again."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(
        {half: {tier: {"uncovered": {"a specific decision": {"f": 1}}}
                for tier in TIERS} for half in ("reference", "selfhost")}))
    monkeypatch.setattr(lines, "LEDGER", path)
    problems = lines.check({half: {tier: {"functions": {"f": 1}, "sizes": {"f": 1}}
                                   for tier in TIERS}
                            for half in ("reference", "selfhost")})
    assert sum("no `_budget` declared" in problem for problem in problems) == 2


def test_write_does_not_touch_the_budget(lines, monkeypatch, tmp_path):
    """The mechanism has to survive the thing that keeps happening: the gate
    fires, `--write` is the fastest green, and `--write` is always available.

    So `--write` stays available and stops being sufficient. It rewrites every
    per-function count and leaves the budget alone, which leaves `--check` red
    with one number named. Written as an assertion because a future regeneration
    that folded the budget in would restore the old incentive silently.
    """
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({
        "_budget": {half: {tier: 1 for tier in TIERS}
                    for half in ("reference", "selfhost")},
        **{half: {tier: {"uncovered": {"a specific decision": {"stdlib_f": 1}}}
                  for tier in TIERS}
           for half in ("reference", "selfhost")}}))
    monkeypatch.setattr(lines, "LEDGER", path)
    lines.write_ledger({half: {tier: {"statements": 99,
                                      "functions": {"stdlib_f": 40},
                                      "sizes": {"stdlib_f": 40}}
                               for tier in TIERS}
                        for half in ("reference", "selfhost")})
    rewritten = json.loads(path.read_text())
    assert rewritten["_budget"]["reference"]["py"] == 1, (
        "`--write` moved the budget, so `--write` is a complete answer again")
    problems = lines.check({half: {tier: {"functions": {"stdlib_f": 40},
                                          "sizes": {"stdlib_f": 40}}
                                   for tier in TIERS}
                            for half in ("reference", "selfhost")})
    assert any("`_budget` allows 1" in problem for problem in problems)
