"""The Temporal side of the seeded-defect saga corpus (issue #2736, second slice):
bench/saga_defects/run_temporal.py and bench/saga_defects/temporal/.

The static half runs everywhere and needs neither node nor a server. Every
corpus row has a Temporal entry, so no row is silently left unscored; every
port derives from the clean sources with each edit applying exactly as often as
it says; the judge reads fabricated outcomes the way it reads real ones; and the
committed results file agrees with the data, with the revl results file and with
its own totals.

The measurement half runs the ports on a real Temporal dev server and compares
what it sees with the data and the committed table. It is slow by nature and is
gated on `REVL_TEMPORAL_DEV_SERVER`, which the `temporal-exit` CI job sets
beside the step that runs this file (tests/test_env_gated_skips_run_somewhere.py
checks the pairing). Past the gate nothing skips: a missing node or install is
a failure, so a broken provisioning step cannot read as green.
"""

import importlib.util
import json
import os
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "saga_defects"
RESULTS = ROOT / "bench" / "results" / "saga-defect-corpus-temporal.md"
REVL_RESULTS = ROOT / "bench" / "results" / "saga-defect-corpus.md"

# A BARE read, deliberately: only CI (or a developer with a server up) supplies a
# value, which is what makes the env-gate audit see this as a gate.
_ADDRESS = os.environ.get("REVL_TEMPORAL_DEV_SERVER")


def _load():
    spec = importlib.util.spec_from_file_location(
        "saga_defects_temporal_bench", BENCH / "run_temporal.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T = _load()
DATA, UNITS = T.load_units()
BY_ID = {unit["id"]: unit for unit in UNITS}
ROW_UNITS = [unit for unit in UNITS if unit["source"] == "row"]
PORTED = [unit for unit in UNITS if unit["expected"] != "no-counterpart"]
NO_COUNTERPART = [unit for unit in UNITS if unit["expected"] == "no-counterpart"]


def _outcome(*runs, steps=None, control_failed=False):
    """A harness outcome: one run per step, each `(failed, world)`."""
    built = []
    for index, (failed, world) in enumerate(runs, 1):
        built.append({"step": index, "failed": failed,
                      "message": f"injected fault after step {index}", "world": world})
    return {"workflow": "checkout", "steps": steps if steps is not None else len(built),
            "control": {"step": None, "failed": control_failed, "message": "boom",
                        "world": _world()},
            "runs": built}


def _world(inventory=(), payments=(), locks=(), journal=()):
    return {"inventory": [list(item) for item in inventory],
            "payments": [list(item) for item in payments],
            "locks": list(locks), "settled": [], "reversed": [], "migrationLog": [],
            "journal": [{"name": name, "args": [], "ok": ok} for name, ok in journal]}


CLEAN_UNWIND = [("reserve", True), ("faultPoint", False), ("release", True)]


# ------------------------------------------------------------------ static

def test_every_corpus_row_has_a_temporal_entry():
    """Silence is not a score: a row with no Temporal port says why instead."""
    assert {unit["id"] for unit in ROW_UNITS} == {row["id"] for row in DATA["defects"]}
    for unit in NO_COUNTERPART:
        assert len(unit["reason"].split()) >= 8, unit["id"]


def test_the_units_are_the_rows_the_temporal_only_defect_and_the_controls():
    sources = {unit["source"] for unit in UNITS}
    assert sources == {"row", "temporal-only", "control"}
    assert {unit["id"] for unit in UNITS if unit["source"] == "control"} == {
        "checkout-clean", "migrator-clean"}
    assert [unit["id"] for unit in UNITS if unit["source"] == "temporal-only"] == [
        "wrong-lifo-order"]


@pytest.mark.parametrize("unit", PORTED, ids=[unit["id"] for unit in PORTED])
def test_each_port_derives_with_every_edit_applying_as_often_as_it_says(unit, tmp_path):
    T.derive(unit, tmp_path / unit["id"])
    clean = {name: (BENCH / "temporal" / "src" / name).read_text(encoding="utf-8")
             for name in T.SOURCES}
    derived = {name: (tmp_path / unit["id"] / "src" / name).read_text(encoding="utf-8")
               for name in T.SOURCES}
    changed = {name for name in T.SOURCES if clean[name] != derived[name]}
    if unit["source"] == "control":
        assert not changed
    else:
        assert changed, f"{unit['id']}: the edits seed nothing"
        assert changed <= {"workflows.ts", "activities.ts"}


def test_an_edit_that_does_not_apply_exactly_once_is_an_error_not_a_seed(tmp_path):
    unit = dict(BY_ID["missing-undo-hold"])
    unit["edits"] = [{"file": "workflows.ts", "find": "no such line", "replace": ""}]
    with pytest.raises(T.CorpusError, match="matches 0 time"):
        T.derive(unit, tmp_path / "x")
    unit["edits"] = [{"file": "workflows.ts", "find": "const compensations",
                      "replace": "let compensations"}]
    with pytest.raises(T.CorpusError, match="matches 2 time"):
        T.derive(unit, tmp_path / "y")


def test_a_row_without_a_temporal_entry_is_a_corpus_error(monkeypatch):
    real = T._load_revl_runner()
    data = real.load_corpus()
    del data["defects"][0]["temporal"]
    monkeypatch.setattr(T, "_load_revl_runner",
                        lambda: type("R", (), {"load_corpus": staticmethod(lambda: data)}))
    with pytest.raises(T.CorpusError, match="no `temporal` entry"):
        T.load_units()


# ------------------------------------------------------------------- judging

def test_a_saga_that_unwinds_cleanly_passes_every_assertion():
    runs = [(True, _world(journal=CLEAN_UNWIND))]
    assert T.judge(_outcome(*runs)) == []


def test_a_saga_that_swallows_the_fault_is_caught():
    problems = T.judge(_outcome((False, _world())))
    assert problems and "did not fail" in problems[0]


def test_residue_in_a_reversible_store_is_caught():
    for store, args in (("inventory", {"inventory": [("sku-1", 1)]}),
                        ("payments", {"payments": [("order-1", 4200)]}),
                        ("locks", {"locks": [42]})):
        problems = T.judge(_outcome((True, _world(**args))))
        assert any(f"residue in {store}" in problem for problem in problems), store


def test_a_compensation_that_raised_is_caught():
    journal = [("charge", True), ("refund", False)]
    problems = T.judge(_outcome((True, _world(journal=journal))))
    assert any("`refund` raised" in problem for problem in problems)


def test_compensations_that_ran_oldest_first_are_caught():
    journal = [("reserve", True), ("charge", True), ("faultPoint", False),
               ("release", True), ("refund", True)]
    problems = T.judge(_outcome((True, _world(journal=journal))))
    assert any("newest first is refund, release" in problem for problem in problems)


def test_a_repeated_compensation_cannot_be_placed_so_the_order_is_not_judged():
    journal = [("reserve", True), ("charge", True), ("settle", True),
               ("release", True), ("refund", True), ("release", True)]
    assert T.judge(_outcome((True, _world(journal=journal)))) == []


def test_a_missing_compensation_is_not_an_order_defect():
    journal = [("reserve", True), ("charge", True), ("faultPoint", False), ("refund", True)]
    assert T.judge(_outcome((True, _world(journal=journal)))) == []


def test_a_sweep_with_no_failure_points_is_an_error_not_missed():
    with pytest.raises(T.CorpusError, match="no failure points"):
        T.classify(_outcome(steps=0))


def test_a_port_that_fails_with_no_fault_is_an_error_not_a_verdict():
    outcome = _outcome((True, _world(journal=CLEAN_UNWIND)), control_failed=True)
    with pytest.raises(T.CorpusError, match="fails with no fault injected"):
        T.classify(outcome)


def test_a_failure_that_is_not_the_injected_fault_is_an_error_not_a_verdict():
    """A timeout, a worker fault or a workflow error leaves residue too; scoring it
    would turn a broken harness into a `fault-test`."""
    outcome = _outcome((True, _world(payments=[("order-1", 4200)])))
    outcome["runs"][0]["message"] = "Workflow execution timed out"
    with pytest.raises(T.CorpusError, match="not the injected fault"):
        T.classify(outcome)
    outcome["runs"][0]["message"] = "injected fault after step 2"
    with pytest.raises(T.CorpusError, match="not the injected fault"):
        T.classify(outcome)


def test_a_failed_assertion_is_fault_test_and_a_held_one_is_missed():
    held = T.classify(_outcome((True, _world(journal=CLEAN_UNWIND))))
    assert held["verdict"] == "missed" and held["steps"] == 1
    failed = T.classify(_outcome((True, _world(payments=[("order-1", 1)]))))
    assert failed["verdict"] == "fault-test" and "residue in payments" in failed["detail"]


# ------------------------------------------------------- the committed results

def _table(path):
    verdicts = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| `") and len(cells) == 6:
            verdicts[cells[0].strip("`")] = cells
    return verdicts


def test_the_temporal_results_cover_every_corpus_row_with_its_expected_verdict():
    table = _table(RESULTS)
    assert set(table) == {unit["id"] for unit in ROW_UNITS}
    for unit in ROW_UNITS:
        cells = table[unit["id"]]
        assert cells[3] == unit["expected"], unit["id"]
        assert cells[4].strip("*") == unit["expected"], (
            f"{unit['id']}: the committed table is stale; rerun run_temporal.py --write")


def test_the_temporal_results_revl_column_is_the_committed_revl_table():
    revl = _table(REVL_RESULTS)
    for ident, cells in _table(RESULTS).items():
        assert cells[2] == revl[ident][4].strip("*"), ident


def _stated_totals(text):
    """(rows, ported, compile, fault-test, missed, no-counterpart) from the totals line."""
    stated = re.search(
        r"\*\*(\d+) corpus rows: (\d+) have a Temporal counterpart \((\d+) refused by tsc, "
        r"(\d+) caught by a fault test, (\d+) missed\) and (\d+) have none", text)
    assert stated is not None, "the temporal results lost their totals line"
    return tuple(int(part) for part in stated.groups())


def test_the_temporal_results_totals_are_the_totals_of_the_expected_verdicts():
    counts = {verdict: sum(1 for unit in ROW_UNITS if unit["expected"] == verdict)
              for verdict in T.VERDICTS}
    rows, ported, compile_, fault_test, missed, none = _stated_totals(
        RESULTS.read_text(encoding="utf-8"))
    assert (rows, none) == (len(ROW_UNITS), counts["no-counterpart"])
    assert (compile_, fault_test, missed) == (
        counts["compile"], counts["fault-test"], counts["missed"])
    assert ported == rows - none


def test_the_comparison_paragraph_is_the_two_committed_tables_counted():
    """The sentence that sets revl beside Temporal states four counts per side; each
    must be what the two committed tables say on the rows that exist on both."""
    text = RESULTS.read_text(encoding="utf-8")
    revl_table, temporal_table = _table(REVL_RESULTS), _table(RESULTS)
    both = [ident for ident, cells in temporal_table.items() if cells[4].strip("*") != "no-counterpart"]

    def count(verdicts, verdict):
        return sum(1 for ident in both if verdicts(ident) == verdict)

    revl = lambda ident: revl_table[ident][4].strip("*")  # noqa: E731
    temporal = lambda ident: temporal_table[ident][4].strip("*")  # noqa: E731
    stated = re.search(
        r"On the (\d+) rows that exist on both sides, revl refused (\d+) at compile time, "
        r"caught (\d+) by a fault test and missed (\d+) \((\d+)/\d+ caught\); the Temporal "
        r"ports were refused by `tsc` (\d+) times, caught (\d+) by a fault test and missed "
        r"(\d+) \((\d+)/\d+ caught\)", text)
    assert stated is not None, "the comparison paragraph is missing or reworded"
    n, rc, rf, rm, rcaught, tc, tf, tm, tcaught = (int(part) for part in stated.groups())
    assert n == len(both)
    assert (rc, rf, rm) == (count(revl, "compile"), count(revl, "fault-test"), count(revl, "missed"))
    assert (tc, tf, tm) == (count(temporal, "compile"), count(temporal, "fault-test"),
                            count(temporal, "missed"))
    assert (rcaught, tcaught) == (rc + rf, tc + tf)


def test_every_no_counterpart_row_is_listed_with_its_reason_in_the_results():
    text = RESULTS.read_text(encoding="utf-8")
    for unit in NO_COUNTERPART:
        assert f"- `{unit['id']}`: {unit['reason']}" in text, unit["id"]


def test_the_temporal_only_row_and_the_controls_are_in_the_results():
    text = RESULTS.read_text(encoding="utf-8")
    assert "`wrong-lifo-order` (expected fault-test): **fault-test**" in text
    for ident in ("checkout-clean", "migrator-clean"):
        assert f"- `{ident}`: **clean**" in text


def test_the_runner_never_scores_a_row_it_cannot_run(monkeypatch, capsys):
    monkeypatch.delenv("REVL_TEMPORAL_DEV_SERVER", raising=False)
    assert T.main(["--only", "missing-undo-hold"]) == 2
    assert "a skip is not a verdict" in capsys.readouterr().err


def test_write_needs_every_unit(capsys):
    assert T.main(["--write", "--only", "missing-undo-hold"]) == 2


# ------------------------------------------------------------- measurement

def _provisioned():
    # Past the gate nothing skips: a missing node or install is a failure.
    assert shutil.which("node"), "node is not on PATH, so nothing can be measured"
    assert (BENCH / "temporal" / "node_modules" / ".bin" / "tsc").exists(), (
        "run `npm ci` in bench/saga_defects/temporal")


@pytest.mark.slow
@pytest.mark.skipif(
    not _ADDRESS,
    reason="the ports run on a Temporal dev server; the temporal-exit CI job sets "
           "REVL_TEMPORAL_DEV_SERVER and runs this file")
def test_the_committed_temporal_table_is_the_measured_table():
    _provisioned()
    outcomes = {unit["id"]: T.measure(unit, _ADDRESS) for unit in UNITS}
    drift = [f"{unit['id']}: " + "; ".join(problems)
             for unit in UNITS if (problems := T.disagreements(unit, outcomes[unit["id"]]))]
    assert not drift, "\n".join(drift)
    table = _table(RESULTS)
    stale = [unit["id"] for unit in ROW_UNITS
             if table[unit["id"]][4].strip("*") != outcomes[unit["id"]]["verdict"]]
    assert not stale, f"the committed temporal table is stale for {stale}"
    assert json.dumps(sorted(table)) == json.dumps(sorted(unit["id"] for unit in ROW_UNITS))
    totals = T.tally(UNITS, outcomes)
    assert _stated_totals(RESULTS.read_text(encoding="utf-8")) == (
        totals["rows"], totals["ported"], totals["compile"], totals["fault-test"],
        totals["missed"], totals["no-counterpart"]), "the committed totals line is stale"
